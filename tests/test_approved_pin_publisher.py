import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).parents[1] / 'scripts/approved_pin_publisher.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class Ledger:
    def __init__(self):
        self.state = {'schema_version': 1, 'lock': None, 'records': {}}
        self.version = 0
        self.fail_at = None
    def read(self):
        return copy.deepcopy(self.state), self.version
    def cas(self, state, sha):
        if sha != self.version or self.fail_at == self.version:
            raise p.Blocked('CAS failed')
        self.state = copy.deepcopy(state)
        self.version += 1
        return self.version

class API:
    def __init__(self):
        self.items = []
        self.calls = 0
        self.crash = False
        self.read_crash = False
    def verify(self):
        pass
    def posts(self):
        return copy.deepcopy(self.items)
    def create(self, payload):
        self.calls += 1
        m = payload['metadata']['pinterest']
        self.items.append({'id': 'new-post', 'channelId': p.CHANNEL, 'status': 'scheduled',
            'text': payload['text'], 'metadata': {'title': m['title'], 'url': m['url'],
            'board': {'serviceId': m['boardServiceId']}}, 'assets': [{'source': payload['assets'][0]['image']['url'], 'image': {'altText': payload['assets'][0]['image']['metadata']['altText']}}]})
        if self.crash:
            raise p.Blocked('ambiguous create')
        return 'new-post'
    def post(self, pid):
        if self.read_crash:
            raise p.Blocked('read failed after create')
        return next(x for x in self.items if x['id'] == pid)

