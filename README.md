# Tool Data Runner

Python runner for scheduled SimilarWeb traffic backfill, homepage asset capture, and pricing task execution.

The repository also contains the Phase 1 `sitemap_monitor` package. It discovers and
recursively checks sitemap resources, uses HTTP validators, filters serialization-only
changes with semantic fingerprints, establishes a no-alert first baseline, and writes
deterministic state/diff objects without using an LLM. See
[`sitemap_monitor/README.md`](sitemap_monitor/README.md) for its isolated CLI and scope.

It uses the Cloudflare D1 `ainav` database as the task source and system of record. Traffic mode first verifies that Similarweb has published the target previous-month data through one configured probe domain. Only after that D1-backed release gate is available does it queue the catalog-wide traffic batch, fetch through the Bright Data proxy zone, store rows in `domain_traffic_snapshots` and `tool_traffic_monthly`, then update `traffic_tasks` and `tool_traffic_fetch_status`.

Pricing mode consumes existing `pricing_tasks`, fetches public pricing pages with normal browser-like request headers, stores `pricing_snapshots` and `pricing_extractions`, and leaves results in `manual_review`. Reviewers approve the stored extraction in ainav Admin; the runner then materializes that exact JSON into the active catalog.

Pricing extraction runs deterministic rules first. If rules cannot produce a trusted structure and `OPENAI_API_KEY` or `OPENAI_API` is set, it falls back to OpenAI structured JSON extraction. The default model is `gpt-5.6-luna`; set `OPENAI_PRICING_FALLBACK_MODEL` only when a second model should be tried after invalid or low-confidence output.

If static fetching and OpenAI still cannot produce trusted pricing from a likely pricing URL, pricing mode can use Cloudflare Browser Run to fetch rendered HTML, then rerun the same rule and OpenAI extraction path. Enable it with `CLOUDFLARE_BROWSER_RENDERING_ENABLED=1`. The Cloudflare token must include Browser Rendering edit access; set `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN` if the normal D1 token does not have that permission.

Pricing extraction payloads include `final_pipeline_stage` for tracking the final path: `rule`, `openai`, `browser_run_rule`, `browser_run_openai`, `contact_sales`, `manual_review`, or `browser_run_manual_review`.

The evidence-bound pricing claims pipeline is guarded by two independent environment flags. `PRICING_CLAIMS_SHADOW=1` enables v2 shadow work without changing the active pricing catalog. `PRICING_CLAIMS_PUBLISH=1` is reserved for the later partial-publish cutover and is rejected unless shadow mode is also enabled. Both flags default to `0`; the initial Phase 0–2 implementation must not publish claims to production.

Shadow mode requires migration `0039_pricing_claims_pipeline.sql` and configured R2 credentials. It stores content-addressed HTML/text/structured-data/DOM-map artifacts, detects the pricing region, and records conservative Level 1 raw claims with DOM evidence. Deterministic normalization and entailment validation run per claim; ambiguous symbols such as bare `$` are never resolved from the diagnostic locale context. An unchanged region reuses existing R2 objects, and browser-rendered captures preserve both the original and rendered HTML. Shadow failures are logged and the legacy pricing extraction continues unchanged.

While Shadow is enabled, each pricing batch fills unused capacity with eligible legacy `manual_review` tasks that do not yet carry the V2 replay checkpoint. Normal queued/retry work remains first priority. `RUNNER_PRICING_MANUAL_REVIEW_REPLAY_LIMIT` caps the old backlog per batch (default `5`; `0` is the replay kill switch) independently of the normal pricing batch limit. The runner writes that checkpoint only after the V2 snapshot and artifacts persist successfully, including the zero-Claim outcome, so the backlog is resumable and cannot loop forever. Failed V2 capture reuses the task's existing bounded attempt budget. This replay never approves the legacy extraction and never publishes a Claim.

