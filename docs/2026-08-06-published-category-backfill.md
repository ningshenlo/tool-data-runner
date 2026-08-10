# Published Category Backfill — Work Log

> Date: 2026-08-06
> Author: Grok (continuing incomplete Codex session)
> Scope: `tool-data-runner`
> Audience: future humans / AI agents reviewing or extending this work

---

## 1. Why this exists

Codex (2026-08-05 session under `E:\桌面\ai_system`) improved hierarchical category classification in the assets pipeline, then inventoryed production:

- ~3,256 **published** tools still had **legacy** categories (no `raw_output` / `classified_at` provenance)
- Normal `--assets` mode **skips** tools that already have `primary_category_id` / `tool_categories`
- User directed: **backfill published only; do not touch rejected**
- Codex planned a resumable published backfill, then hit usage limit while editing `published_legacy_category_tasks` — **no production writes, no completed script**

This work finishes that planned job.

---

## 2. What was implemented

### CLI

```bash
python runner.py --backfill-published-categories [--limit N] [--dry-run] [--task-id TOOL_ID ...]
```

Mutually exclusive with `--pricing` / `--assets` / `--domain-state` / `--backfill-traffic-monthly` / `--all`.
Cannot combine with `--loop`.

### Core code (`runner.py`)

| Symbol | Role |
|--------|------|
| `PUBLISHED_CATEGORY_BACKFILL_VERSION` (`published-legacy-v1`) | Resume marker in JSON raw |
| `annotate_published_category_backfill_raw` | Stamps `backfill` + accepted slugs onto raw |
| `published_category_backfill_success` | Apply gate: L1 required; L2 unmatched → fail; empty L2 OK |
| `raw_has_published_category_backfill` | Detect completed backfill from raw text/JSON |
| `D1AssetStore.published_legacy_category_tasks` | Candidate query (published + legacy + not manual + not done) |
| `D1AssetStore.load_tool_category_snapshot` | Pre-image for `tool_change_log` |
| `D1AssetStore.apply_published_category_backfill` | Atomic replace via D1 `batch` |
| `D1AssetStore.record_published_category_backfill_failure` | Audit/error only; **no** live category delete |
| `backfill_published_categories` | Orchestration loop (Browser Run classify → apply/fail) |

### Apply path (success)

Single D1 batch:

1. `tool_change_log` row (`change_type = 'category_backfill'`, old/new JSON)
2. `DELETE` non-manual `tool_categories` for the tool
3. `INSERT` parent + child (if any) with `source='auto'`, raw, `classified_at`
4. `UPDATE tools`: force `primary_category_id`, `category_classification_status='auto_ok'`, raw with backfill marker
5. `tool_category_classification_events` `outcome='auto_ok'`

### Fail path

- Live `tool_categories` / `primary_category_id` **unchanged**
- Optional `category_classification_last_error` / raw update
- Event `outcome='auto_failed'`

### Candidate filters

Included:

- `tools.status = 'published'`
- Has primary or at least one `tool_categories` row

Excluded:

- Any non-published status (including `rejected`)
- Any `tool_categories.source = 'manual'`
- Already done: `auto_ok` raw with `$.backfill = published-legacy-v1` **or** hierarchical `prompt_version` match

---

## 3. Production smoke test (2026-08-06)

| Item | Value |
|------|-------|
| Tool | `id=4505` / `praiseengine-64107a58` / https://www.praiseengine.com/ |
| Before | `social-creator` + `social-content-production`, primary=36, status=null |
| After | `marketing-seo` + `seo-content-growth`, primary=35, `auto_ok`, `backfill=published-legacy-v1` |
| Audit | `tool_change_log.change_type=category_backfill`; event `auto_ok` |
| Model path | gpt-oss L1/L2 returned empty (`finish_reason=length` @ 256 completion tokens) → automatic per-model retry → Llama 3.3 70B succeeded |

Commands used:

```bash
wrangler d1 execute ainav --remote --command "..."
python runner.py --backfill-published-categories --dry-run --task-id 4505
python runner.py --backfill-published-categories --task-id 4505
```

### Follow-up fixes discovered during the smoke test

