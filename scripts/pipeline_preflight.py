#!/usr/bin/env python3
"""Private, read-only reconciliation. No network, state writes, approval, or dispatch.

Inputs are already-retrieved snapshots. Output is a private diagnostic, never a
publisher manifest. A clean result is not permission to publish. Final bytes,
live route validation, full channel history, ledger, and capacity remain gates.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import re
import os

MAX_JSON_BYTES = 5_000_000
MAX_SOURCE_ITEMS = 5
MAX_DRAFTS = 3
COPY_FIELDS = ('title', 'text', 'alt_text', 'destination_url', 'board_service_id')
EXACT_APPROVAL = 'approved_exact_copy_image_board_destination'
LEGACY_APPROVALS = {'locked_existing_published', 'locked_existing_approved'}
PENDING_LIVE_GATES = [
    'source_and_reader_promise_review', 'exact_final_visual_review',
    'normal_publisher_destination_check', 'hosted_bytes_equal_approved_bytes',
    'full_channel_history_and_ledger_reconciliation', 'one_Eastern_day_and_queue_capacity',
]


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('invalid or oversized JSON input')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('oversized JSON input')
    def reject_constant(_):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw, object_pairs_hook=unique_fields, parse_constant=reject_constant)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def validate_state(state, expected_control_id):
    if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
            or state['schema_version'] != 1 or not isinstance(expected_control_id, str)
            or not expected_control_id or state.get('canonical_drive_file_id') != expected_control_id
            or type(state.get('revision')) is not int or state['revision'] < 0
            or not isinstance(state.get('records'), dict)):
        raise ValueError('wrong private control identity or schema')
    if any(not isinstance(record, dict) or key != record.get('record_id')
           for key, record in state['records'].items()):
        raise ValueError('private record key/identity mismatch')
    if not isinstance(state.get('source_of_truth', {}), dict):
        raise ValueError('invalid source of truth')
    for record in state['records'].values():
        for key in ('approval', 'source', 'publication', 'metrics', 'creative_handoff', 'pipeline_observations', 'stages'):
            if not isinstance(record.get(key, {}), dict):
                raise ValueError('invalid nested record object')
        if record.get('approval', {}).get('immutable_snapshot') is not None and not isinstance(
                record['approval']['immutable_snapshot'], dict):
            raise ValueError('invalid approval snapshot')
        if not isinstance(record.get('metrics', {}).get('buffer_observation', {}), dict):
            raise ValueError('invalid metrics observation')
        if (not isinstance(record.get('private_draft_proposals', []), list)
                or any(not isinstance(row, dict) for row in record.get('private_draft_proposals', []))
                or not isinstance(record.get('blockers', []), list)
                or any(not isinstance(code, str) for code in record.get('blockers', []))):
            raise ValueError('invalid nested record list')
        if any(not isinstance(row, dict) for row in record.get('pipeline_observations', {}).values()):
            raise ValueError('invalid instrumentation shape')


def safe_export_path(export_root, relative_name):
    if export_root is None or not isinstance(relative_name, str):
        raise ValueError('explicit private export root and relative filename required')
    root = Path(export_root)
    child = Path(relative_name)
    if root.is_symlink() or child.is_absolute() or not child.parts or '..' in child.parts:
        raise ValueError('export path escapes explicit root or is a symlink')
    root = root.resolve(strict=True)
    path = root / child
    if any((root / Path(*child.parts[:i])).is_symlink() for i in range(1, len(child.parts) + 1)):
        raise ValueError('export symlink rejected')
    if not path.resolve().is_relative_to(root):
        raise ValueError('export path escapes explicit root')
    return path


def copy_digest(snapshot):
    if not isinstance(snapshot, dict) or any(
        not isinstance(snapshot.get(k), str) or not snapshot[k].strip() for k in COPY_FIELDS
    ):
        raise ValueError('complete exact copy snapshot required')
    canonical = {k: snapshot[k] for k in COPY_FIELDS}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def intake_record_id(canonical_source_url, search_intent, variant_id):
    """Use a previously verified canonical URL, never an inferred redirect target."""
    if not isinstance(canonical_source_url, str):
        raise ValueError('canonical source URL required')
    parts = urlsplit(canonical_source_url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or not parts.path or parts.path == '/'):
        raise ValueError('specific canonical HTTPS source URL required')
    for value in (canonical_source_url, search_intent, variant_id):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or '\n' in value or '\r' in value:
            raise ValueError('nonempty exact source, intent and variant required')
    return hashlib.sha256('\n'.join((canonical_source_url, search_intent, variant_id)).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def metrics_readiness(record):
    observed = record.get('metrics', {}).get('buffer_observation', {})
    updated = timestamp(observed.get('metrics_updated_at'))
    published = timestamp(record.get('publication', {}).get('sent_at') or
                          record.get('publication', {}).get('published_at'))
    if updated is None or published is None:
        freshness = 'unknown'
    elif updated <= published:
        freshness = 'stale_prepublication'
    else:
        freshness = 'postpublication_timestamp_present'
    return {'freshness': freshness, 'provider_updated_at': observed.get('metrics_updated_at'),
            'outbound_clicks': None, 'commercial_attribution': 'unknown',
            'note': 'Buffer Pin Clicks are not outbound link clicks; freshness alone does not establish performance.'}


def audit(state, manifest, expected_control_id, export_paths=None, export_root=None):
    """Never mutates arguments. Paths map record IDs to existing local final exports."""
    validate_state(state, expected_control_id)
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or not isinstance(manifest.get('pins'), list)
            or any(not isinstance(pin, dict) for pin in manifest['pins'])):
        raise ValueError('invalid publisher manifest schema')
    export_paths = export_paths or {}
    if not isinstance(export_paths, dict):
        raise ValueError('export map must be an object')
    records = state['records']
    if any(key != record.get('record_id') for key, record in records.items()):
        raise ValueError('private record key/identity mismatch')
    ids = [pin.get('id') for pin in manifest['pins']]
    if any(not isinstance(pid, str) or not pid for pid in ids) or len(set(ids)) != len(ids):
        raise ValueError('duplicate or invalid public record identity')
    public = {p['id']: p for p in manifest['pins']}
    findings = []
    for pid, pin in public.items():
        if pid not in records:
            findings.append({'record_id': pid, 'code': 'public_record_missing_private_control'})
        if (pin.get('editorial_approved') is not True or
            (pin.get('status') not in ('sent', 'scheduled') and pin.get('visual_qa_approved') is not True)):
            findings.append({'record_id': pid, 'code': 'public_record_not_exactly_approved'})
    seen_ideas, seen_posts = {}, {}
    reports = []
    for pid, record in records.items():
        issues = []
        approval = record.get('approval', {})
        snapshot = approval.get('immutable_snapshot')
        status = approval.get('status')
        copy_check = 'not_approved'
        if status == EXACT_APPROVAL:
            try:
                copy_check = 'match' if copy_digest(snapshot) == approval.get('copy_sha256') else 'mismatch'
            except ValueError:
                copy_check = 'incomplete_snapshot'
            if copy_check != 'match':
                issues.append('exact_copy_approval_hash_' + copy_check)
            if (not isinstance(approval.get('reference'), str) or not approval['reference'].strip()
                    or not re.fullmatch(r'[a-f0-9]{64}', str(approval.get('asset_sha256', '')))):
                issues.append('approval_reference_or_image_hash_missing')
            if isinstance(snapshot, dict) and snapshot.get('asset_sha256') != approval.get('asset_sha256'):
                issues.append('approval_image_hash_disagrees_with_snapshot')
        elif status in LEGACY_APPROVALS:
            copy_check = 'preserved_legacy_hash_basis_not_assumed'
        else:
            issues.append('exact_approval_required')
        for field, seen, source in [('buffer_idea_id', seen_ideas, record),
                                    ('buffer_post_id', seen_posts, record.get('publication', {}))]:
            value = source.get(field)
            if value:
                if value in seen:
                    issues.append('duplicate_' + field + '_with_' + seen[value])
                seen[value] = pid
        publication = record.get('publication', {})
        completed = publication.get('status') in ('sent', 'scheduled', 'sending')
        if not completed:
            if not record.get('source', {}).get('canonical_url'):
                issues.append('specific_source_missing')
            if not record.get('search_intent'):
                issues.append('canonical_search_intent_missing')
        if pid in public and status == EXACT_APPROVAL and isinstance(snapshot, dict):
            changed = [key for key in COPY_FIELDS if public[pid].get(key) != snapshot.get(key)]
            if changed:
                issues.append('public_copy_differs_from_exact_approval:' + ','.join(changed))
            if public[pid].get('asset_sha256') != approval.get('asset_sha256'):
                issues.append('public_image_hash_differs_from_exact_approval')
        export = {'status': 'not_supplied_locally', 'expected_sha256': approval.get('asset_sha256')}
        if pid in export_paths:
            path = safe_export_path(export_root, export_paths[pid])
            if path.is_symlink() or not path.is_file():
                export['status'] = 'missing_or_symlink'
                issues.append('local_export_unavailable')
            elif path.stat().st_size > 5_000_000 or path.stat().st_size == 0:
                export['status'] = 'invalid_size'
                issues.append('local_export_invalid_size')
            else:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    body = stream.read(5_000_001)
                if not 1 <= len(body) <= 5_000_000:
                    raise ValueError('export size changed or exceeds limit')
                export.update(observed_sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
                if export['observed_sha256'] == export['expected_sha256']:
                    export['status'] = 'exact_approved_bytes_match'
                else:
                    export['status'] = 'not_exact_approved_bytes'
                    issues.append('local_export_requires_new_exact_review')
        handoff = record.get('creative_handoff', {})
        proposal_id = handoff.get('copy_proposal_id')
        proposals = [p for p in record.get('private_draft_proposals', []) if p.get('proposal_id') == proposal_id] if proposal_id else []
        reports.append({'record_id': pid, 'publication_status': publication.get('status'),
            'approval_status': status, 'copy_hash_check': copy_check,
            'export': export, 'public_manifest_present': pid in public,
            'manual_handoff': {'required': not completed and status not in LEGACY_APPROVALS | {EXACT_APPROVAL},
                'copy_proposal_id': proposal_id, 'proposal_unambiguously_resolved': len(proposals) == 1,
                'expected_export_name': handoff.get('expected_export_name'),
                'target_design_id': handoff.get('target_design_id'),
                'reference_design_must_be_verified_as_target': True,
                'final_export_reapproval_required_if_bytes_change': True},
            'live_gates_not_checked': [] if completed else list(PENDING_LIVE_GATES),
            'issues': issues, 'existing_record_blockers': list(record.get('blockers', [])),
            'metrics': metrics_readiness(record)})
    return {'mode': 'private_offline_contract_audit', 'state_revision': state.get('revision'),
        'dispatch_authorized': False, 'network_requests': 0, 'state_writes': 0,
        'private_record_count': len(records), 'public_record_count': len(public),
        'unreconciled_public_records': sorted(set(public) - set(records)),
        'findings': findings, 'records': reports,
        'next_execution': 'Same existing orchestrator only; re-read live snapshots before changes. Do not upload this private report to the public repository.'}


# These are instrumentation-only stages, never editorial approval authority.
EVENT_STAGES = {
    'source_intake', 'source_selection', 'search_translation', 'copy_draft',
    'creative_handoff', 'visual_qa', 'destination_qa', 'approved_asset_handoff',
    'publisher_dispatch', 'publication_verify', 'measure', 'learn',
}
EVENT_STATUSES = {'observed', 'held', 'manual_input_required', 'not_attempted'}
ERROR_CODES = {None, 'access_denied', 'source_unavailable', 'awaiting_creative',
               'hash_mismatch', 'schema_mismatch', 'unknown_outcome', 'image_transfer_stall', 'provider_unavailable'}
FORBID_RETRY_CODES = {'access_denied', 'unknown_outcome', 'image_transfer_stall'}


def source_intake_plan(state, source_items):
    """Bounded private-draft proposals only; never invent publication or approval."""
    if not isinstance(source_items, list):
        raise ValueError('source items must be a list')
    existing = {}
    for rid, record in state['records'].items():
        source = record.get('source', {}).get('canonical_url')
        intent = record.get('search_intent')
        if source and intent:
            existing[(source, intent)] = rid
        for proposal in record.get('private_draft_proposals', []):
            key = (proposal.get('evidence_url') or proposal.get('destination_url'),
                   proposal.get('search_intent'))
            if all(key):
                existing[key] = rid
    results, drafts = [], 0
    for source in source_items[:MAX_SOURCE_ITEMS]:
        if not isinstance(source, dict):
            raise ValueError('invalid source observation')
        url, intent, variant = (source.get(k) for k in ('canonical_url', 'search_intent', 'variant_id'))
        rid = intake_record_id(url, intent, variant)
        key = (url, intent)
        if source.get('restriction') in FORBID_RETRY_CODES:
            results.append({'record_id': existing.get(key, rid), 'action': 'hold_source_restriction',
                            'retry_allowed': False, 'reuse_existing_record': key in existing})
        elif key in existing or rid in state['records']:
            results.append({'record_id': existing.get(key, rid), 'action': 'reuse_existing_record',
                            'new_record_allowed': False})
        elif (source.get('source_status') not in {'published_verified', 'approved_private_source'}
              or not source.get('evidence_ref')
              or not re.fullmatch(r'[a-f0-9]{64}', str(source.get('content_sha256', '')))):
            results.append({'candidate_record_id': rid, 'action': 'verify_source_evidence',
                            'new_record_allowed': False})
        elif drafts >= MAX_DRAFTS:
            results.append({'candidate_record_id': rid, 'action': 'defer_to_normal_intake_pass',
                            'new_record_allowed': False})
        else:
            drafts += 1
            existing[key] = rid  # Deduplicate later inputs within this same pass.
            results.append({'candidate_record_id': rid, 'action': 'prepare_private_draft',
                'source_sha256': source['content_sha256'], 'evidence_ref': source['evidence_ref'],
                'publication_approval': False,
                'publisher_identity_mapping_required': True})
    return {'inspected': min(len(source_items), MAX_SOURCE_ITEMS), 'drafts_planned': drafts,
            'source_items_deferred': max(0, len(source_items) - MAX_SOURCE_ITEMS), 'items': results}


def reconcile_plan(state, ledger, history, observed_at):
    """Compare supplied read-only snapshots; never modify the publisher ledger."""
    report = {'read_only_comparison_complete': False, 'actions': [], 'holds': []}
    now = timestamp(observed_at)
    if now is None:
        raise ValueError('aware observation timestamp required')
    if not isinstance(ledger, dict) or not isinstance(history, dict):
        report['holds'].append('fresh_ledger_and_full_channel_history_required')
        return report
    origin = state.get('source_of_truth', {})
    provenance = ledger.get('snapshot')
    if (not isinstance(provenance, dict) or timestamp(provenance.get('observed_at')) is None
            or not 0 <= (now - timestamp(provenance['observed_at'])).total_seconds() <= 900
            or not origin.get('publisher_repository') or not origin.get('publisher_ledger_branch')
            or provenance.get('repository') != origin['publisher_repository']
            or provenance.get('branch') != origin['publisher_ledger_branch']
            or not re.fullmatch(r'[a-f0-9]{40}', str(provenance.get('blob_sha', '')))
            or not isinstance(ledger.get('content'), dict)):
        report['holds'].append('ledger_identity_freshness_or_hash_unverified')
        return report
    ledger = ledger['content']
    expected = origin.get('buffer_channel_id')
    then = timestamp(history.get('observed_at'))
    posts = history.get('posts')
    if (not expected or history.get('channel_id') != expected or history.get('complete') is not True
            or then is None or not 0 <= (now - then).total_seconds() <= 900
            or not isinstance(posts, list) or len(posts) > 2000):
        report['holds'].append('channel_history_incomplete_wrong_identity_or_stale')
        return report
    if (type(ledger.get('schema_version')) is not int or ledger['schema_version'] != 1
            or not isinstance(ledger.get('records'), dict) or 'lock' not in ledger):
        report['holds'].append('ledger_schema_invalid')
        return report
    known = {}
    for post in posts:
        if (not isinstance(post, dict) or not post.get('id') or post['id'] in known
                or post.get('channelId') != expected
                or post.get('status') not in {'draft', 'error', 'needs_approval', 'scheduled', 'sending', 'sent'}):
            report['holds'].append('channel_history_invalid_post')
            return report
        if post['status'] == 'sent':
            sent = timestamp(post.get('sentAt'))
            if sent is None or sent > now:
                report['holds'].append('invalid_sent_timestamp:' + post['id'])
                return report
        elif post['status'] in {'scheduled', 'sending'}:
            due = timestamp(post.get('dueAt'))
            if due is None or due <= now:
                report['holds'].append('pending_publication_uncertain:' + post['id'])
                return report
        known[post['id']] = post
    if ledger['lock'] is not None:
        report['holds'].append('durable_lock_present_do_not_create_or_clear')
    for rid, row in ledger['records'].items():
        if not isinstance(row, dict) or row.get('state') != 'recorded':
            report['holds'].append('unresolved_intent:' + rid)
            continue
        post = known.get(row.get('buffer_post_id'))
        if post is None:
            report['holds'].append('durable_post_missing_from_live_history:' + rid)
            continue
        if (row.get('status'), row.get('sent_at'), row.get('due_at')) != (
                post.get('status'), post.get('sentAt'), post.get('dueAt')):
            report['actions'].append({'record_id': rid, 'action': 'publisher_reconciliation_required',
                'buffer_post_id': post['id'], 'observed_status': post['status'],
                'create_allowed': False, 'direct_ledger_write_allowed': False})
    for rid, record in state['records'].items():
        current = record.get('publication', {})
        post = known.get(current.get('buffer_post_id'))
        if post and (current.get('status'), current.get('sent_at') or current.get('published_at')) != (
                post.get('status'), post.get('sentAt')):
            report['actions'].append({'record_id': rid, 'action': 'refresh_private_publication_observation',
                'buffer_post_id': post['id'], 'observed_status': post['status'], 'create_allowed': False})
    mapped_posts = {row.get('buffer_post_id') for row in ledger['records'].values() if isinstance(row, dict)}
    mapped_posts.update(row.get('publication', {}).get('buffer_post_id') for row in state['records'].values())
    report['external_channel_post_ids'] = sorted(set(known) - mapped_posts)
    report['manual_posts_policy'] = 'Keep external posts in complete history; publisher includes their dates. Do not adopt, change or repost them.'
    report['dispatch_eligibility_established'] = False
    report['read_only_comparison_complete'] = not report['holds']
    return report


def stage_events(state, observations, run_id, observed_at):
    """Instrument only explicit observations, not actions merely planned by this tool."""
    if (not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id)
            or timestamp(observed_at) is None or not isinstance(observations, list)
            or len(observations) > 1000):
        raise ValueError('invalid instrumentation run')
    events, seen = [], set()
    allowed = {'record_id', 'stage', 'status', 'attempted', 'error_code', 'evidence_ref'}
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) - allowed:
            raise ValueError('unrecognized stage observation fields')
        rid, stage = observation.get('record_id'), observation.get('stage')
        if (rid not in state['records'] or stage not in EVENT_STAGES
                or observation.get('status') not in EVENT_STATUSES
                or type(observation.get('attempted')) is not bool
                or observation.get('error_code') not in ERROR_CODES
                or not isinstance(observation.get('evidence_ref'), str)
                or not 1 <= len(observation['evidence_ref']) <= 500):
            raise ValueError('invalid stage observation')
        if (rid, stage) in seen:
            raise ValueError('one stage observation per record per run required')
        seen.add((rid, stage))
        if observation['status'] == 'not_attempted' and observation['attempted']:
            raise ValueError('not_attempted conflicts with attempted')
        event = dict(observation, error_code=observation.get('error_code'), run_id=run_id, observed_at=observed_at)
        event['next_action'] = ('hold_no_retry_until_resolved'
            if event.get('error_code') in FORBID_RETRY_CODES
            else 'continue_independent_stages')
        event['event_id'] = digest(event)
        events.append(event)
    return events


def propose_instrumentation(state, events, expected_state_sha256, expected_control_id):
    """Pure merge for single writer review. Does not save a file or change approvals.

    The caller must first re-read the same live Drive file and validate its hash.
    No Drive compare-and-swap exists: serialization remains an external requirement.
    """
    validate_state(state, expected_control_id)
    if digest(state) != expected_state_sha256:
        raise ValueError('state changed; reload and rebuild, never blind overwrite')
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError('invalid instrumentation event batch')
    updated = copy.deepcopy(state)
    changed_times = []
    for event in events:
        expected = stage_events(state, [{k: event[k] for k in (
            'record_id', 'stage', 'status', 'attempted', 'evidence_ref')} | {
            'error_code': event.get('error_code')}], event['run_id'], event['observed_at'])[0]
        if event != expected:
            raise ValueError('event content or digest mismatch')
        record = updated['records'][event['record_id']]
        stats = record.setdefault('pipeline_observations', {}).setdefault(event['stage'], {
            'attempts': 0, 'events': 0})
        if (type(stats.get('attempts')) is not int or stats['attempts'] < 0
                or type(stats.get('events')) is not int or stats['events'] < 0):
            raise ValueError('invalid instrumentation counters')
        if stats['attempts'] > stats['events']:
            raise ValueError('instrumentation attempts exceed events')
        if stats['events']:
            previous_event = stats.get('last_event')
            if (not isinstance(previous_event, dict)
                    or timestamp(stats.get('last_observed_at')) is None
                    or stats.get('last_event_id') != previous_event.get('event_id')
                    or stats.get('last_run_id') != previous_event.get('run_id')
                    or stats.get('last_observed_at') != previous_event.get('observed_at')
                    or previous_event.get('record_id') != event['record_id']
                    or previous_event.get('stage') != event['stage']):
                raise ValueError('corrupt populated instrumentation watermark')
            previous_payload = {key: value for key, value in previous_event.items() if key != 'event_id'}
            if digest(previous_payload) != previous_event['event_id']:
                raise ValueError('corrupt previous event digest')
        elif any(key in stats for key in ('last_event', 'last_event_id', 'last_run_id', 'last_observed_at')):
            raise ValueError('unexpected watermark on empty instrumentation')
        active = stats.get('active_restrictions', {})
        if (not isinstance(active, dict) or any(code not in FORBID_RETRY_CODES or not isinstance(row, dict)
                for code, row in active.items())):
            raise ValueError('invalid sticky stage restrictions')
        if event['event_id'] == stats.get('last_event_id'):
            if event != stats.get('last_event'):
                raise ValueError('event ID conflict')
            continue
        # One bounded watermark per stage avoids an unbounded journal. Old or
        # conflicting replay fails rather than incrementing a counter again.
        previous = timestamp(stats.get('last_observed_at'))
        current = timestamp(event['observed_at'])
        if (previous is not None and current <= previous) or event['run_id'] == stats.get('last_run_id'):
            raise ValueError('stale or conflicting stage event; reload and review')
        if event.get('error_code') in FORBID_RETRY_CODES:
            stats.setdefault('active_restrictions', {})[event['error_code']] = {
                'event_id': event['event_id'], 'run_id': event['run_id'],
                'observed_at': event['observed_at'], 'evidence_ref': event['evidence_ref']}
        # Null/success/not-attempted observations NEVER clear a denial or stall.
        # Clearing a sticky restriction needs a separate authorized, evidence-backed resolution.
        stats['attempts'] += int(event['attempted'])
        stats['events'] += 1
        stats.update(last_event_id=event['event_id'], last_run_id=event['run_id'],
            last_observed_at=event['observed_at'], status=event['status'],
            error_code=event.get('error_code'), evidence_ref=event['evidence_ref'],
            next_action=event['next_action'], last_event=copy.deepcopy(event))
        changed_times.append(current)
    if changed_times:
        updated['revision'] += 1
        prior_update = timestamp(updated.get('updated_at'))
        if prior_update is not None:
            changed_times.append(prior_update)
        updated['updated_at'] = max(changed_times).astimezone(timezone.utc).isoformat()
    return updated


def restriction_map(state, observations, restrictions):
    result = {}
    for rid, record in state['records'].items():
        for stage, row in record.get('pipeline_observations', {}).items():
            active = row.get('active_restrictions', {})
            if not isinstance(active, dict) or any(code not in FORBID_RETRY_CODES for code in active):
                raise ValueError('invalid sticky stage restrictions')
            if active:
                result[(rid, stage)] = sorted(active)[0]
            if row.get('error_code') in FORBID_RETRY_CODES:
                result[(rid, stage)] = row['error_code']
        # Legacy unscoped holds conservatively block external stage recommendations.
        for code in record.get('blockers', []):
            if code in FORBID_RETRY_CODES:
                result[(rid, '*')] = code
    for row in (observations or []) + (restrictions or []):
        if not isinstance(row, dict) or row.get('record_id') not in state['records']:
            raise ValueError('invalid restriction record')
        stage, code = row.get('stage'), row.get('error_code')
        if stage not in EVENT_STAGES or code not in ERROR_CODES:
            raise ValueError('invalid restriction stage or code')
        if code in FORBID_RETRY_CODES:
            result[(row['record_id'], stage)] = code
    return result


def integration_plan(state, manifest, expected_control_id, run_id, observed_at,
                     exports=None, export_root=None, source_items=None,
                     ledger=None, history=None, observations=None, restrictions=None):
    report = audit(state, manifest, expected_control_id, exports, export_root)
    report.update(run_id=run_id, observed_at=observed_at, base_state_sha256=digest(state),
                  public_manifest_sha256=digest(manifest))
    holds = restriction_map(state, observations, restrictions)
    report['source_intake'] = source_intake_plan(state, source_items or [])
    for item in report['source_intake']['items']:
        rid = item.get('record_id') or item.get('candidate_record_id')
        code = holds.get((rid, 'source_intake')) or holds.get((rid, '*'))
        if code:
            item.update(action='hold_source_restriction', retry_allowed=False, restriction=code)
    report['publication_reconciliation'] = reconcile_plan(state, ledger, history, observed_at)
    report['stage_events'] = stage_events(state, observations or [], run_id, observed_at)
    actions = []
    for record in report['records']:
        rid = record['record_id']
        for issue in record['issues']:
            actions.append({'record_id': rid, 'action': 'resolve_private_contract_issue', 'issue': issue})
        if record['manual_handoff']['required']:
            actions.append({'record_id': rid, 'action': 'prepare_private_manual_handoff_request', 'stage': 'creative_handoff',
                'service_action_authorized': False,
                'proposal_id': record['manual_handoff']['copy_proposal_id'],
                'required_inputs': ['specific_source', 'selected_copy_proposal', 'final_export_bytes',
                                    'exact_visual_and_editorial_review'], 'public_upload_allowed': False})
        elif record['approval_status'] == EXACT_APPROVAL and record['export']['status'] == 'not_supplied_locally':
            actions.append({'record_id': rid, 'action': 'retrieve_existing_exact_export', 'stage': 'approved_asset_handoff',
                'expected_sha256': record['export']['expected_sha256'], 'regeneration_authorized': False})
        if record['metrics']['freshness'] in {'unknown', 'stale_prepublication'}:
            actions.append({'record_id': rid, 'action': 'refresh_available_aggregate_metrics', 'stage': 'measure',
                'missing_value_policy': 'null_unknown_not_zero', 'outbound_clicks_are_pin_clicks': False})
    for action in actions:
        rid, stage = action['record_id'], action.get('stage')
        code = holds.get((rid, stage)) or holds.get((rid, '*'))
        if stage and code:
            action.update(action='hold_stage_no_retry', restriction=code, retry_allowed=False,
                next_action='Resolve existing denial or uncertain transfer through the authorized path; continue independent private work.')
    report['actions'] = actions
    report['active_stage_holds'] = [{'record_id': rid, 'stage': stage, 'error_code': code}
        for (rid, stage), code in sorted(holds.items())]
    report['deployment_boundary'] = {'single_existing_orchestrator': True, 'new_scheduler': False,
        'approval_changes': False, 'publisher_changes': False, 'direct_buffer_create': False,
        'state_proposal_requires_fresh_read_and_single_writer': True}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Private JSON envelope; no network reads')
    parser.add_argument('--export-root', help='Explicit private directory of staged exact exports')
    args = parser.parse_args()
    data = read_json(args.input)
    report = integration_plan(data['state'], data['manifest'], data['expected_control_id'],
        data['run_id'], data['observed_at'], data.get('exports'), args.export_root,
        data.get('source_items'), data.get('ledger'), data.get('history'), data.get('observations'), data.get('restrictions'))
    # stdout is private process output. Never attach it to public Actions artifacts/logs.
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, TypeError, KeyError, OSError):
        print(json.dumps({'mode': 'private_offline_contract_audit', 'dispatch_authorized': False,
                          'error': 'invalid_snapshot_or_input; no external action performed'}))
        raise SystemExit(2)
