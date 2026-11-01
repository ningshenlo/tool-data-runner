# Ops tools (not Dokploy loop dependencies)

Manual / incident / evaluation entry points. Production worker images still ship
this package so `python runner.py --eval-gold` keeps working, but these modules
are not on the hot path for assets/traffic/taxonomy loops.

| Module | Role |
|---|---|
| `taxonomy_eval.py` | Gold CSV evaluation (`runner.py --eval-gold`) |
| `dry_run_non_product_classification.py` | Read-only non-product cohort dry run |
| `taxonomy_incident_apply.py` | Apply a frozen taxonomy incident plan |
| `taxonomy_incident_rollback.py` | Roll back an incident |
| `taxonomy_incident_requeue_dry_run.py` | Dry-run requeue impact report |

Prefer `python -m ops.<module> …` when a module exposes `__main__`.
