# Tool Data Runner

Python runner for scheduled SimilarWeb traffic backfill, homepage asset capture, domain facts, and taxonomy updates.

The repository also contains the Phase 1 `sitemap_monitor` package. It discovers and
recursively checks sitemap resources, uses HTTP validators, filters serialization-only
changes with semantic fingerprints, establishes a no-alert first baseline, and writes
deterministic state/diff objects without using an LLM. See
[`sitemap_monitor/README.md`](sitemap_monitor/README.md) for its isolated CLI and scope.

It uses the Cloudflare D1 `ainav` database as the task source and system of record. Traffic mode first verifies that Similarweb has published the target previous-month data through one configured probe domain. Only after that D1-backed release gate is available does it queue the catalog-wide traffic batch, fetch through the Bright Data proxy zone, store rows in `domain_traffic_snapshots` and `tool_traffic_monthly`, then update `traffic_tasks` and `tool_traffic_fetch_status`.

Price monitoring was retired on 2026-10-01. Collection, extraction, review, and publication entrypoints have been removed. Historical evidence is retained; migration `0128_retire_price_monitoring.sql` deactivates collection sources.

Assets mode scans active catalog tools (`pending_enrich`, `pending_review`, and `published`) missing required asset or homepage facts, claims `asset_tasks`, captures homepage screenshots with Cloudflare Browser Run, uploads screenshots/favicons to R2, and writes assets, localization, content-safety, and key features. It does not classify or repair categories. Every assets batch also refreshes canonical readiness for the active catalog independently of whether an asset task was claimed, so an accepted taxonomy assignment or manual taxonomy decision can advance a `pending_enrich` tool to `pending_review`. The same worker automatically publishes bounded batches of `pending_review` tools that still pass the complete live-readiness predicate at commit time. Content-safety, screenshot, localization/name, feature, taxonomy, source, duplicate, or readiness failures remain in review. Each successful transition writes a policy- and runner-stamped `tool_change_log` entry before the status change in the same D1 batch.

Asset retries are requirement-driven and bounded. Before every attempt the runner checks the current screenshot, favicon, published description, key features, and content-safety state, then runs only the missing stages. Core metadata and key-feature extraction use separate Browser Run requests, so a later retry never recaptures or re-uploads a screenshot or favicon that already succeeded. Missing key-feature output falls back to deterministic facts extracted from the verified homepage. Incomplete dead letters are revived after a generation-based cooldown (24 hours, 7 days, then 30 days), while content-safety blocks are never revived automatically.

The standalone taxonomy worker is the only production classification owner. It writes canonical `product_taxonomy_assignments` through the OpenAI Batch/Responses pipeline (`gpt-5.6-luna`, with selective `gpt-5.6-terra` escalation). Catalog readiness and publication accept only a `verified` or `auto_accepted` assignment to an active primary-category leaf. Runtime code never reads or writes the retired category projection; final schema deletion is guarded by a published-tool coverage gate.

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
- Optional runner identity and tuning: stable `RUNNER_INSTANCE_ID`, `RUNNER_SERVICE_NAME`, deploy label `RUNNER_VERSION`, `RUNNER_LIMIT`.
- Workload concurrency: `RUNNER_TRAFFIC_CONCURRENCY`, `RUNNER_DOMAIN_CONCURRENCY`, and `RUNNER_ASSET_CONCURRENCY`. Each falls back to legacy `RUNNER_CONCURRENCY` when omitted.
- Logging: `RUNNER_LOG_LEVEL=info` emits lifecycle, batch summaries, failures, retries, and non-success task outcomes. Use `debug` temporarily for per-task success/start events.
- Traffic release gate: `TRAFFIC_RELEASE_PROBE_DOMAIN` (default `chatgpt.com`), `TRAFFIC_RELEASE_PROBE_START_DAY` (default `7`), `TRAFFIC_RELEASE_PROBE_INTERVAL_SECONDS` (default `21600`), and `TRAFFIC_RELEASE_QUEUE_LIMIT` (default `5000`).
- Optional rendered-page fallback: `CLOUDFLARE_BROWSER_RENDERING_ENABLED`, `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS`.
- Assets mode: `RUNNER_ASSET_LIMIT` (default `25`), `RUNNER_ASSET_CONCURRENCY` (default `10`), `RUNNER_ASSET_POLL_INTERVAL_SECONDS` (idle default `30`), `RUNNER_ENRICHMENT_RECONCILE_LIMIT` (default `100`), `RUNNER_ENRICHMENT_RECONCILE_CONCURRENCY` (default `5`), `CATALOG_AUTO_PUBLISH_ENABLED` (default `1`, emergency kill switch), `CATALOG_AUTO_PUBLISH_LIMIT` (default `25`, hard maximum `100`), `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_R2_ACCESS_KEY_ID`, `CLOUDFLARE_R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_R2_BUCKET`, and optional `R2_PUBLIC_BASE_URL`. Full asset batches and unfinished enrichment reconciliation continue immediately; only partial or empty queues back off.
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
| `taxonomy-worker` | `--taxonomy --loop` | production primary taxonomy automation |

