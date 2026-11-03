# Approved-Pin publisher — approved-inventory automation

Baseline: `a1998f1c901bf411b793c358e72838c1b98d87b6`, checked 2026-11-03 UTC.
Original first-Pin asset, README, validation workflow and read-only preflight are preserved. The two next approved assets and manifest records are added; the existing audit cron delegates sequential replenishment without a second scheduler. The owner approved the two-Pin launch, durable job-state writes, and ongoing scheduling from individually approved Pinterest inventory on 2026-11-03. Activation still requires successful Pages/read-only checks and the repository enable switch.

## Verified current state

- Buffer organization `6ad44c983788a9f13eb0c4cf`; Pinterest channel `6ae8780be29c69a171371a76`, connected/unlocked/unpaused.
- MVP board allowlist is only Workflow Design & Process Improvement, service ID `1083467691545125927`.
- Only canonical destination allowed is `https://kraase.gumroad.com/l/kg-diagnostic`. New Pins add deterministic nonprivate attribution parameters: `utm_source=pinterest&utm_medium=organic_social&utm_campaign=kg_diagnostic&utm_content=PIN_ID`. The manifest keeps the canonical URL; code derives the exact tracking URL. It must return the same available product without redirects. UTM instrumentation is not evidence of attributed revenue.
- First Pin `pin-01-handoff`, Buffer `6ae9060f3b6f22c15ed12663`, is sent, with sentAt `2026-11-02T22:15:04.050Z` and Pinterest URL `https://www.pinterest.com/pin/1083467622891676708`. Its ID and creative SHA are excluded from new creation.
- Current repo has **3 manifest records**, not all ten campaign assets: one sent and two individually approved for the bounded launch. Their cropped/resized existing artwork passed visual QA; exact approved titles, descriptions, alt text and SHA-256 hashes are recorded. Other campaign drafts are not imported or approved.

## Commands and defaults

`python3 scripts/approved_pin_publisher.py` is an **offline plan**, does not read credentials, contact APIs, write a ledger, upload images, or publish.

- `--mode audit`: meaningful read-only account/channel/board check, complete remote queue and capacity audit, first-Pin publication status check, and per-record readiness/blockers. Works with no ready records. This is the recommended first remote dry-run.
- `--mode check --pin-id ID`: read-only live account, board, complete post-history, cap, hosted-image-byte and destination checks; no durable writes.
- `--mode replenish`: sequentially processes only individually approved records, checks cap/dedup before each create, and stops the batch on any uncertain write. No parallel matrix.
- `--mode execute --pin-id ID`: at most one `createPost`, only with explicit enable variable, canonical repository/main, valid approvals, and durable state.
- `--mode reconcile --pin-id ID`: durable ledger update only, **never creates**. Requires exact single remote matching post and image/alt verification; no match keeps intent locked.
- `--mode status --pin-id ID`: read-only post outcome using manifest or ledger Buffer ID. Reports scheduled/error/draft/sending/sent distinctly. “publication_verified” means Buffer reports sentAt plus a valid Pinterest publication URL, **not independent Pinterest-page retrieval**. Purchases/subscriptions remain UNKNOWN without separate attribution evidence.

No auto retry of mutations. No redirect following (including credential requests). The destination gate parses only public product/offer fields from the current product page: exact known product and URL, published and not compliance-blocked, available inventory, no waitlist/preorder, USD $39 minimum pay-what-you-want offer, corroborated by JSON-LD InStock. Missing/changed schema blocks. Full page/props are never logged or stored by the publisher; they can contain runtime tokens. This is offer-availability verification, not a checkout/payment test.

Network timeout 20 seconds, workflow timeout 10 minutes, image maximum 5 MB, history maximum 20 × 100 posts, fail closed on incomplete history. Python standard library only; checkout action SHA pinned, no dependency install.

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

## Integration and activation gates

1. Obtain permission for code publication and credential replacement. Minimum Buffer scopes: accountRead (`account:read`), postsRead (`posts:read`), postsWrite (`posts:write`). No accountWrite, ideas, insights, snippets. The current account-read-only key cannot publish. Buffer key has account-wide access; allowlisting is enforced by code, not token scope.
2. Review/commit these files and run offline tests/plan on main. No second cron or publishing-on-push trigger is added. The existing audit schedule remains `15 13 * * *` (13:15 UTC, 08:15 EST / 09:15 EDT). Only its scheduled event, after successful audit and with the enable switch on, calls the publisher using explicit BUFFER_API_KEY secret mapping. Manual audit and pushes do not schedule Pins.
3. Authorized task lookup returned zero ChatGPT automations; the earlier claimed 09:00 Eastern task is unverified. Reuse the verified existing repository audit cron instead. Do not add another ChatGPT task or GitHub cron. The audit job remains read-only; its dependent scheduled-only publisher job receives contents:write.
4. Durable state initialization is authorized and the `pinterest-state` branch is initialized once. Configure `pinterest-publishing` with Selected branches and tags allowing branch `main` only (not a same-named tag), no per-run required reviewers or wait timer. The owner authorized ongoing approved-inventory scheduling; per-record editorial/visual approvals are the content gate, not daily approval clicks. Verify the actual environment policy before enabling. Configure `PINTEREST_PUBLISHER_ENABLED=true` only after the authorized launch passes live read-only checks. It is absent/false by default.
5. Independently confirm destination purchasability and approved offer/price before live enablement; the automated HTTP 200/HTML check establishes retrieval only, not checkout or sales readiness. Populate one genuinely approved, full-size QA-passed, public image/copy record. Run `check` with read scope. Review output. The first two-Pin write run is authorized in scope, but must wait for the coordinated activation checks; plan/check success alone is not evidence of publication.
6. Existing orchestrator dispatches one chosen record at a time; follow with `status`, report errors/drafts distinctly, and later verify sentAt/publication ID. Do not treat queued as sent. Continue outcome tracking at the existing scheduler cadence, with no new scheduler.

Schema/live reads were verified via connected Buffer tools, but **new direct GraphQL operations and GitHub ledger writes have not been integration-tested with real keys**. Activation needs authorized read-only direct query check first, then controlled live execution with the two approved Pins. Current tests are fake API/ledger tests, not evidence of real publication.

## Official API sources reviewed 2026-11-03

- https://developers.buffer.com/reference — CreatePostInput, ImageAssetInput → metadata → altText, PinterestPostMetadataInput, post status, post externalLink/sentAt, pagination and channel metadata.
- https://developers.buffer.com/examples/create-image-post.html — image URL input and PostActionSuccess/MutationError.
- https://developers.buffer.com/examples/get-paginated-posts.html — complete history pagination.
- https://developers.buffer.com/guides/authentication.html — Bearer header, scope definitions, account-wide key boundary.
- https://support.buffer.com/en-us/articles/how-to-create-your-buffer-api-key-ShIgYVwM6j — personal-key permissions and free-plan single-key limit.
