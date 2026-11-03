# One Pin per Eastern calendar day

Local proposal against main `407455eab6cf9942d1fe44925369491909bf5a38`.
The owner approved one Pin per day for remaining inventory on 2026-11-03. This
patch does not expand the KG destination/board allowlist, approve new creative,
change Buffer's queue settings, create a scheduler, or modify campaign/history.

## Date selection

Before every new create, read the current channel timezone and posting schedule,
all channel posts, and the durable ledger. Require `America/New_York`. Count
remote sent timestamps and pending due timestamps in that zone, including manual
posts. Preserve every durable reservation and historical outcome date. A pending
post missing from live history, unresolved intent, malformed/missing timestamp,
future sent timestamp, overdue scheduled/sending post, or sending post dated
outside today blocks new creation.
Past sent dates are valid history. Multiple historical posts on the same date
remain unchanged and consume that date once; no retroactive edits are attempted.

Use the earliest still-future existing Buffer slot on the first unoccupied local
date, at least five minutes ahead. That is an existing-slot selection policy,
not an invented preferred hour. Paused days are skipped. No active slots or no
free slot within 366 days blocks. At review the channel has four daily slots;
only one is selected per date. Calendar-day iteration uses `zoneinfo`, not
24-hour UTC arithmetic. Nonexistent DST times are skipped and repeated times use
the first occurrence. Once a date is reserved, it is never automatically reused.

## API contract and durable safety

Official source verified 2026-11-03:
https://developers.buffer.com/reference
`CreatePostInput` supports `dueAt` as a DateTime and `mode` as ShareMode.
The mode value is `customScheduled`; `schedulingType` stays `automatic`, which
selects automatic delivery rather than notification delivery. Channel fields
are `timezone` and `postingSchedule { day times paused }`; days are mon–sun and
times are HH:MM. No connector writes are used.

The existing candidate payload and `payload_sha256` remain unchanged for legacy
identity/deduplication, including original Pins 2/3. New intents additionally
persist `schedule` (policy, timezone, local_date, due_at) and
`scheduled_payload_sha256` for the exact effective custom-scheduled request.
Refresh/reconcile reconstruct that exact original request from the reservation,
never choose a new dueAt, and verify its hash. Legacy records without a schedule
continue using their original hash. Existing records are not migrated.

The global CAS lock and date reservation are durable before the sole create
attempt. After locking, re-read remote posts and channel settings; a collision,
changed channel, or elapsed safety lead retains the intent for review. Verify
returned content, post ID, status and exact dueAt before clearing the lock. A
sent readback must also match the reserved local day. Unknown create outcomes
remain locked. Reconcile never creates. The ten-post queue cap and all approval,
asset, offer, destination, idempotency and canonical-main gates remain intact.

The lock controls this publisher only. An external/manual writer can race after
the final check; Buffer has no documented transactional channel/day reservation.
Late delivery can also fall on another day. This enforces noncolliding planned
publication dates at creation, not an unconditional guarantee against Buffer
outages or external writers. Do not use another post creator. Date drift blocks
refresh/reconcile and needs review, without silently rescheduling or deleting.

`check` now requires read access to the durable ledger and reports the proposed
date/time without claiming it. `audit` remains a content/queue audit and explicitly
reports `content_checks_passed_schedule_not_checked`, not scheduling readiness.
Offline `plan` still uses no network or credentials. No workflow timer changes.

## Local validation

Run `python3 -m unittest discover -s tests -v` and
`python3 scripts/approved_pin_publisher.py --mode plan`.
Tests use fake API and CAS ledger. Coverage includes date collisions, sent/sending,
stale/missing timestamps, post-lock races, repeat/concurrent claims, ambiguous
writes and reconcile, DST boundaries/gaps/folds, legacy hashes/records, original
reservations, and midnight Eastern/UTC differences. No live create or dispatch
has been performed for this proposal; runtime integration remains untested.