1. **Response unwrapping**: gpt-oss returns OpenAI `choices[0].message.content` (or only `reasoning`) instead of a flat schema object → `normalize_structured_json_payload` / `extract_json_object_from_text`.
2. **Empty-schema fallback**: Browser Run does **not** advance `custom_ai` when the primary model returns HTTP 200 with truncated/empty content. `fetch_structured_asset_data` now walks models one-by-one and retries when required fields are blank.
3. Browser Run currently appears to **hard-cap completions near 256 tokens**, so gpt-oss often exhausts the budget on reasoning when the full L1 catalog prompt is large; Llama fallback keeps the job usable.

## 4. What was intentionally not done

- Did **not** bulk-run the remaining ~3,255 published tools
- Did **not** reprocess `rejected` tools
- Did **not** auto-overwrite Admin manual categories
- Did **not** clear categories before model success (avoids empty-catalog windows)

---

## 4. Prerequisites before production run

1. Deploy / apply ainav migrations if not already:
   - `0029_category_classification_quality.sql`
   - `0030_category_classification_workflow.sql`
2. Ensure runner env has Browser Rendering + Workers AI tokens for category models.
3. Prefer a dry-run / single-tool live verify first:

```bash
python runner.py --backfill-published-categories --dry-run --task-id <published_tool_id>
python runner.py --backfill-published-categories --task-id <published_tool_id>
python runner.py --backfill-published-categories --limit 20
# repeat with higher limits; already-done tools are skipped
```

---

## 5. How to verify (for reviewers)

### Unit tests

```bash
cd tool-data-runner
python -m unittest test_runner_stores.RunnerStoreLifecycleTests.test_published_legacy_category_candidate_filters \
  test_runner_stores.RunnerStoreLifecycleTests.test_published_category_backfill_replaces_atomically_and_is_resumable \
  test_runner_stores.RunnerStoreLifecycleTests.test_published_category_backfill_failure_keeps_live_categories \
  test_runner_stores.RunnerStoreLifecycleTests.test_published_category_backfill_success_helper -v
```

### Production SQL checks (examples)

```sql
-- remaining candidates (approximate)
SELECT COUNT(*) FROM tools t
WHERE t.status = 'published'
  AND (t.primary_category_id IS NOT NULL
       OR EXISTS (SELECT 1 FROM tool_categories tc WHERE tc.tool_id = t.id))
  AND NOT EXISTS (
    SELECT 1 FROM tool_categories tc
    WHERE tc.tool_id = t.id AND tc.source = 'manual'
  )
  AND NOT (
    t.category_classification_status = 'auto_ok'
    AND json_extract(t.category_classification_raw, '$.backfill') = 'published-legacy-v1'
  );

-- audit trail
SELECT COUNT(*) FROM tool_change_log WHERE change_type = 'category_backfill';
SELECT COUNT(*) FROM tool_category_classification_events
WHERE outcome = 'auto_ok'
  AND json_extract(raw_output, '$.backfill') = 'published-legacy-v1';
```

### Resume semantics

Re-running the same command after a partial batch should **skip** tools whose raw already contains the backfill marker; failed tools remain eligible for retry.

---

## 6. Related files

| Path | Change |
|------|--------|
| `tool-data-runner/runner.py` | Backfill implementation + CLI |
| `tool-data-runner/test_runner_stores.py` | Unit tests for filters / apply / fail |
| `tool-data-runner/README.md` | Operator docs for the new mode |
| `tool-data-runner/docs/2026-08-06-published-category-backfill.md` | This work log |
| Codex session (context only) | `~/.codex/sessions/2026/08/05/rollout-2026-08-05T15-41-50-019fd0df-*.jsonl` |

---

## 7. Suggested next steps (not done here)

1. Single-tool live verify on production
2. Batch 20 → 100 → full published residual
3. Spot-check Admin for tools where L1 changed vs old snapshot in `tool_change_log`
4. Optionally mark pre-backfill rows as `source='legacy'` in a one-shot SQL (Codex plan item 1; separate from apply path)
5. Commit hierarchical classification + this backfill together when ready
