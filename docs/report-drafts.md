# Monthly report drafts in Dokploy

`docker-compose.dokploy.yml` builds both the existing collector and the report
renderer from this repository. The deployment branch is
`agent/fix-traffic-runner-observability`. No separate image registry, Sigpik
checkout, desktop package, new API key or external scheduler is required.

## What runs automatically

1. `periodic-facts-worker` finishes its traffic pass, including an idle pass.
2. The export hook checks the previous calendar month and older pending months.
   It uses the existing guarded D1 client and SELECTs only. The monthly release
   must be available and every candidate must have terminal collection evidence.
3. It freezes hashed snapshot/traffic files and writes `complete.json` last.
4. `report-draft-worker` polls the shared exports every 60 seconds. It validates
   completion hashes and coverage, then generates bilingual report data, copy,
   PNG charts, CSV downloads, a review document and a pending approval file.
5. Completed input/runtime combinations are deduplicated across restarts.
   Waiting/blocked markets retry independently with backoff from 5 minutes to
   6 hours. A changed input or runtime is retried immediately. No report is
   automatically installed into the website, indexed, published or deployed.

The collector checks readiness at most once per hour per pending month by
default. `REPORT_EXPORT_ENABLED=0` pauses new exports; existing queued exports
can still be consumed. Stop `report-draft-worker` to pause rendering as well.
Do not remove the named volumes when redeploying.

## Persistent state and verification

| Container path | Named volume | Contents |
| --- | --- | --- |
| `/report-exports` in collector; `/exports` read-only in renderer | `sigpik-report-exports` | `<month>/export-status.json`, sector snapshots and completion events |
| `/app/work` in renderer | `sigpik-report-drafts` | `reports/drafts/`, automation state, attempt receipts and logs |

The renderer has a read-only root filesystem and no network, database or provider
credentials. Only its working volume and temporary directory are writable.
Its health check measures worker liveness, not whether all markets are ready.
Inspect producer `export-status.json` and consumer `state.json` for readiness;
a healthy idle worker alone does not prove that production data has arrived.

From the Dokploy Compose project directory:

```sh
docker compose -f docker-compose.dokploy.yml ps
docker compose -f docker-compose.dokploy.yml logs --tail=80 report-draft-worker
docker compose -f docker-compose.dokploy.yml exec -T periodic-facts-worker python report_exports.py --check-month 2026-08
docker compose -f docker-compose.dokploy.yml exec -T report-draft-worker python -c "import json; from pathlib import Path; [print(p, json.loads(p.read_text())) for p in Path('/app/work/reports/automation').glob('*/*/state.json')]"
```

`--check-month` uses SELECTs only, never writes completion events and never calls
a collection provider. Exit code 2 means a blocked market; check the JSON for
waiting states even when the exit code is 0. An exception produces a nonzero exit.
The renderer's `--once` returns 2 if any persisted market remains blocked. Every
attempt has its own `receipt.json`, `queue.json` and `process.log` when available.

Open the draft's `REVIEW.md`, PNG and CSV files for review. Copy files directly
from `/app/work/reports/drafts/<folder>/`; a ZIP is not needed. Approval remains
bound to the exact draft bytes and a second reviewer. Release must run in the
matching website workspace with the same Node/Python/font runtime, followed by
the existing site checks and deployment procedure. The server runtime explicitly
refuses `release`.

## Markets and future maintenance

The current generator supports music, image and video. Their category mappings
and dated supplemental coverage evidence are in `report-markets.json`. The
first public edition remains music only; image/video drafts do not become public
just because the worker generated them. Missing required-domain evidence blocks
that market. Reuse authorization is edition-specific, not inherited by new months.
Adding a new market requires an explicit sector definition, category/coverage
rules and validation before enabling its monthly export.

`report-runtime/` is a vendored code package. It contains no report datasets or
generated assets. `runtime-baseline.json` carries hashes and publication status
instead of omitted website data, preserving stale-source and frozen-edition
checks. `bundle.json` verifies every shipped file during Docker build. Its source
commit is provenance only; file hashes also capture uncommitted source changes.

After changing report templates, generator code, shared registration/SEO source,
or publishing a website edition, refresh the bundle and commit it with the change:

```sh
python scripts/sync-report-runtime.py --source /path/to/sigpik
python scripts/sync-report-runtime.py --verify
```

The script copies only the code allowlist and brand logo; all existing report
JSON, CSV, charts and release artifacts remain outside this public repository.
Do not hand-edit vendored files. If the website changes after a draft is prepared,
refresh the baseline and regenerate before review; never bypass its release hash
check. Frozen monthly exports are retained rather than overwritten by corrections.
A data correction therefore requires an explicit reviewed replacement workflow.

## Build validation

The `Report runtime` GitHub Actions workflow builds both images, runs the real
collector-hook/worker tests, and performs an offline smoke test with synthetic
counts. It renders two markets while a third is deliberately blocked, checks all
28 PNG assets and manifest hashes, restarts the worker, and verifies immutable
exports and the release restriction. Code and exports are mounted read-only in
the rendering test. No live data, credentials or report artifacts are uploaded.

GitHub CI success proves the build and synthetic flow. Production activation
still requires the Dokploy deployment to succeed and its existing D1 guard
credentials to work; inspect the server receipts above for the first real batch.
