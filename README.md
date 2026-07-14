# Traffic Runner

Python runner for scheduled SimilarWeb traffic backfill, homepage asset capture, and pricing task execution.

It uses the Cloudflare D1 `ainav` database as the task source and system of record. Traffic mode first verifies that Similarweb has published the target previous-month data through one configured probe domain. Only after that D1-backed release gate is available does it queue the catalog-wide traffic batch, fetch through the Bright Data proxy zone, store rows in `domain_traffic_snapshots` and `tool_traffic_monthly`, then update `traffic_tasks` and `tool_traffic_fetch_status`.

Pricing mode consumes existing `pricing_tasks`, fetches public pricing pages with normal browser-like request headers, stores `pricing_snapshots` and `pricing_extractions`, and leaves results in `manual_review`. Reviewers approve the stored extraction in ainav Admin; the runner then materializes that exact JSON into the active catalog.

Pricing extraction runs deterministic rules first. If rules cannot produce a trusted structure and `OPENAI_API_KEY` or `OPENAI_API` is set, it falls back to OpenAI structured JSON extraction. The default model is `gpt-5.4-mini`; set `OPENAI_PRICING_FALLBACK_MODEL` only when a second model should be tried after invalid or low-confidence output.

If static fetching and OpenAI still cannot produce trusted pricing from a likely pricing URL, pricing mode can use Cloudflare Browser Run to fetch rendered HTML, then rerun the same rule and OpenAI extraction path. Enable it with `CLOUDFLARE_BROWSER_RENDERING_ENABLED=1`. The Cloudflare token must include Browser Rendering edit access; set `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN` if the normal D1 token does not have that permission.

Pricing extraction payloads include `final_pipeline_stage` for tracking the final path: `rule`, `openai`, `browser_run_rule`, `browser_run_openai`, `contact_sales`, `manual_review`, or `browser_run_manual_review`.

Assets mode scans active catalog tools (`pending_enrich`, `pending_review`, and `published`) missing required catalog data, claims `asset_tasks`, captures homepage screenshots with Cloudflare Browser Run, uploads screenshots/favicons to R2, and writes assets, localization, categories, and key features. Every assets batch also refreshes the canonical readiness projection for the active catalog independently of whether an asset task was claimed, so manual fixes can advance a `pending_enrich` tool to `pending_review` and published records retain current quality signals.

Domain-state mode queues stale or missing domains into `domain_state_tasks`, then claims them with expiring leases and fenced completion tokens before updating `domain_states`. Every workload writes D1-backed runner heartbeats and batch history to `runner_instances` and `runner_runs`.

## Setup

```bash
cd tool-data-runner
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` with:

- `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`: D1 REST API access.
- `BRIGHTDATA_PROXY_USER`, `BRIGHTDATA_PROXY_PASSWORD`: Bright Data proxy credentials for traffic mode.
- Optional runner identity and tuning: stable `RUNNER_INSTANCE_ID`, deploy label `RUNNER_VERSION`, `RUNNER_LIMIT`, `RUNNER_PRICING_LIMIT`, `RUNNER_PRICING_TIMEOUT_SECONDS`.
- Traffic release gate: `TRAFFIC_RELEASE_PROBE_DOMAIN` (default `chatgpt.com`), `TRAFFIC_RELEASE_PROBE_START_DAY` (default `7`), `TRAFFIC_RELEASE_PROBE_INTERVAL_SECONDS` (default `21600`), and `TRAFFIC_RELEASE_QUEUE_LIMIT` (default `5000`).
- Optional pricing AI fallback: `OPENAI_API_KEY` or `OPENAI_API`, plus `OPENAI_PRICING_MODEL` and `OPENAI_PRICING_FALLBACK_MODEL`.
- Optional rendered-page fallback: `CLOUDFLARE_BROWSER_RENDERING_ENABLED`, `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS`.
- Assets mode: `RUNNER_ASSET_LIMIT`, `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN`, `CLOUDFLARE_R2_ACCESS_KEY_ID`, `CLOUDFLARE_R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_R2_BUCKET`, and optional `R2_PUBLIC_BASE_URL`.
  Use the real R2 bucket name for `CLOUDFLARE_R2_BUCKET` (for example `sitesimgs`) and the public/custom domain for `R2_PUBLIC_BASE_URL` (for example `https://img.sigpik.com`). The D1 `tool_assets.storage_bucket` value remains `sitesimgs` for compatibility with the existing frontend.

`wrangler.toml` points at the same `ainav` D1 database used by the frontend. Keep `CLOUDFLARE_D1_DATABASE_ID` in `.env` aligned with that file.

## Run

Docker default command runs all loops in one process:

```bash
python runner.py --all --loop --interval-seconds 300
```

Combined `--all` mode deliberately leaves new pricing results in `manual_review`. The legacy `--approve-pricing` switch is rejected so a refetch cannot bypass the audited Admin review.

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

Process queued pricing tasks:

```bash
python runner.py --pricing --once --limit 10
```

Dry-run a specific pricing task without D1 writes:

```bash
python runner.py --pricing --once --task-id 126 --dry-run
```
