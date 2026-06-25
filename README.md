# Traffic Runner

Python runner for scheduled SimilarWeb traffic backfill.

It uses the Cloudflare D1 `ainav` database as the task source and system of record. Each run automatically queues missing previous-month SimilarWeb traffic tasks, fetches due traffic through the Bright Data proxy zone, stores rows in `domain_traffic_snapshots` and `tool_traffic_monthly`, then updates `traffic_tasks` and `tool_traffic_fetch_status`.

Domain-state and asset fetching are handled by `site-scraper`; this runner should not write `domain_states`.

## Setup

```bash
cd traffic-runner
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` with:

- `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`: D1 REST API access.
- `BRIGHTDATA_PROXY_USER`, `BRIGHTDATA_PROXY_PASSWORD`: Bright Data proxy credentials.
- Optional runner tuning: `RUNNER_LIMIT`.

`wrangler.toml` points at the same `ainav` D1 database used by the frontend. Keep `CLOUDFLARE_D1_DATABASE_ID` in `.env` aligned with that file.

## Run

Process one batch:

```bash
python runner.py --once --limit 20
```

Run as a polling worker:

```bash
python runner.py --loop --interval-seconds 300
```

The runner claims due D1 traffic tasks where `traffic_tasks.status` is `queued`, `failed`, `sync_failed`, or stale `processing`.
