"""Offline fake-API/CAS tests. No credentials, network, or live writes."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_approved_pin_publisher as base
p = base.p


class DailyCadenceTests(unittest.TestCase):
    run_publish = base.PublisherTests.run_publish
    def setUp(self):
        base.PublisherTests.setUp(self)
        self.now = p.timestamp('2026-11-03T14:00:00Z')
        self.clock = patch.object(p, 'utc_now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def remote(self, status, due=None, sent=None, id='manual'):
        return {'id': id, 'channelId': p.CHANNEL, 'status': status,
                'dueAt': due, 'sentAt': sent}

    def test_custom_schedule_uses_existing_earliest_slot(self):
        result = self.run_publish(False)
        self.assertEqual(result['schedule']['due_at'], '2026-11-03T20:00:00Z')
        self.run_publish()
        record = self.ledger.state['records'][self.pin['id']]
        payload = p.candidate(self.pin, self.assets)[0]
        self.assertEqual(record['payload_sha256'], p.digest(payload))
        custom = p.scheduled_payload(payload, record['schedule'])
        self.assertEqual(custom['mode'], 'customScheduled')
        self.assertEqual(custom['schedulingType'], 'automatic')
        self.assertEqual(record['scheduled_payload_sha256'], p.digest(custom))

    def test_every_remote_publication_status_occupies_local_day(self):
        for post in [self.remote('scheduled', '2026-11-04T01:00:00Z'),
                     self.remote('sending', '2026-11-03T20:00:00Z'),
                     self.remote('sent', sent='2026-11-03T12:00:00Z')]:
            with self.subTest(post=post):
                self.api.items = [post]
                self.assertEqual(self.run_publish(False)['schedule']['local_date'], '2026-11-04')

    def test_missing_naive_invalid_future_sent_and_stale_pending_block(self):
        bad = [self.remote('sent'), self.remote('scheduled'), self.remote('sending'),
               self.remote('scheduled', '2026-11-04T12:00:00'),
               self.remote('scheduled', 'garbage'),
               self.remote('scheduled', '2026-11-03T13:59:00Z'),
               self.remote('sending', '2026-11-03T02:00:00Z'),
               self.remote('sending', '2026-11-03T13:59:00Z'),
               self.remote('sending', '2026-11-04T20:00:00Z'),
               self.remote('sent', sent='2026-11-04T12:00:00Z')]
        for post in bad:
            with self.subTest(post=post):
                self.api.items = [post]
                with self.assertRaises(p.Blocked): self.run_publish()
                self.assertEqual(self.api.calls, 0)
                self.assertEqual(self.ledger.version, 0)

    def test_ledger_only_sent_date_and_original_reservation_remain_occupied(self):
        self.ledger.state['records']['history'] = {
            'state': 'recorded', 'status': 'sent', 'sent_at': '2026-11-03T12:00:00Z',
            'buffer_post_id': 'removed', 'payload_sha256': 'immutable-historical-hash'}
        old = copy.deepcopy(self.ledger.state['records']['history'])
        self.assertEqual(self.run_publish(False)['schedule']['local_date'], '2026-11-04')
        self.run_publish()
        self.assertEqual(self.ledger.state['records']['history'], old)

    def test_ledger_missing_pending_post_fails_closed(self):
        self.ledger.state['records']['history'] = {'state': 'recorded', 'status': 'scheduled',
            'due_at': '2026-11-04T20:00:00Z', 'buffer_post_id': 'missing'}
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)

    def test_multiple_creates_in_one_run_reserve_distinct_days(self):
        self.run_publish()
        self.pin.update(id='pin-03-test', search_intent='different', title='Different title')
        body = (self.assets / self.pin['asset']).read_bytes() + b'new-art'
        self.pin['asset'] = 'tce_pin_03_publish.jpg'
        (self.assets / self.pin['asset']).write_bytes(body)
        self.pin['asset_sha256'] = p.hashlib.sha256(body).hexdigest()
        # Real API IDs are unique; give the first fake item its own identity.
        self.api.items[0]['id'] = 'first-post'
        self.ledger.state['records']['pin-02-test']['buffer_post_id'] = 'first-post'
        self.run_publish()
        dates = [r['schedule']['local_date'] for r in self.ledger.state['records'].values()]
        self.assertEqual(dates, ['2026-11-03', '2026-11-04'])
        self.assertEqual(self.run_publish()['action'], 'remote_duplicate_no_create')
        self.assertEqual(self.api.calls, 2)

    def test_after_lock_collision_retains_claim_and_blocks_create(self):
        original = self.api.posts
        reads = 0
        def posts():
            nonlocal reads
            reads += 1
            return original() if reads == 1 else [self.remote('scheduled', '2026-11-03T23:00:00Z')]
        with patch.object(self.api, 'posts', side_effect=posts):
            with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.ledger.state['records'][self.pin['id']]['schedule']['local_date'], '2026-11-03')

    def test_stale_cas_runner_cannot_create(self):
        original = self.ledger.cas
        def race(state, sha):
            competing, _ = self.ledger.read()
            competing['lock'] = 'another-runner'
            original(competing, sha)
            return original(state, sha)
        with patch.object(self.ledger, 'cas', side_effect=race):
            with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)

    def test_remote_due_at_mismatch_keeps_lock_and_reconcile_blocks(self):
        original = self.api.create
        def wrong(payload):
            id = original(payload)
            self.api.items[0]['dueAt'] = '2026-11-04T20:00:00Z'
            return id
        with patch.object(self.api, 'create', side_effect=wrong):
            with self.assertRaises(p.Blocked): self.run_publish()
        with self.assertRaises(p.Blocked): p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.api.calls, 1)

    def test_timezone_or_schedule_change_blocks(self):
        for channel in [{}, {'timezone': 'UTC', 'postingSchedule': []},
                        {'timezone': p.CHANNEL_TIMEZONE, 'postingSchedule': []}]:
            with patch.object(self.api, 'verify', return_value=channel):
                with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)

    def test_dst_dates_not_fixed_24_hour_offsets(self):
        channel = self.api.verify()
        for now, expected in [('2026-03-07T14:00:00Z', '2026-03-08T19:00:00Z'),
                              ('2026-10-31T14:00:00Z', '2026-11-01T20:00:00Z')]:
            now = p.timestamp(now)
            occupied = {now.astimezone(p.LOCAL_ZONE).date()}
            self.assertEqual(p.choose_schedule(channel, occupied, now)['due_at'], expected)

    def test_dst_gap_skipped_and_fold_uses_first_occurrence(self):
        channel = {'timezone': p.CHANNEL_TIMEZONE, 'postingSchedule': [
            {'day': 'sun', 'times': ['02:30', '03:30'], 'paused': False}]}
        result = p.choose_schedule(channel, set(), p.timestamp('2026-03-08T05:00:00Z'))
        self.assertEqual(result['due_at'], '2026-03-08T07:30:00Z')
        channel['postingSchedule'][0]['times'] = ['01:30']
        result = p.choose_schedule(channel, set(), p.timestamp('2026-11-01T04:00:00Z'))
        self.assertEqual(result['due_at'], '2026-11-01T05:30:00Z')

    def test_paused_day_and_elapsed_slots_skipped(self):
        channel = self.api.verify()
        channel['postingSchedule'][1]['paused'] = True
        self.assertEqual(p.choose_schedule(channel, set(), self.now)['local_date'], '2026-11-04')
        channel['postingSchedule'][1]['paused'] = False
        self.assertEqual(p.choose_schedule(channel, set(), p.timestamp('2026-11-03T22:58:00Z'))['local_date'], '2026-11-04')

    def test_legacy_payload_reconcile_preserves_old_hash(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.api.create(payload)
        self.api.items[0].update(status='sent', sentAt='2026-11-03T12:00:00Z')
        fingerprints = {'payload_sha256': p.digest(payload), 'asset_sha256': self.pin['asset_sha256'],
                        'intent_sha256': p.digest([self.pin['source_id'], self.pin['search_intent']])}
        self.ledger.state['records'][self.pin['id']] = {'state': 'recorded', **fingerprints,
                                                       **p.outcome(self.api.items[0])}
        before = copy.deepcopy(self.ledger.state)
        self.assertEqual(self.run_publish()['ledger_refresh'], 'unchanged')
        self.assertEqual(self.ledger.state, before)
        p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.ledger.state, before)
        self.assertNotIn('schedule', self.ledger.state['records'][self.pin['id']])

    def test_corrupt_reservation_or_hash_blocks_reconcile(self):
        self.api.crash = True
        with self.assertRaises(p.Blocked): self.run_publish()
        record = self.ledger.state['records'][self.pin['id']]
        record['schedule']['local_date'] = '2026-11-04'
        with self.assertRaises(p.Blocked): p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.api.calls, 1)

    def test_historical_manifest_payload_hashes_and_first_pin_exclusion(self):
        pins = json.loads((p.ROOT / 'campaign/pins.json').read_text())['pins']
        expected = {
            'pin-02-revisited-decisions': '3a9e6ead9b72bcc4843d6c9a4659095de795036b850af6dcb797df7d1931c794',
            'pin-03-lost-context': 'b232708c9b921ead54ba82cd63f7927d25ae8390cb0ef958fb901b3a7fa72c04'}
        by_id = {pin['id']: pin for pin in pins}
        with self.assertRaises(p.Blocked): p.candidate(by_id[p.FIRST_ID])
        for pin_id, payload_hash in expected.items():
            with self.subTest(pin_id=pin_id):
                pin = by_id[pin_id]
                if pin.get('buffer_post_id') or pin.get('status') in ('sent', 'scheduled'):
                    with self.assertRaises(p.Blocked): p.candidate(pin)
                # Lifecycle updates must not change the historical content hash.
                # Only a local snapshot is made eligible; the manifest is untouched.
                snapshot = copy.deepcopy(pin)
                snapshot.pop('buffer_post_id', None)
                snapshot['status'] = 'approved'
                payload, _ = p.candidate(snapshot)
                self.assertEqual(p.digest(payload), payload_hash)
                self.assertEqual(payload['mode'], 'addToQueue')

    def test_original_reservation_never_released_if_remote_post_moves(self):
        self.run_publish()
        record = self.ledger.state['records'][self.pin['id']]
        self.api.items[0]['dueAt'] = '2026-11-04T20:00:00Z'
        occupied = p.occupied_dates(self.api.items, self.ledger.state['records'], self.now)
        self.assertEqual({d.isoformat() for d in occupied}, {'2026-11-03', '2026-11-04'})
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(record['schedule']['local_date'], '2026-11-03')

    def test_date_near_midnight_is_eastern_not_utc(self):
        post = self.remote('sent', sent='2026-11-03T02:56:11Z')
        occupied = p.occupied_dates([post], {}, self.now)
        self.assertEqual({d.isoformat() for d in occupied}, {'2026-11-02'})
        self.api.items = [post]
        self.assertEqual(self.run_publish(False)['schedule']['local_date'], '2026-11-03')

    def test_stale_legacy_pending_record_uses_matching_fresh_sent_post(self):
        self.api.items = [self.remote('sent', '2026-11-03T02:56:00Z', '2026-11-03T02:56:11Z', id='pin3')]
        self.ledger.state['records']['pin-03-history'] = {
            'state': 'recorded', 'status': 'scheduled', 'due_at': '2026-11-03T02:56:00Z',
            'buffer_post_id': 'pin3', 'payload_sha256': 'original-unchanged'}
        history = copy.deepcopy(self.ledger.state['records']['pin-03-history'])
        self.run_publish()
        self.assertEqual(self.ledger.state['records']['pin-03-history'], history)
        self.assertEqual(self.ledger.state['records'][self.pin['id']]['schedule']['local_date'], '2026-11-03')

    def test_schedule_change_after_lock_retains_intent(self):
        channel = self.api.verify()
        changed = copy.deepcopy(channel)
        changed['postingSchedule'][1]['times'] = ['16:00']
        with patch.object(self.api, 'verify', side_effect=[channel, changed]):
            with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])

    def test_no_cadence_preview_without_ledger(self):
        with self.assertRaises(p.Blocked):
            p.publish(self.pin, self.api, None, False, self.assets, lambda a,b: None)
        self.assertEqual(self.api.calls, 0)

    def test_reconcile_mismatched_readback_identity_retains_claim(self):
        self.api.crash = True
        with self.assertRaises(p.Blocked): self.run_publish()
        before, version = self.ledger.read()
        wrong = dict(self.api.items[0], id='wrong-post-id')
        with patch.object(self.api, 'post', return_value=wrong):
            with self.assertRaises(p.Blocked):
                p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.ledger.state, before)
        self.assertEqual(self.ledger.version, version)
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.api.calls, 1)

    def test_future_sent_readback_does_not_finalize_create(self):
        create = self.api.create
        def future_sent(payload):
            result = create(payload)
            self.api.items[0].update(status='sent', sentAt=payload['dueAt'])
            return result
        with patch.object(self.api, 'create', side_effect=future_sent):
            with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.ledger.state['records'][self.pin['id']]['state'], 'intent')
        self.assertEqual(self.api.calls, 1)
        before, version = self.ledger.read()
        with self.assertRaises(p.Blocked):
            p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.ledger.state, before)
        self.assertEqual(self.ledger.version, version)

    def test_future_sent_readback_does_not_refresh_record(self):
        self.run_publish()
        before, version = self.ledger.read()
        self.api.items[0].update(status='sent', sentAt=self.api.items[0]['dueAt'])
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.ledger.state, before)
        self.assertEqual(self.ledger.version, version)
        self.assertEqual(self.api.calls, 1)

    def test_off_day_sending_readback_does_not_finalize_or_reconcile(self):
        self.api.items = [self.remote('sent', sent='2026-11-03T12:00:00Z')]
        create = self.api.create
        def sending(payload):
            result = create(payload)
            self.api.items[-1]['status'] = 'sending'
            return result
        with patch.object(self.api, 'create', side_effect=sending):
            with self.assertRaises(p.Blocked): self.run_publish()
        before, version = self.ledger.read()
        self.assertEqual(before['records'][self.pin['id']]['schedule']['local_date'], '2026-11-04')
        with self.assertRaises(p.Blocked):
            p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.ledger.state, before)
        self.assertEqual(self.ledger.version, version)
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.api.calls, 1)

    def test_overdue_pending_readback_cannot_refresh_or_reconcile(self):
        self.run_publish()
        before, version = self.ledger.read()
        for status in ('scheduled', 'sending'):
            self.api.items[0]['status'] = status
            with patch.object(p, 'utc_now', return_value=p.timestamp('2026-11-04T14:00:00Z')):
                with self.assertRaises(p.Blocked): self.run_publish()
                with self.assertRaises(p.Blocked):
                    p.reconcile(self.pin, self.api, self.ledger, self.assets)
            self.assertEqual(self.ledger.state, before)
            self.assertEqual(self.ledger.version, version)
        self.assertEqual(self.api.calls, 1)
