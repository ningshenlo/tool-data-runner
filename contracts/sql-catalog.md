# PostgreSQL SQL catalog rules

Transport: `shared/db/legacy.py` + `shared/db/postgres-query-catalog.json`.

## Rules

1. Prefer an explicit catalog entry (hashed ainav SQL → Postgres SQL).
2. Changing runner SQL shape requires a catalog update and a regression in `tests/test_postgres_deployment.py`.
3. `dynamic_postgres_entry()` is a last-resort tokenizer rewrite. Compound operators (`<>`, `<=`, `>=`, `!=`) must remain single tokens.
4. Unknown SQL must fail closed (`Unmapped ainav SQL`) when dynamic translation is disabled or unsuitable — never silently talk to the wrong database.

## Layout

| Path | Role |
|---|---|
| `shared/db/client.py` | psycopg pool / explicit statements |
| `shared/db/legacy.py` | catalog lookup + D1-shaped API |
| `shared/db/postgres-query-catalog.json` | hashed query bodies |
| `postgres_client.py` / `postgres_legacy.py` | root compatibility shims for CI and older imports |
