# Catalog readiness contract

Source of truth: `shared/readiness.py`.

## Hard gates (`tool_live_ready_predicate`)

A tool may auto-publish only when all of these hold:

1. `content_safety_status = 'safe'`
2. `tool_enrichment_states.readiness = 'ready'`
3. localization has a non-empty `name`
4. accepted primary taxonomy (`verified` or `auto_accepted` with confidence ≥ floor)
5. at least one `tool_sources` row
6. admission JSON does not set `autoPublishAllowed=false` on official_site sources

Enrichment `blocking_json` (writer in assets/enrichment path) currently hard-blocks on:

- `published_localization` (name present)
- `category` (accepted primary)
- `source_evidence`

Screenshot, favicon, traffic, domain_state, name_quality are warnings — not hard blockers in current runner code.

## Admin UI caveat

`/admin/tools` historically recomputed a stricter “required” list in the Sigpik admin mapper. Prefer aligning that UI to this contract; do not treat the admin badge as the publish gate.
