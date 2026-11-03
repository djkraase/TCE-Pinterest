# Approved-Pin publisher — reviewed implementation, not enabled

Baseline: `a1998f1c901bf411b793c358e72838c1b98d87b6`, checked 2026-11-03 UTC.
No existing manifest, image, README, audit workflow, validation workflow, or preflight was changed. Implementation is approved for commit and read-only audit only; live publishing remains disabled.

## Verified current state

- Buffer organization `6ad44c983788a9f13eb0c4cf`; Pinterest channel `6ae8780be29c69a171371a76`, connected/unlocked/unpaused.
- MVP board allowlist is only Workflow Design & Process Improvement, service ID `1083467691545125927`.
- Only destination allowed is `https://kraase.gumroad.com/l/kg-diagnostic`.
- First Pin `pin-01-handoff`, Buffer `6ae9060f3b6f22c15ed12663`, is sent, with sentAt `2026-11-02T22:15:04.050Z` and Pinterest URL `https://www.pinterest.com/pin/1083467622891676708`. Its ID and creative SHA are excluded from new creation.
- Current repo has **3 manifest records**, not all ten campaign assets: one sent and two unapproved/asset-pending. No new Pin is ready. No approval was elevated.

## Commands and defaults

`python3 scripts/approved_pin_publisher.py` is an **offline plan**, does not read credentials, contact APIs, write a ledger, upload images, or publish.

- `--mode audit`: meaningful read-only account/channel/board check, complete remote queue and capacity audit, first-Pin publication status check, and per-record readiness/blockers. Works with no ready records. This is the recommended first remote dry-run.
- `--mode check --pin-id ID`: read-only live account, board, complete post-history, cap, hosted-image-byte and destination checks; no durable writes.
- `--mode execute --pin-id ID`: at most one `createPost`, only with explicit enable variable, canonical repository/main, valid approvals, and durable state.
- `--mode reconcile --pin-id ID`: durable ledger update only, **never creates**. Requires exact single remote matching post and image/alt verification; no match keeps intent locked.
- `--mode status --pin-id ID`: read-only post outcome using manifest or ledger Buffer ID. Reports scheduled/error/draft/sending/sent distinctly. “publication_verified” means Buffer reports sentAt plus a valid Pinterest publication URL, **not independent Pinterest-page retrieval**. Purchases/subscriptions remain UNKNOWN without separate attribution evidence.

No auto retry of mutations. No redirect following (including credential requests). Network timeout 20 seconds, workflow timeout 10 minutes, image maximum 5 MB, history maximum 20 × 100 posts, fail closed on incomplete history. Python standard library only; checkout action SHA pinned, no dependency install.

## Required record extension (existing v1 records remain valid for audit)

For a **newly approved** record retain `id`, `source_id`, `search_intent`, `asset`, `destination_url`, `board_service_id`; set `status: approved`, `editorial_approved: true`, and add:

- `visual_qa_approved: true` — actual visual review completed, not inferred from file presence.
- `title` (1–100 chars), `text` (1–500), `alt_text` (1–500): exact approved publication copy.
- `asset_sha256`: SHA-256 of exact approved public image; remote bytes must match local bytes.
- `approval_ref`: non-private review identifier (not private source content or credentials).

Do not copy private source documents into this public repo. No placeholders or draft copy become approvals. The `status: draft` audit eligibility is deliberately stricter here: publisher requires explicit `approved`. No Canva automation needed for an already approved, ready image. It never uploads or transforms creative.

## Durable state and crash behavior

A dedicated **non-Pages** branch `pinterest-state` holds `publisher-ledger.json`. This branch must be deliberately initialized once after approval; code does not create it on missing/404 response. Minimum initial content:

```json
{"schema_version":1,"lock":null,"records":{}}
```

