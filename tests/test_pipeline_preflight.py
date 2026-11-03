import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import importlib.util
spec = importlib.util.spec_from_file_location('pipeline_preflight', Path(__file__).resolve().parents[1] / 'scripts/pipeline_preflight.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
copy_digest, intake_record_id, metrics_readiness, read_json, EXACT_APPROVAL = (p.copy_digest, p.intake_record_id, p.metrics_readiness, p.read_json, p.EXACT_APPROVAL)
CONTROL_ID = 'private-control-fixture'

def audit(s, m, exports=None, export_root=None):
    return p.audit(s, m, CONTROL_ID, exports, export_root)



def sample():
    snap = dict(title='One handoff', text='Inspect one handoff.', alt_text='Handoff graphic.',
                destination_url='https://example.com/p/one-handoff', board_service_id='board1')
    body = b'approved-final-bytes'
    sha = hashlib.sha256(body).hexdigest()
    snap['asset_sha256'] = sha
    record = dict(record_id='pin-test', search_intent='handoff-reasoning',
        source={'canonical_url': snap['destination_url']}, publication={'status':'private_draft'},
        approval={'status':EXACT_APPROVAL, 'reference':'user-exact-approval',
                  'asset_sha256':sha, 'copy_sha256':copy_digest(snap), 'immutable_snapshot':snap})
    state={'source_of_truth': {'buffer_channel_id':'channel-fixture', 'publisher_repository':'owner/repo', 'publisher_ledger_branch':'state'}, 'schema_version':1, 'canonical_drive_file_id':CONTROL_ID, 'revision':18,
           'records':{'pin-test':record}}
    public=dict(id='pin-test', **snap, status='approved', editorial_approved=True, visual_qa_approved=True)
    return state, {'schema_version':1,'pins':[public]}, body


class AuditTests(unittest.TestCase):
    def test_never_authorizes_dispatch_or_claims_live_checks(self):
        s,m,_=sample();r=audit(s,m)
        self.assertFalse(r['dispatch_authorized'])
        self.assertEqual(r['network_requests'],0); self.assertEqual(r['state_writes'],0)
        self.assertIn('normal_publisher_destination_check',r['records'][0]['live_gates_not_checked'])

    def test_inputs_not_mutated(self):
        s,m,_=sample();saved=copy.deepcopy((s,m));audit(s,m);self.assertEqual((s,m),saved)

    def test_exact_approved_export(self):
        s,m,b=sample()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'image.png';p.write_bytes(b)
            self.assertEqual(audit(s,m,{'pin-test':p.name},d)['records'][0]['export']['status'], 'exact_approved_bytes_match')

    def test_transformed_export_requires_reapproval(self):
        s,m,_=sample()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'image.png';p.write_bytes(b'transformed')
            self.assertIn('local_export_requires_new_exact_review',audit(s,m,{'pin-test':p.name},d)['records'][0]['issues'])

    def test_symlink_not_followed(self):
        s,m,b=sample()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'real.png';p.write_bytes(b);q=Path(d)/'link.png';q.symlink_to(p)
            with self.assertRaises(ValueError):audit(s,m,{'pin-test':q.name},d)

    def test_copy_tampering_detected(self):
        s,m,_=sample();s['records']['pin-test']['approval']['immutable_snapshot']['title']='Changed'
        self.assertIn('exact_copy_approval_hash_mismatch',audit(s,m)['records'][0]['issues'])

    def test_public_copy_drift_detected(self):
        s,m,_=sample();m['pins'][0]['alt_text']='Different'
        self.assertIn('public_copy_differs_from_exact_approval:alt_text',audit(s,m)['records'][0]['issues'])

    def test_public_review_drafts_flagged(self):
        s,m,_=sample();m['pins'][0].update(id='essay-untracked',editorial_approved=False,status='review')
        r=audit(s,m);self.assertEqual(r['unreconciled_public_records'],['essay-untracked'])
        self.assertIn('public_record_not_exactly_approved',[f['code'] for f in r['findings']])

    def test_wrong_private_file_rejected(self):
        s,m,_=sample();s['canonical_drive_file_id']='other'
        with self.assertRaises(ValueError):audit(s,m)

    def test_record_identity_mismatch_rejected(self):
        s,m,_=sample();s['records']['pin-test']['record_id']='different'
        with self.assertRaises(ValueError):audit(s,m)

    def test_duplicate_public_ids_rejected(self):
        s,m,_=sample();m['pins'].append(copy.deepcopy(m['pins'][0]))
        with self.assertRaises(ValueError):audit(s,m)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'snapshot.json';p.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError):read_json(p)

    def test_legacy_hash_preserved_without_guessing_basis(self):
        s,m,_=sample();s['records']['pin-test']['approval']['status']='locked_existing_approved'
        s['records']['pin-test']['approval']['copy_sha256']='legacy-hash'
        self.assertEqual(audit(s,m)['records'][0]['copy_hash_check'],'preserved_legacy_hash_basis_not_assumed')

    def test_missing_intent_is_not_silently_invented(self):
        s,m,_=sample();s['records']['pin-test']['search_intent']=None
        self.assertIn('canonical_search_intent_missing',audit(s,m)['records'][0]['issues'])

    def test_completed_records_do_not_request_new_creative(self):
        s,m,_=sample();s['records']['pin-test']['publication']['status']='sent'
        self.assertFalse(audit(s,m)['records'][0]['manual_handoff']['required'])

    def test_stale_zero_metrics_not_performance(self):
        r={'publication':{'sent_at':'2026-11-02T23:00:00Z'},
           'metrics':{'buffer_observation':{'metrics_updated_at':'2026-11-02T22:00:00Z'}}}
        o=metrics_readiness(r);self.assertEqual(o['freshness'],'stale_prepublication')
        self.assertIsNone(o['outbound_clicks']);self.assertEqual(o['commercial_attribution'],'unknown')

    def test_missing_metrics_are_unknown(self):
        self.assertEqual(metrics_readiness({})['freshness'],'unknown')

    def test_future_intake_id_is_deterministic(self):
        args=('https://example.com/p/source','handoff-reasoning','v1')
        self.assertEqual(intake_record_id(*args),hashlib.sha256('\n'.join(args).encode()).hexdigest())
        self.assertNotEqual(intake_record_id(*args),intake_record_id(args[0],args[1],'v2'))

    def test_intake_rejects_homepage_tracking_or_separator_collision(self):
        for url,intent,variant in [('https://example.com/','a','b'),
            ('https://example.com/p/x?utm_content=a','a','b'),
            ('https://example.com/p/x','a\nb','c')]:
            with self.assertRaises(ValueError):intake_record_id(url,intent,variant)