class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = Path(self.tmp.name)
        body = b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (600).to_bytes(4, 'big') + (900).to_bytes(4, 'big')
        (self.assets / 'tce_pin_02_publish.jpg').write_bytes(body)
        self.pin = {'id': 'pin-02-test', 'status': 'approved', 'editorial_approved': True,
            'visual_qa_approved': True, 'board_service_id': p.BOARD, 'destination_url': p.DESTINATION,
            'title': 'Approved title', 'text': 'Approved description', 'alt_text': 'Approved alt',
            'approval_ref': 'review-123', 'source_id': 'kg-diagnostic', 'search_intent': 'test',
            'asset': 'tce_pin_02_publish.jpg', 'asset_sha256': hashlib.sha256(body).hexdigest()}
        self.api, self.ledger = API(), Ledger()
    def run_publish(self, execute=True):
        return p.publish(self.pin, self.api, self.ledger, execute, self.assets, lambda a,b: None)
    def test_dry_run_no_writes(self):
        self.assertEqual(self.run_publish(False)['action'], 'dry_run_ready')
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.ledger.version, 0)
    def test_success_and_repeat(self):
        self.assertEqual(self.run_publish()['action'], 'created')
        self.assertEqual(self.run_publish()['action'], 'remote_duplicate_no_create')
        self.assertEqual(self.api.calls, 1)
    def test_cap_ten(self):
        self.api.items = [{'id': str(i), 'status': 'scheduled'} for i in range(10)]
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
    def test_nine_allows_one(self):
        self.api.items = [{'id': str(i), 'status': 'scheduled'} for i in range(9)]
        self.run_publish()
        self.assertEqual(self.api.calls, 1)
    def test_missing_approval(self):
        for key in ('editorial_approved', 'visual_qa_approved'):
            with self.subTest(key=key):
                pin = dict(self.pin, **{key: False})
                with self.assertRaises(p.Blocked): p.candidate(pin, self.assets)
    def test_first_pin_always_rejected(self):
        self.pin['id'] = p.FIRST_ID
        with self.assertRaises(p.Blocked): self.run_publish()
    def test_already_processed(self):
        self.pin['buffer_post_id'] = p.FIRST_POST
        with self.assertRaises(p.Blocked): self.run_publish()
    def test_wrong_board_destination_asset_hash(self):
        for key in ('board_service_id', 'destination_url', 'asset_sha256', 'asset'):
            with self.subTest(key=key):
                with self.assertRaises(p.Blocked): p.candidate(dict(self.pin, **{key: 'bad'}), self.assets)
    def test_cas_failure_prevents_create(self):
        self.ledger.fail_at = 0
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
    def test_global_lock_prevents_other_runner(self):
        self.ledger.state['lock'] = 'other-pin'
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
    def test_ambiguous_create_reconciles_without_retry(self):
        self.api.crash = True
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.ledger.state['lock'], self.pin['id'])
        self.assertEqual(self.run_publish()['action'], 'remote_duplicate_no_create')
        result = p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(result['action'], 'reconciled_no_create')
        self.assertIsNone(self.ledger.state['lock'])
        self.assertEqual(self.api.calls, 1)
    def test_crash_after_create_before_verify(self):
        self.api.read_crash = True
        with self.assertRaises(p.Blocked): self.run_publish()
        self.api.read_crash = False
        p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.api.calls, 1)
    def test_crash_before_create_is_never_automatically_retried(self):
        self.ledger.state['records'][self.pin['id']] = {'state': 'intent', 'payload_sha256': p.digest(p.candidate(self.pin, self.assets)[0])}
        self.ledger.state['lock'] = self.pin['id']
        with self.assertRaises(p.Blocked): self.run_publish()
        with self.assertRaises(p.Blocked): p.reconcile(self.pin, self.api, self.ledger, self.assets)
        self.assertEqual(self.api.calls, 0)
    def test_result_save_failure_retains_intent(self):
        self.ledger.fail_at = 1
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.ledger.state['records'][self.pin['id']]['state'], 'intent')
    def test_sent_requires_url_and_timestamp(self):
        post = {'id': 'p', 'channelId': p.CHANNEL, 'status': 'sent'}
        self.assertFalse(p.outcome(post)['publication_verified'])
        post.update(sentAt='2026-11-03T00:00:00Z', externalLink='https://www.pinterest.com/pin/123')
        self.assertEqual(p.outcome(post)['publication_id'], '123')
    def test_image_schema(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.assertEqual(payload['assets'][0]['image']['metadata']['altText'], 'Approved alt')
    def test_redirect_rejected(self):
        with self.assertRaises(p.Blocked): p.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test')
    def test_credentials_never_sent_to_asset_url(self):
        with self.assertRaises(p.Blocked): p.request(p.IMAGE_BASE + 'x.jpg', token='FAKE')
    def test_remote_image_mismatch_blocks(self):
        payload, body = p.candidate(self.pin, self.assets)
        with patch.object(p, 'request', return_value=(b'wrong', 'image/jpeg')):
            with self.assertRaises(p.Blocked): p.verify_urls(payload, body)
    def test_pagination_incomplete_blocks(self):
        api = p.Buffer('FAKE')
        with patch.object(api, 'query', return_value={'posts': {'edges': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'same'}}}):
            with self.assertRaises(p.Blocked): api.posts()

    def test_invalid_image_header(self):
        with self.assertRaises(p.Blocked): p.image_dimensions(b'not-an-image')
    def test_cross_id_fingerprint_claim_blocks(self):
        self.ledger.state['records']['old-id'] = {'state': 'recorded', 'asset_sha256': self.pin['asset_sha256']}
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.api.calls, 0)
    def test_two_stale_claims_only_one_wins(self):
        a, sha_a = self.ledger.read()
        b, sha_b = self.ledger.read()
        a['lock'] = 'runner-a'
        b['lock'] = 'runner-b'
        self.ledger.cas(a, sha_a)
        with self.assertRaises(p.Blocked): self.ledger.cas(b, sha_b)
        self.assertEqual(self.ledger.state['lock'], 'runner-a')
    def test_unknown_remote_status_blocks(self):
        api = p.Buffer('FAKE')
        with patch.object(api, 'query', return_value={'posts': {'edges': [{'node': {'id': 'x', 'channelId': p.CHANNEL, 'status': 'unknown'}}], 'pageInfo': {'hasNextPage': False}}}):
            with self.assertRaises(p.Blocked): api.posts()
    def test_remote_missing_alt_fails_strict_verification(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.api.create(payload)
        self.api.items[0]['assets'][0]['image']['altText'] = 'unapproved'
        self.assertFalse(p.verified_content(self.api.items[0], payload))
    def test_live_audit_reports_pending_and_existing_without_writes(self):
        import contextlib
        import io
        import json
        self.api.items = [{'id': p.FIRST_POST, 'channelId': p.CHANNEL, 'status': 'sent',
                           'sentAt': '2026-11-02T22:15:04.050Z',
                           'externalLink': 'https://www.pinterest.com/pin/1083467622891676708'}]
        output = io.StringIO()
        with patch.object(p, 'Buffer', return_value=self.api), patch('sys.argv', ['publisher', '--mode', 'audit']), patch.object(p, 'verify_urls'), contextlib.redirect_stdout(output):
            p.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['mode'], 'read_only_audit')
        self.assertEqual(result['available_slots'], 10)
        self.assertTrue(result['pins'][0]['publication_verified'])
        self.assertEqual([x['action'] for x in result['pins'][1:]], ['ready_to_schedule', 'ready_to_schedule'])
        self.assertEqual(self.api.calls, 0)
    def test_replenish_sequential_and_skips_unapproved(self):
        pins = [dict(self.pin, editorial_approved=False), self.pin, dict(self.pin, id='pin-next')]
        order = []
        def fake_publish(pin, api, ledger, execute=False):
            order.append(pin['id'])
            self.assertTrue(execute)
            return {'action': 'created'}
        with patch.object(p, 'candidate', side_effect=lambda pin: (_ for _ in ()).throw(p.Blocked('unapproved')) if not pin['editorial_approved'] else ({}, b'')), patch.object(p, 'publish', side_effect=fake_publish):
            result = p.replenish(pins, self.api, self.ledger)
        self.assertEqual(order, [self.pin['id'], 'pin-next'])
        self.assertEqual(result['pins'][0]['action'], 'skipped')
    def test_replenish_uncertain_write_stops_batch(self):
        with patch.object(p, 'candidate', return_value=({}, b'')), patch.object(p, 'publish', side_effect=p.Blocked('uncertain')) as create:
            with self.assertRaises(p.Blocked): p.replenish([self.pin, self.pin], self.api, self.ledger)
            self.assertEqual(create.call_count, 1)
    def test_write_guard_blocks_until_enabled_on_canonical_main(self):
        import os
        with patch.dict(os.environ, {'PUBLISHER_ENABLED': 'false', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': p.REPO}):
            with self.assertRaises(p.Blocked): p.write_guard()
        with patch.dict(os.environ, {'PUBLISHER_ENABLED': 'true', 'GITHUB_REF': 'refs/heads/evil', 'GITHUB_REPOSITORY': p.REPO}):
            with self.assertRaises(p.Blocked): p.write_guard()
    def test_tracking_url_is_deterministic_and_keeps_canonical_manifest(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.assertEqual(payload['metadata']['pinterest']['url'], p.DESTINATION + '?utm_source=pinterest&utm_medium=organic_social&utm_campaign=kg_diagnostic&utm_content=pin-02-test')
        self.assertEqual(self.pin['destination_url'], p.DESTINATION)
        with self.assertRaises(p.Blocked): p.tracked_destination('x&redirect=evil')
    def test_untracked_identical_copy_suppresses_tracked_duplicate(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.api.create(payload)
        self.api.items[0]['metadata']['url'] = p.DESTINATION
        self.api.items[0]['assets'] = []
        self.assertTrue(p.matches(self.api.items[0], payload))
        self.assertFalse(p.verified_content(self.api.items[0], payload))
        self.assertEqual(self.run_publish()['action'], 'remote_duplicate_no_create')
        self.assertEqual(self.api.calls, 1)
    def test_unknown_query_is_not_canonicalized(self):
        self.assertIsNone(p.canonical_destination(p.DESTINATION + '?redirect=elsewhere'))
        self.assertIsNone(p.canonical_destination(p.DESTINATION + '#other'))
    def test_replenish_full_queue_defers_without_claim_or_create(self):
        self.api.items = [{'id': str(i), 'status': 'scheduled'} for i in range(10)]
        original_candidate = p.candidate
        with patch.object(p, 'candidate', side_effect=lambda pin, *args: original_candidate(pin, self.assets)):
            result = p.replenish([self.pin, dict(self.pin, id='pin-next')], self.api, self.ledger)
        self.assertEqual(result['pins'], [{'pin_id': self.pin['id'], 'action': 'deferred', 'reason': 'queue_capacity_reached'}])
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.ledger.version, 0)
        self.assertIsNone(self.ledger.state['lock'])
        self.assertEqual(self.ledger.state['records'], {})
    def test_recorded_sent_refresh_and_repeat_are_create_free(self):
        self.run_publish()
        self.api.items[0].update(status='sent', sentAt='2026-11-03T01:40:00Z', externalLink='https://www.pinterest.com/pin/123')
        before = self.ledger.version
        result = self.run_publish()
        self.assertEqual(result['ledger_refresh'], 'updated')
        self.assertEqual(self.ledger.version, before + 1)
        record = self.ledger.state['records'][self.pin['id']]
        self.assertEqual(record['state'], 'recorded')
        self.assertEqual(record['status'], 'sent')
        self.assertEqual(record['publication_id'], '123')
        self.assertTrue(record['publication_verified'])
        self.assertIsNone(self.ledger.state['lock'])
        self.assertEqual(self.run_publish()['ledger_refresh'], 'unchanged')
        self.assertEqual(self.ledger.version, before + 1)
        self.assertEqual(self.api.calls, 1)
    def test_refresh_never_clears_intent_or_any_lock(self):
        self.run_publish()
        before = self.ledger.version
        record = self.ledger.state['records'][self.pin['id']]
        record['state'] = 'intent'
        self.assertEqual(self.run_publish()['ledger_refresh'], 'review_required')
        record['state'] = 'recorded'
        for lock in [self.pin['id'], 'other-pin']:
            self.ledger.state['lock'] = lock
            self.assertEqual(self.run_publish()['ledger_refresh'], 'deferred_locked')
            self.assertEqual(self.ledger.state['lock'], lock)
        self.assertEqual(self.ledger.version, before)
        self.assertEqual(self.api.calls, 1)
    def test_refresh_mismatch_and_multiple_duplicates_never_overwrite(self):
        self.run_publish()
        before = self.ledger.version
        original = copy.deepcopy(self.ledger.state)
        for key in ['buffer_post_id', 'payload_sha256', 'asset_sha256', 'intent_sha256']:
            self.ledger.state = copy.deepcopy(original)
            self.ledger.state['records'][self.pin['id']][key] = 'mismatch'
            self.assertEqual(self.run_publish()['ledger_refresh'], 'review_required')
        self.ledger.state = copy.deepcopy(original)
        self.api.items.append(dict(self.api.items[0], id='another-duplicate'))
        self.assertEqual(self.run_publish()['ledger_refresh'], 'review_required')
        self.assertEqual(self.ledger.version, before)
        self.assertEqual(self.api.calls, 1)
    def test_unknown_duplicate_does_not_initialize_ledger_record(self):
        payload, _ = p.candidate(self.pin, self.assets)
        self.api.create(payload)
        self.assertEqual(self.run_publish()['ledger_refresh'], 'no_record')
        self.assertEqual(self.ledger.state['records'], {})
        self.assertEqual(self.ledger.version, 0)
        self.assertEqual(self.api.calls, 1)
    def test_refresh_cas_conflict_stops_without_retry_or_create(self):
        self.run_publish()
        self.api.items[0]['status'] = 'sending'
        self.ledger.fail_at = self.ledger.version
        with self.assertRaises(p.Blocked): self.run_publish()
        self.assertEqual(self.ledger.state['records'][self.pin['id']]['status'], 'scheduled')
        self.assertEqual(self.api.calls, 1)
    def test_dry_run_duplicate_never_writes_ledger(self):
        self.run_publish()
        self.api.items[0]['status'] = 'sending'
        before = self.ledger.version
        self.assertNotIn('ledger_refresh', self.run_publish(False))
        self.assertEqual(self.ledger.version, before)
        self.assertEqual(self.api.calls, 1)
    def test_refresh_fresh_post_content_mismatch_never_overwrites(self):
        self.run_publish()
        before = self.ledger.version
        wrong = copy.deepcopy(self.api.items[0])
        wrong['assets'][0]['image']['altText'] = 'unexpected'
        with patch.object(self.api, 'post', return_value=wrong):
            self.assertEqual(self.run_publish()['ledger_refresh'], 'review_required')
        self.assertEqual(self.ledger.version, before)
        self.assertEqual(self.api.calls, 1)
