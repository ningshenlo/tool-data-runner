# `INC-2026-08-EXAMPLE-COM` rollback dry-run

## Purpose

`taxonomy_incident_rollback.py` creates a read-only rollback plan for the frozen
1,369-tool confirmed `example.com` classification incident. It does not write D1, enqueue
reclassification, fetch websites, or call a model.

The frozen cohort comes from the original 2026-08-14 anomaly dry-run JSON. That
source candidate set contained 1,371 `non_product` records, but only 1,369 had
confirmed `neutral_transport` evidence. Clone My Voice (#3551) and Macaron
(#3782) had real blog/sunset-page evidence and are excluded from this incident;
they require separate entity/page-role review. This
is intentional: a live `WHERE entity_kind = 'non_product'` query would drop tools
already repaired by later clean V4 runs and would make the incident population
change over time.

## Command

```powershell
python taxonomy_incident_rollback.py `
  --manifest logs/non-product-classification-dry-run-20260814T091304Z.json `
  --expected-count 1369 `
  --expected-source-count 1371
```

The command writes a JSON audit artifact and a compact Markdown summary under
`logs/`. The JSON includes every incident member, the captured incident run,
current entity state, later/prior clean runs, active reprocess requests,
surviving legacy/manual assignments, proposed effects, blockers and a SHA-256
plan hash.

## Safety model

- D1 access uses `ReadOnlyD1Client`, which rejects mutation SQL and all batches.
- A captured incident run pointer is required for automatic entity restoration.
- A missing or inconsistent pointer becomes `ambiguous_provenance` and has no
  proposed effects.
- Manual entity decisions and verified manual primary assignments are protected.
- Later clean runs win; the plan records the incident but does not roll the tool
  back past a later repair.
- Only assignments whose current `run_id` exactly equals the incident run may be
  proposed for superseding.
- Polluted `product_profiles` are a separate effect because the old classifier
  upserted the profile before returning `entity_not_eligible:non_product`.
- The plan is not apply-ready while any active reprocess request or ambiguous
  member remains.

## Required freeze before any future apply

The production runtime must have:

```text
TAXONOMY_RECHECK_AUTO_NON_PRODUCT=0
```

The current standard backlog skips tools with a prior terminal Shadow run, and
the anomaly scanner requires administrator approval before it creates a
reprocess request. Existing queued/running reprocess requests are still reported
as blockers and must be resolved before applying a matching plan.

Any future apply command must reload the same current-state fields and require an
exact `rollback_plan_hash` match. It should execute no more than 50 tools per
batch and verify the hard invariants after each batch.

## Production apply result

Applied on 2026-08-15 as incident `INC-2026-08-EXAMPLE-COM` after D1 migration
`0065_taxonomy_incident_rollback_audit.sql` created the immutable incident,
member, invalidation and batch ledgers.

- Frozen source candidates: **1,371**
- Confirmed incident members: **1,369**
- Excluded as real page evidence: Clone My Voice (#3551) and Macaron/MidReal
  (#3782)
- Locked rollback plan hash:
  `db26bed84ed44426daa83731b232a71726524f15f6ca4ef999b91e64509a3dba`
- Restored from a previous clean automatic decision: **7**
- Reset from polluted `non_product` to `unresolved`: **1,209**
- Already repaired by a later clean run: **141**
- No current entity effect / audit-only: **12** (two still required polluted
  profile invalidation)
- Invalidated historical run effects: **1,369**
- Entity current-state effects: **1,216**
- Polluted materialized profiles removed after full audit copy: **1,218**
- Preserved legacy mappings: **1,366** tools
- D1 atomic batches: **55**, covering **1,369** members, failed batches: **0**
- Paid model calls and reclassification requests created by this apply: **0**

Final production verification returned zero entity mismatches, zero remaining
materialized polluted profiles, zero missing historical runs, zero invalid
profile audit documents and zero active reclassification requests. The incident
closed at `2026-08-15T05:40:24.452Z`.

`taxonomy_incident_apply.py` is resumable. Before the first write it requires a
fresh whole-plan hash match. Once frozen, a resume verifies the incident and
every member's plan-item hash instead, because successfully applied canary rows
are expected to change the live projection. Every pending member also guards
against current-state drift, active reprocess requests, manual entity decisions
and classification runs created after the incident freeze. The immutable
`classification_runs` rows are never updated or deleted.

## D1 recovery coordinates

- Immediately before migration/apply:
  `0000400c-00039f49-000050c8-8a222ff88b52a5d96755d7be113d7377`
- Immediately after verified completion:
  `0000400c-0003b462-000050c8-a501ef1fd250a8f8c33de558f55cfcb4`

Time Travel restore is an emergency database-wide action, not a normal rollback
step. If it is ever required, first re-check the bookmark and production state;
the Wrangler form is:

```powershell
npx wrangler d1 time-travel restore ainav `
  --config wrangler.toml `
  --bookmark <approved-bookmark>
```