class IntegrationTests(unittest.TestCase):
    NOW='2026-11-03T06:00:00Z'
    def source(self, n=1):
        return {'canonical_url':f'https://example.com/p/source-{n}', 'search_intent':f'intent-{n}',
            'variant_id':'v1', 'source_status':'published_verified', 'content_sha256':'a'*64,
            'evidence_ref':f'source-{n}'}
    def observation(self, **extra):
        return dict(record_id='pin-test',stage='destination_qa', status='held',
            attempted=True,error_code='access_denied',evidence_ref='normal-publisher-403', **extra)
    def history(self):
        return {'channel_id':'channel-fixture','complete':True,'observed_at':self.NOW,'posts':[]}
    def ledger(self):
        return {'snapshot':{'observed_at':self.NOW,'repository':'owner/repo','branch':'state','blob_sha':'a'*40}, 'content':{'schema_version':1,'lock':None,'records':{}}}

    def test_stage_plan_instruments_only_supplied_observations(self):
        s,m,_=sample();r=p.integration_plan(s,m,CONTROL_ID,'run1',self.NOW)
        self.assertEqual(r['stage_events'],[])
        self.assertFalse(r['dispatch_authorized'])
        self.assertTrue(r['deployment_boundary']['single_existing_orchestrator'])
        self.assertFalse(r['deployment_boundary']['new_scheduler'])

    def test_bounded_five_sources_three_drafts(self):
        s,_,_=sample();r=p.source_intake_plan(s,[self.source(n) for n in range(10)])
        self.assertEqual(r['inspected'],5);self.assertEqual(r['drafts_planned'],3)
        self.assertEqual(r['source_items_deferred'],5)
        self.assertFalse(r['items'][0]['publication_approval'])

    def test_same_batch_source_intent_variant_deduplication(self):
        s,_,_=sample();a=self.source();b=dict(a,variant_id='v2')
        r=p.source_intake_plan(s,[a,b])
        self.assertEqual(r['drafts_planned'],1)
        self.assertEqual(r['items'][1]['action'],'reuse_existing_record')

    def test_existing_proposal_reused(self):
        s,_,_=sample();src=self.source();s['records']['pin-test']['private_draft_proposals']=[{
            'evidence_url':src['canonical_url'],'search_intent':src['search_intent']}]
        self.assertEqual(p.source_intake_plan(s,[src])['items'][0]['record_id'],'pin-test')

    def test_denied_source_not_retried(self):
        s,_,_=sample();src=dict(self.source(),restriction='access_denied')
        item=p.source_intake_plan(s,[src])['items'][0]
        self.assertFalse(item['retry_allowed']);self.assertEqual(item['action'],'hold_source_restriction')

    def test_unknown_source_no_draft(self):
        s,_,_=sample();src=dict(self.source(),source_status='unknown')
        self.assertEqual(p.source_intake_plan(s,[src])['drafts_planned'],0)

    def test_approval_stage_cannot_be_instrumented(self):
        s,_,_=sample();o=self.observation();o['stage']='approval'
        with self.assertRaises(ValueError):p.stage_events(s,[o],'run1',self.NOW)

    def test_extra_event_fields_rejected(self):
        s,_,_=sample();o=self.observation();o['approval']=True
        with self.assertRaises(ValueError):p.stage_events(s,[o],'run1',self.NOW)

    def test_denied_event_holds_no_retry(self):
        s,_,_=sample();events=p.stage_events(s,[self.observation()],'run1',self.NOW)
        self.assertEqual(events[0]['next_action'],'hold_no_retry_until_resolved')

    def test_duplicate_stage_same_run_rejected(self):
        s,_,_=sample();o=self.observation()
        with self.assertRaises(ValueError):p.stage_events(s,[o,o],'run1',self.NOW)

    def test_instrumentation_merge_preserves_authority_and_unknown_fields(self):
        s,_,_=sample();s['records']['pin-test']['custom_field']={'keep':'yes'}
        before=copy.deepcopy(s);events=p.stage_events(s,[self.observation()],'run1',self.NOW)
        after=p.propose_instrumentation(s,events,p.digest(s),CONTROL_ID)
        self.assertEqual(s,before)
        self.assertEqual(after['records']['pin-test']['approval'],before['records']['pin-test']['approval'])
        self.assertEqual(after['records']['pin-test']['custom_field'],{'keep':'yes'})
        self.assertEqual(after['revision'],19)
        self.assertEqual(after['records']['pin-test']['pipeline_observations']['destination_qa']['attempts'],1)

    def test_idempotent_repeated_event_no_second_attempt_or_revision(self):
        s,_,_=sample();events=p.stage_events(s,[self.observation()],'run1',self.NOW)
        first=p.propose_instrumentation(s,events,p.digest(s),CONTROL_ID)
        second=p.propose_instrumentation(first,events,p.digest(first),CONTROL_ID)
        self.assertEqual(first,second)

    def test_stale_state_hash_blocks_merge(self):
        s,_,_=sample();events=p.stage_events(s,[self.observation()],'run1',self.NOW)
        old=p.digest(s);s['revision']+=1
        with self.assertRaises(ValueError):p.propose_instrumentation(s,events,old,CONTROL_ID)

    def test_event_tampering_rejected(self):
        s,_,_=sample();events=p.stage_events(s,[self.observation()],'run1',self.NOW)
        events[0]['next_action']='retry_now'
        with self.assertRaises(ValueError):p.propose_instrumentation(s,events,p.digest(s),CONTROL_ID)

    def test_no_attempt_when_not_attempted(self):
        s,_,_=sample();o=self.observation();o['attempted']=False
        events=p.stage_events(s,[o],'run1',self.NOW)
        after=p.propose_instrumentation(s,events,p.digest(s),CONTROL_ID)
        self.assertEqual(after['records']['pin-test']['pipeline_observations']['destination_qa']['attempts'],0)

    def test_old_event_after_newer_event_rejected_bounded_watermark(self):
        s,_,_=sample();a=p.stage_events(s,[self.observation()],'run1',self.NOW)
        b=p.stage_events(s,[self.observation()],'run2','2026-11-04T06:00:00Z')
        after=p.propose_instrumentation(s,a,p.digest(s),CONTROL_ID)
        after=p.propose_instrumentation(after,b,p.digest(after),CONTROL_ID)
        with self.assertRaises(ValueError):p.propose_instrumentation(after,a,p.digest(after),CONTROL_ID)
        stats=after['records']['pin-test']['pipeline_observations']['destination_qa']
        self.assertEqual(stats['events'],2);self.assertNotIn('applied_events',after)

    def test_empty_event_batch_is_no_op(self):
        s,_,_=sample();self.assertEqual(p.propose_instrumentation(s,[],p.digest(s),CONTROL_ID),s)

    def test_missing_history_never_empty_history(self):
        s,_,_=sample();r=p.reconcile_plan(s,self.ledger(),None,self.NOW)
        self.assertFalse(r['read_only_comparison_complete']);self.assertTrue(r['holds'])

    def test_full_correct_empty_history_is_only_a_snapshot(self):
        s,_,_=sample();r=p.reconcile_plan(s,self.ledger(),self.history(),self.NOW)
        self.assertTrue(r['read_only_comparison_complete']);self.assertNotIn('dispatch_authorized',r)

    def test_incomplete_wrong_channel_stale_history_held(self):
        s,_,_=sample()
        for k,v in [('complete',False),('channel_id','other'),('observed_at','2026-11-03T05:00:00Z')]:
            with self.subTest(k=k):
                h=self.history();h[k]=v
                self.assertFalse(p.reconcile_plan(s,self.ledger(),h,self.NOW)['read_only_comparison_complete'])

    def test_ledger_unknown_intent_holds_without_unlock(self):
        s,_,_=sample();ledger=self.ledger();ledger['content']['lock']='pin-test'
        ledger['content']['records']['pin-test']={'state':'intent'}
        r=p.reconcile_plan(s,ledger,self.history(),self.NOW)
        self.assertFalse(r['read_only_comparison_complete']);self.assertEqual(ledger['content']['lock'],'pin-test')

    def test_stale_ledger_creates_reconciliation_action_only(self):
        s,_,_=sample();ledger=self.ledger();ledger['content']['records']['pin-test']={
            'state':'recorded','buffer_post_id':'post1','status':'scheduled'}
        h=self.history();h['posts']=[{'id':'post1','channelId':'channel-fixture','status':'sent',
            'sentAt':'2026-11-03T05:00:00Z','dueAt':'2026-11-03T04:59:00Z'}]
        r=p.reconcile_plan(s,ledger,h,self.NOW)
        self.assertEqual(r['actions'][0]['action'],'publisher_reconciliation_required')
        self.assertFalse(r['actions'][0]['create_allowed']);self.assertFalse(r['actions'][0]['direct_ledger_write_allowed'])

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ('../outside.png','/tmp/outside.png'):
                with self.assertRaises(ValueError):p.safe_export_path(root,name)

    def test_parent_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root)/'nested').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):p.safe_export_path(root,'nested/image.png')

    def test_boolean_schema_rejected(self):
        s,m,_=sample();s['schema_version']=True
        with self.assertRaises(ValueError):audit(s,m)

    def test_nonfinite_json_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'nan.json';path.write_text('{"x":NaN}')
            with self.assertRaises(ValueError):read_json(path)


    def test_generated_event_without_optional_error_code_merges(self):
        s,_,_=sample();o=self.observation();del o['error_code']
        events=p.stage_events(s,[o],'run1',self.NOW)
        self.assertIsNone(events[0]['error_code'])
        p.propose_instrumentation(s,events,p.digest(s),CONTROL_ID)

    def test_attempt_status_consistency(self):
        s,_,_=sample();o=self.observation();o['status']='not_attempted'
        with self.assertRaises(ValueError):p.stage_events(s,[o],'run1',self.NOW)

    def test_nested_null_object_rejected_cleanly(self):
        for key in ('approval','source','publication','metrics','creative_handoff','stages'):
            s,m,_=sample();s['records']['pin-test'][key]=None
            with self.subTest(key=key),self.assertRaises(ValueError):audit(s,m)

    def test_restricted_reused_source_stays_held(self):
        s,_,_=sample();source=self.source();source['restriction']='image_transfer_stall'
        s['records']['pin-test']['source']['canonical_url']=source['canonical_url']
        s['records']['pin-test']['search_intent']=source['search_intent']
        item=p.source_intake_plan(s,[source])['items'][0]
        self.assertEqual(item['record_id'],'pin-test');self.assertFalse(item['retry_allowed'])

    def test_stage_hold_overrides_export_action(self):
        s,m,_=sample();r=p.integration_plan(s,m,CONTROL_ID,'run1',self.NOW,restrictions=[{
            'record_id':'pin-test','stage':'approved_asset_handoff','error_code':'image_transfer_stall'}])
        export=[a for a in r['actions'] if a.get('stage')=='approved_asset_handoff'][0]
        self.assertEqual(export['action'],'hold_stage_no_retry');self.assertFalse(export['retry_allowed'])

    def test_legacy_unscoped_hold_prevents_external_recommendations(self):
        s,m,_=sample();s['records']['pin-test']['blockers']=['access_denied']
        r=p.integration_plan(s,m,CONTROL_ID,'run1',self.NOW)
        self.assertTrue(all(a['action']=='hold_stage_no_retry' for a in r['actions'] if a.get('stage')))

    def test_creative_denial_does_not_block_metrics(self):
        s,m,_=sample();s['records']['pin-test']['approval']['status']='not_approved'
        o=self.observation();o['stage']='creative_handoff'
        r=p.integration_plan(s,m,CONTROL_ID,'run1',self.NOW,observations=[o])
        self.assertEqual([a['action'] for a in r['actions'] if a.get('stage')=='creative_handoff'],['hold_stage_no_retry'])
        self.assertEqual([a['action'] for a in r['actions'] if a.get('stage')=='measure'],['refresh_available_aggregate_metrics'])

    def test_external_manual_post_reported_not_adopted(self):
        s,_,_=sample();h=self.history();h['posts']=[{'id':'manual1','channelId':'channel-fixture',
            'status':'sent','sentAt':'2026-11-03T05:00:00Z'}]
        r=p.reconcile_plan(s,self.ledger(),h,self.NOW)
        self.assertEqual(r['external_channel_post_ids'],['manual1'])
        self.assertFalse(r['dispatch_eligibility_established'])

    def test_ledger_provenance_required(self):
        s,_,_=sample();ledger=self.ledger();ledger['snapshot']['repository']='wrong/repo'
        r=p.reconcile_plan(s,ledger,self.history(),self.NOW)
        self.assertFalse(r['read_only_comparison_complete'])

    def test_invalid_or_overdue_history_timestamp_held(self):
        s,_,_=sample()
        for status,fields in [('sent',{}),('sent',{'sentAt':'2026-11-04T00:00:00Z'}),
                             ('scheduled',{'dueAt':'2026-11-03T05:00:00Z'})]:
            h=self.history();h['posts']=[dict(id='post1',channelId='channel-fixture',status=status,**fields)]
            with self.subTest(status=status,fields=fields):
                self.assertFalse(p.reconcile_plan(s,self.ledger(),h,self.NOW)['read_only_comparison_complete'])


    def test_sticky_denial_survives_routine_later_observation(self):
        s,m,_=sample();a=p.stage_events(s,[self.observation()],'run1',self.NOW)
        after=p.propose_instrumentation(s,a,p.digest(s),CONTROL_ID)
        routine=self.observation();routine.update(status='not_attempted',attempted=False,error_code=None)
        b=p.stage_events(after,[routine],'run2','2026-11-04T06:00:00Z')
        after=p.propose_instrumentation(after,b,p.digest(after),CONTROL_ID)
        stats=after['records']['pin-test']['pipeline_observations']['destination_qa']
        self.assertIsNone(stats['error_code'])
        self.assertIn('access_denied',stats['active_restrictions'])
        self.assertEqual(p.restriction_map(after,[],[])[('pin-test','destination_qa')],'access_denied')

    def test_corrupt_populated_watermark_rejected(self):
        s,_,_=sample();a=p.stage_events(s,[self.observation()],'run1',self.NOW)
        after=p.propose_instrumentation(s,a,p.digest(s),CONTROL_ID)
        after['records']['pin-test']['pipeline_observations']['destination_qa']['last_observed_at']='broken'
        b=p.stage_events(after,[self.observation()],'run2','2026-11-04T06:00:00Z')
        with self.assertRaises(ValueError):p.propose_instrumentation(after,b,p.digest(after),CONTROL_ID)

    def test_multiple_hold_types_are_preserved(self):
        s,_,_=sample();a=p.stage_events(s,[self.observation()],'run1',self.NOW)
        after=p.propose_instrumentation(s,a,p.digest(s),CONTROL_ID)
        other=self.observation();other['error_code']='image_transfer_stall'
        b=p.stage_events(after,[other],'run2','2026-11-04T06:00:00Z')
        after=p.propose_instrumentation(after,b,p.digest(after),CONTROL_ID)
        active=after['records']['pin-test']['pipeline_observations']['destination_qa']['active_restrictions']
        self.assertEqual(set(active),{'access_denied','image_transfer_stall'})


if __name__=='__main__':unittest.main()
