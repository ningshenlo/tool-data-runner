# tool-data-runner root layout

## Runtime (Dokploy image / worker loops)

Kept near the entrypoint or under packages the image copies:

| Path | Role |
|---|---|
| `runner.py` | CLI + late re-exports for unittest patches |
| `config/`, `libs/`, `shared/`, `workloads/`, `contracts/` | packages used by workers |
| `ops/` | operator CLIs; still in image for `--eval-gold` |
| `report_*.py`, `market_snapshot_refresh.py`, `search_demand_*.py` | report / market / demand refresh |
| `taxonomy_batch.py`, `taxonomy_shadow.py`, `taxonomy_schema.py`, `taxonomy_precision.py` | taxonomy pipeline support |
| `jev.py`, `jev_capabilities.py`, `jev_taxonomy.py` | Jev taxonomy helpers |
| `anti_bot_signatures.py`, `classification_anomalies.py` | assets / taxonomy support |
| `postgres_client.py`, `postgres_legacy.py` | PG transport shims |
| `sitemap_monitor/` | sitemap worker package |
| `report-runtime/`, Compose, Dockerfiles, `requirements*.txt` | deploy artifacts |

## Tests

All `test_*.py` live under `tests/` (fixtures in `tests/fixtures/`). From repo root:

```bash
python -m unittest tests.test_runner_stores
python -m unittest discover -s tests -p "test_*.py"
```

## One-shot / ops (not on the worker hot path)

| Path | Role |
|---|---|
| `scripts/once/*_once.py` | sitemap keyword / seed experiments — run once, not Dokploy loops |
| `ops/` | taxonomy incident + gold eval tooling |
| `scripts/sql/` | historical SQL helpers |
| `scripts/check-*.py`, `crawl_ai_directories.py`, `build_revision.py`, `sync-report-runtime.py` | CI / ops utilities (see `scripts/README.md`) |

See also `ops/README.md`.
