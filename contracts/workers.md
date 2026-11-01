# Worker map

One Docker image. Separate Dokploy services. Code packages under `workloads/`.

| Dokploy service | CLI | Package | Owns |
|---|---|---|---|
| `assets-worker` | `python runner.py --assets --loop` | `workloads/assets` | `asset_tasks`, enrichment reconcile, catalog auto-publish |
| `periodic-facts-worker` | `python runner.py --periodic-facts --loop` | `workloads/traffic` + `workloads/domain_state` + `workloads/market` (+ `workloads/periodic`) | Similarweb traffic, Ahrefs DR/RDAP, market snapshot hooks |
| `taxonomy-worker` | `python runner.py --taxonomy --loop` | `workloads/taxonomy` | primary taxonomy automation |
| `sitemap-monitor-worker` | sitemap_monitor CLI | `sitemap_monitor/` | sitemap sync (separate package) |

Legacy: `python runner.py --all --loop` → `workloads.periodic.run_all_loop` (rollback only).

Heartbeat / identity: `runner_instances.instance_id` defaults to the service name (`assets-worker`, …).

## Package modules

### `workloads/assets/`

| Module | Responsibility |
|---|---|
| `loops.py` | `--assets --loop` scheduling |
| `pipeline.py` | `run_assets_once`, `process_asset_task`, favicon fetch |
| `store.py` | `D1AssetStore` |
| `browser.py` | Browser Rendering + static homepage fetch |
| `content.py` | HTML/name/safety helpers |
| `enrichment.py` / `publish.py` / `r2.py` | readiness + auto-publish + R2 |

### `workloads/traffic/`

| Module | Responsibility |
|---|---|
| `loops.py` | traffic polling loop |
| `pipeline.py` | `run_once` / `_run_once` / `process_task` (+ market/search/report hooks) |
| `client.py` | `SimilarWebClient` |
| `store.py` | `D1TaskStore` |
| `release.py` | month release gate |
| `parse.py` / `country.py` / `backfill.py` | metrics parse, country dual-write, projection backfill |

### `workloads/domain_state/`

| Module | Responsibility |
|---|---|
| `loops.py` | domain-state polling + discovery backoff |
| `pipeline.py` | `run_domain_state_once` / `process_domain_state` |
| `client.py` | Ahrefs + RDAP client |
| `store.py` | `D1DomainStateStore` |

### `workloads/market/`

| Module | Responsibility |
|---|---|
| `snapshot.py` | build / activate / preview / coverage / facet rollups |

Publication refresh orchestration still lives in root `market_snapshot_refresh.py` / `search_demand_refresh.py` / `report_exports.py`; traffic `run_once` calls them via `runner.*`.

## Where to look when reviewing

- assets stuck / waiting / auto-publish → `workloads/assets/*`, `shared/readiness.py`
- traffic / release gate / country dual-write → `workloads/traffic/*`
- DR / RDAP → `workloads/domain_state/*`
- market snapshot activate/build → `workloads/market/snapshot.py` + `market_snapshot_refresh.py`
- classification → `workloads/taxonomy` + root `taxonomy_batch.py` / `taxonomy_shadow.py`
- PG SQL mapping → `shared/db/`