Assets mode scans active catalog tools (`pending_enrich`, `pending_review`, and `published`) missing required catalog data, claims `asset_tasks`, captures homepage screenshots with Cloudflare Browser Run, uploads screenshots/favicons to R2, and writes assets, localization, categories, and key features. Every assets batch also refreshes the canonical readiness projection for the active catalog independently of whether an asset task was claimed, so manual fixes can advance a `pending_enrich` tool to `pending_review` and published records retain current quality signals. The same worker automatically publishes bounded batches of `pending_review` tools that still pass the complete live-readiness predicate at commit time. Content-safety, screenshot, localization/name, feature, category, source, duplicate, or readiness failures remain in review. Each successful transition writes a policy- and runner-stamped `tool_change_log` entry before the status change in the same D1 batch.

Asset retries are requirement-driven and bounded. Before every attempt the runner checks the current screenshot, favicon, published description, key features, and category state, then runs only the missing stages. Core metadata, key-feature extraction, and category classification use separate Browser Run requests, so a later retry never recaptures or re-uploads a screenshot or favicon that already succeeded. Missing AI output falls back to deterministic facts extracted from the verified homepage and to lexical matching against the active taxonomy. Incomplete dead letters are revived after a generation-based cooldown (24 hours, 7 days, then 30 days), while content-safety blocks are never revived automatically.

Category classification first fetches rendered homepage HTML, removes navigation, footer, scripts, styles, templates, and SVG markup, prefers semantic `<main>` / `role=main`, and falls back to the cleaned body. Only this bounded main content is sent through Cloudflare Browser Rendering `/json` using inline HTML transport; the model-side request never navigates to the real product URL or a neutral external page. **DeepSeek V4 Flash** (`deepseek/deepseek-v4-flash`) is primary and Workers AI Llama 3.3 70B is fallback. DeepSeek custom-AI payloads request non-thinking mode and cap output tokens. Classification remains hierarchical: first choose one top-level category, then only among that parent's children. Stored raw results include the cleaned-input character count and content hash as well as the model chain, prompt version, and taxonomy hash. If the model chain is unavailable or returns an empty result, the runner assigns a deterministic taxonomy fallback from the official description and extracted features, records that provenance, and leaves the final catalog decision to the existing `pending_review` gate.

Published-category backfill re-runs hierarchical classification for `status='published'` tools that still carry pre-hierarchical / unprovenanced category rows. It never selects `rejected` tools, never overwrites `source='manual'` assignments, and only replaces live categories after a successful classification. Failures keep the previous live categories and write audit/error only. Successful runs stamp `category_classification_raw.backfill = published-legacy-v1` so the job is resumable.

Domain-state mode refreshes each Ahrefs DR once every 30 days by default. Request starts are paced at 60 per minute: one request per second, matching Ahrefs' documented default limit. The domain queue polls every second instead of pausing for the former fixed 15-minute interval between batches. RDAP is independent and currently runs only once per domain; `done`, `no_data`, and `failed` outcomes are persisted so later DR refreshes never repeat the RDAP request. Tasks use expiring leases and fenced completion tokens before updating `domain_states`. Every workload writes D1-backed runner heartbeats and batch history to `runner_instances` and `runner_runs`.

## Setup

```bash
cd tool-data-runner
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` with:

- `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`: D1 REST API access.
- `AHREF_API_KEY`: Ahrefs free Domain Rating API authentication. The runner sends it as `Authorization: Bearer <token>`.
- DR cadence and provider budget: `RUNNER_DOMAIN_STATE_MAX_AGE_DAYS` (default `30`), `RUNNER_DOMAIN_POLL_INTERVAL_SECONDS` (default `1` while draining a full batch), and `RUNNER_AHREFS_REQUESTS_PER_MINUTE` (default `60`, hard-clamped to Ahrefs' documented ceiling). Polling backs off to at least 10 seconds after a partial batch and 60 seconds when idle, avoiding a full D1 queue scan every second. The free DR endpoint does not consume API units; HTTP 429 responses honor `Retry-After` before retrying.
  The limiter is process-local, so production should run one `periodic-facts-worker` replica. If that service is intentionally scaled out, divide the 60 requests/minute budget across replicas.
- `BRIGHTDATA_PROXY_USER`, `BRIGHTDATA_PROXY_PASSWORD`: Bright Data proxy credentials for traffic mode.
- Optional runner identity and tuning: stable `RUNNER_INSTANCE_ID`, `RUNNER_SERVICE_NAME`, deploy label `RUNNER_VERSION`, `RUNNER_LIMIT`, `RUNNER_PRICING_LIMIT`, `RUNNER_PRICING_MANUAL_REVIEW_REPLAY_LIMIT`, `RUNNER_PRICING_TIMEOUT_SECONDS`.
- Workload concurrency: `RUNNER_TRAFFIC_CONCURRENCY`, `RUNNER_DOMAIN_CONCURRENCY`, `RUNNER_ASSET_CONCURRENCY`, and `RUNNER_PRICING_CONCURRENCY`. Each falls back to legacy `RUNNER_CONCURRENCY` when omitted.
- Logging: `RUNNER_LOG_LEVEL=info` emits lifecycle, batch summaries, failures, retries, and non-success task outcomes. Use `debug` temporarily for per-task success/start events.
- Traffic release gate: `TRAFFIC_RELEASE_PROBE_DOMAIN` (default `chatgpt.com`), `TRAFFIC_RELEASE_PROBE_START_DAY` (default `7`), `TRAFFIC_RELEASE_PROBE_INTERVAL_SECONDS` (default `21600`), and `TRAFFIC_RELEASE_QUEUE_LIMIT` (default `5000`).
- Optional pricing AI fallback: `OPENAI_API_KEY` or `OPENAI_API`, plus `OPENAI_PRICING_MODEL` and `OPENAI_PRICING_FALLBACK_MODEL`.
- Optional rendered-page fallback: `CLOUDFLARE_BROWSER_RENDERING_ENABLED`, `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS`.
- Assets mode: `RUNNER_ASSET_LIMIT`, `CATALOG_AUTO_PUBLISH_ENABLED` (default `1`, emergency kill switch), `CATALOG_AUTO_PUBLISH_LIMIT` (default `25`, hard maximum `100`), `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_WORKERS_AI_API_TOKEN`, `DEEPSEEK_API_KEY` (for DeepSeek V4 Flash category classification), `CATEGORY_MAIN_CONTENT_MAX_CHARS` (default `10000`), `CATEGORY_DEEPSEEK_MAX_OUTPUT_TOKENS` (default `1024`), `CLOUDFLARE_R2_ACCESS_KEY_ID`, `CLOUDFLARE_R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_R2_BUCKET`, and optional `R2_PUBLIC_BASE_URL`. The Workers AI token is used for the category fallback model.
  Use the real R2 bucket name for `CLOUDFLARE_R2_BUCKET` (for example `sitesimgs`) and the public/custom domain for `R2_PUBLIC_BASE_URL` (for example `https://img.sigpik.com`). The D1 `tool_assets.storage_bucket` value remains `sitesimgs` for compatibility with the existing frontend.

`wrangler.toml` points at the same `ainav` D1 database used by the frontend. Keep `CLOUDFLARE_D1_DATABASE_ID` in `.env` aligned with that file.

## Run

Run the Sitemap Monitor Phase 1 CLI locally (isolated from the legacy `runner.py`
workloads and from remote D1/R2):

```bash
python -m sitemap_monitor --site https://example.com --once
python -m sitemap_monitor --site https://example.com --loop \
  --interval-seconds 30 --check-interval-seconds 21600
```

The first successful scan only creates a baseline. A later scan emits a diff only
when the normalized URL set changes. The loop polls the D1-backed due-site scheduler;
it does not rescan every configured site on every loop. `sitemap_jobs` provides the
idempotency key, bounded retry/DLQ state, expiring job lease, and fenced completion.
Local state defaults to `.sitemap-monitor/`.

Production defaults to a six-hour successful-site cadence. Only transient network,
429, and 5xx failures retry within one job; deterministic failures such as 404 and
unsafe XML enter the dead-letter ledger after one attempt and schedule the site on a
24-hour, 72-hour, then seven-day cooldown. Six-hour maintenance expires superseded
jobs and prunes bounded batches of low-value run/scan history while retaining
baselines, changed runs, migration/resource-set audits, and current semantic state.

Production is split into four isolated Dokploy services that share one Python image:

```bash
docker compose -f docker-compose.dokploy.yml up -d
```

| Service | Command | Workloads |
|---|---|---|
| `periodic-facts-worker` | `--periodic-facts --loop` | Similarweb traffic + Ahrefs DR/RDAP |
| `assets-worker` | `--assets --loop` | assets + enrichment readiness + guarded catalog auto-publish |
| `pricing-monitor-worker` | `--pricing --loop` | paused by default; pricing snapshots/extractions/Claims shadow when explicitly enabled |
| `taxonomy-worker` | `--taxonomy --loop` | production primary taxonomy automation |

An additional `sitemap-monitor-worker` profile is defined with a safe-off default.
Remote migrations `0060_sitemap_monitor_phase1.sql` and
`0061_sitemap_comparability_gate.sql`, a private R2 bucket, a 30-site active
observation cohort, and an 8-site ineligible quarantine have been prepared.
Enable it explicitly with both
`SITEMAP_MONITOR_ENABLED=1` and a non-zero `SITEMAP_MONITOR_REPLICAS`; the cohort is
observation-only and does not publish Signals.

The compose file is intended for a Dokploy Compose project and intentionally exposes
no HTTP ports. Give every service a stable, unique `RUNNER_INSTANCE_ID`; the defaults
already match the service names. Deploy the split services before stopping the legacy
container: D1 leases and fenced completions prevent duplicate commits during the brief
overlap. Once all four services have healthy heartbeats, stop the old `--all` service.

`--all --loop` remains available only for rollback compatibility and logs a deprecation
event. Do not add new production workloads to it.

The taxonomy worker classifies active `pending_enrich`, `pending_review`, and `published`
tools that have no terminal run for the current prompt. It runs primary classification
only; capabilities remain optional and never block the primary category.

Successful leaves at or above `TAXONOMY_AUTO_ACCEPT_CONFIDENCE` (default `0.50`)
are stored as `auto_accepted` and are immediately available to product detail
pages. Results between the evidence floor (`0.35`) and the auto-accept threshold
remain `provisional`; lower-confidence leaves are `unresolved`. Existing verified
manual primaries are immutable and always win.

Production defaults process batches of 50 tools with concurrency 3. Full batches
continue immediately while work remains; the worker waits 5 minutes only after a
partial or empty batch indicates that the current backlog is drained. A
billing, quota, rate-limit, or authentication response trips a batch circuit
breaker and backs taxonomy off for 6 hours. Set `TAXONOMY_AUTO_ENABLED=0` as the
emergency kill switch. Genuine failed runs retry at most three times; succeeded,
partial, and skipped results are not repeatedly charged. Failed-run budgets are
scoped to the active primary model, so switching providers can recover previously
exhausted tools without deleting their audit history.

The paid stale-`non_product` incident recheck is frozen by default. When explicitly
enabled, it selects published tools whose labels were written automatically and have
no terminal run for the current prompt. These incident rows are drained before
ordinary backlog; manual entity decisions and pending catalog records are never
selected by this incident cohort.
If a blocked or unreachable page cannot provide fresh evidence, the stale automatic
label is safely demoted to `unresolved` and recorded as `partial`, preventing repeated
provider charges. Incident batches are paced by the normal taxonomy interval even when
full, leaving an operator window between batches. Keep
`TAXONOMY_RECHECK_AUTO_NON_PRODUCT=0`; setting it to `1` requires an explicitly
approved cohort and spend window.

Taxonomy stages use only the configured trusted model chain; Workers AI is excluded
from entity, L1 and leaf decisions. Anti-bot pages rejected by the pre-model quality
gate use no model.

Pricing monitoring is paused by default. Compose scales `pricing-monitor-worker`
to zero with `PRICING_MONITOR_REPLICAS=0`, and `PRICING_MONITOR_ENABLED=0` is a
second kill switch that prevents D1/provider work even if a container is started
manually. Re-enable pricing only by setting both values to `1`.

Pricing deliberately leaves new results in `manual_review`. The legacy `--approve-pricing` switch is rejected so a refetch cannot bypass the audited Admin review.

Run the combined periodic facts profile locally:

```bash
python runner.py --periodic-facts --once --limit 20
python runner.py --periodic-facts --loop --interval-seconds 300
```

Run taxonomy independently:

```bash
python runner.py --taxonomy --once
python runner.py --taxonomy --loop
```

Process one batch:

```bash
python runner.py --once --limit 20
```

Run as a polling worker:

```bash
python runner.py --loop --interval-seconds 300
```

The runner does not enqueue a new month merely because the calendar changed. Starting on the configured release-probe day, it checks one stable domain on the configured interval. The Similarweb response must contain usable data for the exact target month before the full queue is opened. A response containing only an older month remains blocked and is recorded in `traffic_month_release_checks` for audit.

The runner claims due D1 traffic tasks where `traffic_tasks.status` is `queued`, `failed`, `sync_failed`, or stale `processing`.
If a legacy task is marked `done` but its `tool_traffic_monthly` materialization is missing, the runner opens a new fenced generation and refetches it. `no_data` and `forbidden` remain terminal and are not revived.

Capture missing homepage screenshots and favicons:

```bash
python runner.py --assets --once --limit 10
```

Reclassify published tools with legacy category data (resumable; rejects are skipped):

```bash
# Dry-run one tool first
python runner.py --backfill-published-categories --dry-run --task-id 12345

# Small live batch
python runner.py --backfill-published-categories --limit 20

# Continue until candidates are exhausted (repeat; already-backfilled tools are skipped)
python runner.py --backfill-published-categories --limit 100
```

Run assets as a polling worker:

```bash
python runner.py --assets --loop --interval-seconds 300
```

Process queued pricing tasks:

```bash
python runner.py --pricing --once --limit 10
```

Dry-run a specific pricing task without D1 writes:

```bash
python runner.py --pricing --once --task-id 126 --dry-run
```

Build and promote a Market Explorer serving snapshot in separate, auditable steps:

```bash
# Creates a candidate. This does not change the active snapshot.
python runner.py --build-market-snapshot

# WRITE operation: reruns coverage gates, then atomically retires the old active
# snapshot and activates the specified candidate. It does not rebuild data.
python runner.py --activate-market-snapshot-id <candidate_id>
```

The activation-only command is mutually exclusive with build and loop modes. It
requires D1 credentials but does not require Bright Data credentials. Always inspect
the candidate's coverage summary from the build output before running it.

## Shadow taxonomy (P2A)

Multidim Shadow Mode classifies products into **new tables only**
(product_profiles, classification_runs, product_taxonomy_assignments).
It does **not** dual-write legacy primary_category_id / 	ool_categories.

Admin review is exception-based in production: `auto_accepted` assignments stay
out of the default queue. The queue contains provisional/unresolved primaries and
the latest failed/partial current-prompt runs. Use the named audit cohort when a
spot-check of auto-accepted results is desired.

`ash
# dry-run one tool (no D1 writes)
python runner.py --shadow-taxonomy --task-id 43 --dry-run

# write shadow results for specific tools (entity_kind may be unresolved)
python runner.py --shadow-taxonomy --task-id 43 --task-id 46 --limit 5

# batch existing independent_product records only
python runner.py --shadow-taxonomy --limit 10 --primary-only

# full-catalog first pass: auto-resolve entity, then classify eligible products
# Current-prompt runs are skipped automatically, so repeating this command
# advances to the next batch instead of reprocessing the first 100 tools.
python runner.py --shadow-taxonomy --limit 100 --allow-unresolved-entity --primary-only

# optional explicit resume cursor; specific --task-id reruns always bypass skip logic
python runner.py --shadow-taxonomy --limit 100 --after-tool-id 500 --allow-unresolved-entity --primary-only

# once a sequential smoke batch is stable, use bounded concurrency for larger batches
python runner.py --shadow-taxonomy --limit 300 --after-tool-id 184 --allow-unresolved-entity --primary-only --shadow-concurrency 3
`

## Gold evaluation (P2B)

Compare Shadow assignments to a Gold CSV and emit metrics + legacy/shadow/gold diffs.
Does **not** write legacy category tables.

`ash
python runner.py --eval-gold
python runner.py --eval-gold --gold-csv ../docs/taxonomy/gold-dataset-seed-draft.csv --auto-accepted-threshold 0.85
`

Reports land in docs/taxonomy/reports/ (gold-eval-latest.md / .json).