An additional `sitemap-monitor-worker` profile is defined with a safe-off process
gate. Once enabled with `SITEMAP_MONITOR_ENABLED=1` and a non-zero
`SITEMAP_MONITOR_REPLICAS`, it authoritatively syncs every safe, non-duplicate,
published tool from remote D1 once per hour. Newly published domains are registered,
domains that leave the eligible catalog are paused, and previously paused domains are
reactivated when eligible again. Explicit `SITEMAP_MONITOR_SITES` and optional site
files remain supported as operator additions; an optional paused file overrides both
catalog and manual sources.

The initial catalog baseline is bounded by `SITEMAP_MONITOR_BATCH_SIZE` and
`SITEMAP_MONITOR_EXECUTION_CONCURRENCY`; it does not create unbounded HTTP work.
Sites without a usable sitemap remain registered and move through the existing
24-hour, 72-hour, and seven-day failure cooldown instead of being permanently
excluded. The historical 38-site observation files remain audit fixtures but are no
longer production defaults.

The compose file is intended for a Dokploy Compose project and intentionally exposes
no HTTP ports. Give every service a stable, unique `RUNNER_INSTANCE_ID`; the defaults
already match the service names. Deploy the split services before stopping the legacy
container: D1 leases and fenced completions prevent duplicate commits during the brief
overlap. Once all four services have healthy heartbeats, stop the old `--all` service.

`--all --loop` remains available only for rollback compatibility and logs a deprecation
event. Do not add new production workloads to it.

The taxonomy worker incrementally classifies active `pending_enrich`, `pending_review`,
and `published` tools. A verified or auto-accepted assignment to an active leaf is a
trusted primary and is never reclassified merely because the prompt or provider changed.
Only tools without such a primary enter the full profile/L1/leaf pipeline. Each eligible
product can receive one stable primary market, up to three evidence-backed secondary
markets, and up to twelve controlled capabilities. Capability assignments carry a
`core`, `supporting`, or `integration` role inside `evidence_json`.

By default (`TAXONOMY_BATCH_ENABLED=1`) all model work uses the OpenAI Batch API and
the Responses API with strict Structured Outputs. One D1-backed state machine records
the profile, L1, leaf, and capability stages, each JSONL request, the OpenAI batch/file
IDs, and input/cached/output/reasoning token usage. Jobs can therefore wait up to the
Batch API's 24-hour completion window and resume safely after a runner restart. Batch
files are grouped by model because an OpenAI input file may contain only one model.
An existing evidence-grounded profile from the current extractor version is reused,
so those tools begin at L1 and avoid one model call. Requests also share stable prompt
cache routing keys; actual cached-token hits are recorded per request rather than
assumed in the cost estimate.

`gpt-5.6-luna` handles the normal path. L1 uses low reasoning, profile and capability
use medium reasoning, and the primary leaf uses high reasoning. A missing or weak
answer, absent grounded leaf evidence, leaf confidence below `0.60`, or an L1 Top-2
gap below `0.08` escalates only that item to `gpt-5.6-terra` with high reasoning. A
tool that still has no valid leaf after escalation is explicitly stored as
`needs_review`; it is not silently treated as a successful classification.

The worker accepts either the standard `OPENAI_API_KEY` variable or the existing
`OPENAI_API` variable. `TAXONOMY_BATCH_ENABLED=0` is the rollback switch for the old
synchronous provider path. D1 migrations `0077_openai_taxonomy_batch.sql` and
`0079_taxonomy_batch_retry.sql` must be applied before enabling the Batch worker.
They create the persistent Batch tables and the bounded retry state respectively.

Capability classification is enabled by `TAXONOMY_CAPABILITIES_ENABLED=1`. It does not
send all active capability terms to the model: the worker deterministically recalls at
most `TAXONOMY_CAPABILITY_CANDIDATE_LIMIT` terms (default `96`) from the selected market
leaves and grounded product profile, then makes one capability model call. Set the
capability switch to `0` as an independent cost kill switch. A capability provider
failure never removes prior assignments or invalidates a successfully selected primary.

`TAXONOMY_CAPABILITY_BACKFILL_ENABLED=1` gradually fills tools that already have a
stored grounded profile but no active capabilities. For a tool with a trusted primary,
backfill preserves that primary, reuses the profile, skips homepage/profile/L1/leaf model
calls, and makes only the single bounded capability call. Tools whose profiles contain no
grounded capability evidence are left for evidence refresh instead of being sent through
full classification. Backfill batches always wait for the normal taxonomy interval even
when full; set this switch to `0` independently while comparing classification LLMs.

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
emergency kill switch. Genuine failures receive at most three total attempts;
succeeded, partial, and skipped results are not repeatedly charged. Failed-run budgets are
scoped to the active primary model, so switching providers can recover previously
exhausted tools without deleting their audit history.

