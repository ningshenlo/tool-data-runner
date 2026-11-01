# tool-data-runner contracts

Single place to find worker boundaries, readiness gates, and SQL catalog rules.

| Doc | Purpose |
|---|---|
| [workers.md](workers.md) | Dokploy service ↔ package ↔ CLI mapping |
| [readiness.md](readiness.md) | Hard publication gates (source of truth) |
| [sql-catalog.md](sql-catalog.md) | PostgreSQL query catalog rules |

## Layout

```
tool-data-runner/
  runner.py                 # CLI, Config dataclass, D1Client, late re-exports
  config/constants.py       # static knobs (Similarweb, Ahrefs, assets, SQL templates)
  libs/                     # reusable helpers (logging, domains, numbers, http, time)
  shared/
    db/                     # PostgreSQL transport + query catalog
    readiness.py            # live-ready / auto-publish predicates
  workloads/                # one package per worker concern
  contracts/                # this folder
```

`runner.py` remains the process entrypoint. Prefer `from config.constants import …` and `from libs…` in new code; `runner` still re-exports names so existing patches and imports keep working.

Tests live under `tests/`. One-shot sitemap/Jev experiments are in `scripts/once/`; taxonomy incident / gold-eval tools are in `ops/`. See [docs/ROOT-LAYOUT.md](../docs/ROOT-LAYOUT.md).
