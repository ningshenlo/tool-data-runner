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