Ledger has only public-safe Pin IDs, hashes, status, Buffer IDs, timestamps and public publication URLs. No keys, raw API responses, emails, full text, or private source material. Preserve history; never reset/delete the state branch. GitHub workflow `contents:write` is scoped at job level but GitHub does not restrict that token to one branch; code only addresses the fixed ledger path/branch.

Before create: global lock + intent atomically persisted with GitHub Contents SHA compare-and-swap. Two runners with same old SHA cannot both claim; additionally workflow concurrency serializes all modes. Before mutation, recheck remote cap/dedup. Final state persists verified Buffer ID and clears the lock. Cross-ID payload, creative and source-intent hashes prevent renamed-ID resubmissions even if Buffer history is later deleted.

If timeout/crash occurs after claim, including before request or after successful remote creation, **intent stays locked**. Next run deduplicates remote first, does not blindly retry. Explicit reconcile can attach the unique verified post without creating another. If no remote match, multiple matches, changed content, rejected mutation, or outcome unavailable: stop and investigate. Never automatically expire the lock; manually clearing an uncertain intent could duplicate a real Pin. This deliberately trades liveness for at-most-one request. Buffer does not document a create idempotency key; exactly-once delivery cannot be honestly promised.

The cap check counts scheduled+sending on the allowed channel; Buffer's own plan enforcement is final protection against a simultaneous human/external writer. The ledger lock coordinates this publisher only. Existing orchestrator must route all publisher writes through this workflow rather than also create Pins through the connector. Avoid independent manual queue writes while executing a batch.

## Integration and activation gates (NOT performed)

1. Obtain permission for code publication and credential replacement. Minimum Buffer scopes: accountRead (`account:read`), postsRead (`posts:read`), postsWrite (`posts:write`). No accountWrite, ideas, insights, snippets. The current account-read-only key cannot publish. Buffer key has account-wide access; allowlisting is enforced by code, not token scope.
2. Review/commit these files and run offline tests/plan on main. No automatic publisher cron, push trigger, or workflow run added.
3. Confirm original “TCE Pinterest Queue Replenishment” task identity and edit its existing orchestration, rather than creating another scheduled task. Existing repo audit cron stays read-only and unchanged.
4. Separately authorize initialization of state branch; configure `pinterest-publishing` GitHub environment with required human approval. An environment name alone does not enforce reviewer approval: confirm protection before enabling. Configure `PINTEREST_PUBLISHER_ENABLED=true` **only after explicit live-publishing authorization**. It is absent/false by default.
5. Independently confirm destination purchasability and approved offer/price before live enablement; the automated HTTP 200/HTML check establishes retrieval only, not checkout or sales readiness. Populate one genuinely approved, full-size QA-passed, public image/copy record. Run `check` with read scope. Review output. First write run requires further authorization; plan/check success is not authorization.
6. Existing orchestrator dispatches one chosen record at a time; follow with `status`, report errors/drafts distinctly, and later verify sentAt/publication ID. Do not treat queued as sent. Continue outcome tracking at the existing scheduler cadence, with no new scheduler.

Schema/live reads were verified via connected Buffer tools, but **new direct GraphQL operations and GitHub ledger writes have not been integration-tested with real keys**. Activation needs authorized read-only direct query check first, then controlled live test with a newly approved Pin. Current tests are fake API/ledger tests, not evidence of real publication.

## Official API sources reviewed 2026-11-03

- https://developers.buffer.com/reference — CreatePostInput, ImageAssetInput → metadata → altText, PinterestPostMetadataInput, post status, post externalLink/sentAt, pagination and channel metadata.
- https://developers.buffer.com/examples/create-image-post.html — image URL input and PostActionSuccess/MutationError.
- https://developers.buffer.com/examples/get-paginated-posts.html — complete history pagination.
- https://developers.buffer.com/guides/authentication.html — Bearer header, scope definitions, account-wide key boundary.
- https://support.buffer.com/en-us/articles/how-to-create-your-buffer-api-key-ShIgYVwM6j — personal-key permissions and free-plan single-key limit.