Homepage acquisition and retryable OpenAI stage failures use persistent exponential
backoff. `OPENAI_TAXONOMY_MAX_ATTEMPTS` defaults to `3` total attempts, including the
first, and `OPENAI_TAXONOMY_RETRY_BASE_SECONDS` defaults to `300` seconds (then 600
seconds). Retry state survives process restarts. Only an exhausted item enters review;
deterministic request defects are not repeatedly submitted, and a late result from an
older attempt cannot overwrite a newer attempt.

Leaf transport/provider exceptions are stored as `failed` and remain retryable. A
semantic no-fit response is stored as `partial` for review. Either case preserves the
last valid automatic primary; old assignments are superseded only after a replacement
has been written. Browser extensions and standalone apps are product-eligible alongside
independent web products.

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

Run assets as a polling worker:

```bash
python runner.py --assets --loop --interval-seconds 300
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

Multidim Shadow Mode writes model results into the canonical taxonomy tables
(`product_profiles`, `classification_runs`, `product_taxonomy_assignments`). It
does not model-write legacy category fields; the deterministic compatibility
projector performs that projection after accepted assignments exist.

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


## Automatic monthly report drafts

The main Dokploy Compose file now builds and runs the report exporter and offline
renderer from this repository. See [deployment, state and review instructions](docs/report-drafts.md).

## Automatic monthly search demand publication

After each traffic batch (including idle batches), the collector catches up search
demand for the oldest missing, closed month that already passed market publication.
The published market snapshot fixes the product cohort; withdrawn/unsafe products
are excluded. This keeps source availability separate from a complete release.

`SEARCH_DEMAND_AUTO_PUBLISH_ENABLED` defaults to `1` in Compose; when unspecified
it follows `MARKET_SNAPSHOT_AUTO_PUBLISH_ENABLED`. Checks run hourly by default
(`SEARCH_DEMAND_CHECK_INTERVAL_SECONDS`, minimum 300 seconds). The lock and status
live in `/market-snapshots/search-demand` on the existing shared durable volume.

The task preserves stable keyword IDs, historical rows, and all current brand
labels, including manual reviews. Only new labels use the existing v2 domain and
direct-traffic policy. It writes canonical taxonomy term columns, never maps new
terms back into legacy category IDs. Traffic is summed across observed products;
search volume is the maximum sample value, not a sum. Missing previous months do
not become zero or cross-month comparisons.

Unpublished projections are written in batches of 100 records through the normal
D1 cost guard. Every persisted observation, monthly metric and taxonomy aggregate
is checked against the source plan. The source fingerprint is checked again, then
one final coverage-row insert publishes the month. Failures preserve the previous
public month; restart retries only unpublished data. Already published months are
immutable for this job. Data corrections need a separate revision procedure.

No new source requests, database migration, or frontend deployment are required.
The existing two-minute page-data cache picks up a published month on subsequent
requests. Diagnostic command (uses the collector's standard environment, read only):

```sh
python scripts/check-search-demand-publication.py --month 2026-08
```

Log event: `search_demand.auto_publish`. Status file:
`/market-snapshots/search-demand/publication-status.json`. Disable the dedicated
flag to pause this job without stopping traffic collection or market publication.

## Automatic monthly market publication

The traffic batch now checks completed public-catalog months and builds/activates
their immutable serving snapshot. Idle batches also check; report exports remain
independent. Dokploy defaults MARKET_SNAPSHOT_AUTO_PUBLISH_ENABLED to 1. Set it
explicitly to 1 in the production service; local .env.example keeps it at 0.
MARKET_SNAPSHOT_CHECK_INTERVAL_SECONDS defaults to 3600 (minimum 300). The shared
market-snapshots volume stores publication-status.json and the process lock.

Only released, closed months newer than the active version are eligible. Every
eligible domain must have materialized data or a genuine no_data/forbidden
terminal outcome. Failed, unfinished or contradictory tasks block publication.
The builder rechecks inputs, preserves the existing coverage gates and atomically
switches versions. Older delayed jobs cannot roll back a newer active month.
Previously published months are not automatically revised after later corrections.

Read-only diagnosis: python scripts/check-market-publication.py --month YYYY-MM.
Failures are recorded in publication-status.json and market_snapshot.auto_publish
logs; they do not undo collection or stop report exports. Authorization for remote
publication and deployment is still required before enabling a production run.


### D1 connectivity (2026-09-24)

Production runners use the Cloudflare D1 API directly with the existing account token. The per-query cost-control proxy and its cooldown ledger are retired. Optimized queue, taxonomy and sitemap SQL remain in the clients. Use Cloudflare billing and the existing budget alert for account spend.
