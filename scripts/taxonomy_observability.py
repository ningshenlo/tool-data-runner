"""Read-only taxonomy observability report (cache hit rate, escalations, gaps)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner  # noqa: E402

REQUESTS_BY_STAGE_SQL = """
SELECT stage, model, status,
       CASE WHEN request_json LIKE '%taxonomy_single_call%' THEN 'single_call' ELSE 'legacy' END AS mode,
       COUNT(*) AS request_count,
       COALESCE(SUM(input_tokens), 0) AS input_tokens,
       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
       COALESCE(SUM(output_tokens), 0) AS output_tokens,
       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
FROM taxonomy_batch_requests
WHERE created_at >= ?
GROUP BY stage, model, status, CASE WHEN request_json LIKE '%taxonomy_single_call%' THEN 'single_call' ELSE 'legacy' END
ORDER BY stage, model, status, mode
""".strip()

ITEMS_BY_STATUS_SQL = """
SELECT status, current_stage, COUNT(*) AS item_count
FROM taxonomy_batch_items
WHERE updated_at >= ?
GROUP BY status, current_stage
ORDER BY status, current_stage
""".strip()

TAXONOMY_GAPS_SQL = """
SELECT error, COUNT(*) AS gap_count, MAX(updated_at) AS latest_at
FROM taxonomy_batch_items
WHERE updated_at >= ? AND error LIKE 'taxonomy_gap:%'
GROUP BY error
ORDER BY gap_count DESC, latest_at DESC
LIMIT 50
""".strip()

SECONDARY_ASSIGNMENTS_SQL = """
SELECT decision_status, COUNT(*) AS assignment_count
FROM product_taxonomy_assignments
WHERE is_primary = 0 AND updated_at >= ?
GROUP BY decision_status
ORDER BY decision_status
""".strip()

SINGLE_CALL_ITEM_COUNT_SQL = """
SELECT COUNT(DISTINCT item_id) AS item_count
FROM taxonomy_batch_requests
WHERE created_at >= ? AND stage = 'profile' AND request_json LIKE '%taxonomy_single_call%'
""".strip()

SINGLE_CALL_ESCALATED_COUNT_SQL = """
SELECT COUNT(DISTINCT r_esc.item_id) AS item_count
FROM taxonomy_batch_requests r_esc
WHERE r_esc.created_at >= ?
  AND r_esc.stage IN ('l1_escalation', 'leaf_escalation')
  AND EXISTS (
    SELECT 1 FROM taxonomy_batch_requests r_prof
    WHERE r_prof.item_id = r_esc.item_id
      AND r_prof.stage = 'profile'
      AND r_prof.request_json LIKE '%taxonomy_single_call%'
  )
""".strip()

OBSERVABILITY_QUERIES: tuple[tuple[str, str], ...] = (
    ("requests_by_stage", REQUESTS_BY_STAGE_SQL),
    ("items_by_status", ITEMS_BY_STATUS_SQL),
    ("taxonomy_gaps", TAXONOMY_GAPS_SQL),
    ("secondary_assignments", SECONDARY_ASSIGNMENTS_SQL),
    ("single_call_items", SINGLE_CALL_ITEM_COUNT_SQL),
    ("single_call_escalated", SINGLE_CALL_ESCALATED_COUNT_SQL),
)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


def default_since_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def collect_taxonomy_observability(
    d1: Any,
    *,
    since_iso: str,
) -> dict[str, Any]:
    """Query read-only taxonomy batch token, cache, escalation, gap, and secondary stats."""
    request_rows = await d1.query(REQUESTS_BY_STAGE_SQL, [since_iso])
    item_rows = await d1.query(ITEMS_BY_STATUS_SQL, [since_iso])
    gap_rows = await d1.query(TAXONOMY_GAPS_SQL, [since_iso])
    secondary_rows = await d1.query(SECONDARY_ASSIGNMENTS_SQL, [since_iso])
    single_call_rows = await d1.query(SINGLE_CALL_ITEM_COUNT_SQL, [since_iso])
    escalated_rows = await d1.query(SINGLE_CALL_ESCALATED_COUNT_SQL, [since_iso])

    formatted_requests: list[dict[str, Any]] = []
    tot_req = 0
    tot_in = 0
    tot_cached = 0
    tot_out = 0
    tot_reason = 0
    for row in request_rows:
        req_count = int(row.get("request_count") or 0)
        in_tok = int(row.get("input_tokens") or 0)
        cached_tok = int(row.get("cached_input_tokens") or 0)
        out_tok = int(row.get("output_tokens") or 0)
        reason_tok = int(row.get("reasoning_tokens") or 0)
        tot_req += req_count
        tot_in += in_tok
        tot_cached += cached_tok
        tot_out += out_tok
        tot_reason += reason_tok
        formatted_requests.append(
            {
                "stage": row.get("stage"),
                "model": row.get("model"),
                "status": row.get("status"),
                "mode": row.get("mode"),
                "request_count": req_count,
                "input_tokens": in_tok,
                "cached_input_tokens": cached_tok,
                "cache_hit_rate": _ratio(cached_tok, in_tok),
                "output_tokens": out_tok,
                "reasoning_tokens": reason_tok,
                "reasoning_ratio": _ratio(reason_tok, out_tok),
                "avg_input_tokens": round(in_tok / req_count, 1) if req_count else 0.0,
                "avg_output_tokens": round(out_tok / req_count, 1) if req_count else 0.0,
            }
        )

    sc_items = int((single_call_rows[0] if single_call_rows else {}).get("item_count") or 0)
    sc_escalated = int((escalated_rows[0] if escalated_rows else {}).get("item_count") or 0)
    return {
        "since": since_iso,
        "totals": {
            "request_count": tot_req,
            "input_tokens": tot_in,
            "cached_input_tokens": tot_cached,
            "cache_hit_rate": _ratio(tot_cached, tot_in),
            "output_tokens": tot_out,
            "reasoning_tokens": tot_reason,
            "reasoning_ratio": _ratio(tot_reason, tot_out),
        },
        "single_call_summary": {
            "item_count": sc_items,
            "escalated_item_count": sc_escalated,
            "escalation_rate": _ratio(sc_escalated, sc_items),
        },
        "requests_by_stage": formatted_requests,
        "items_by_status": [
            {
                "status": row.get("status"),
                "current_stage": row.get("current_stage"),
                "item_count": int(row.get("item_count") or 0),
            }
            for row in item_rows
        ],
        "taxonomy_gaps": [
            {
                "error": row.get("error"),
                "gap_count": int(row.get("gap_count") or 0),
                "latest_at": row.get("latest_at"),
            }
            for row in gap_rows
        ],
        "secondary_assignments": [
            {
                "decision_status": row.get("decision_status"),
                "assignment_count": int(row.get("assignment_count") or 0),
            }
            for row in secondary_rows
        ],
    }


async def _run(since_iso: str) -> dict[str, Any]:
    async with runner.D1Client(runner.load_config(require_brightdata=False)) as d1:
        return await collect_taxonomy_observability(d1, since_iso=since_iso)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--since", default="", help="Explicit UTC ISO lower bound")
    args = parser.parse_args()
    since = str(args.since).strip() or default_since_iso(args.days)
    try:
        report = asyncio.run(_run(since))
        print(json.dumps(report, indent=2, ensure_ascii=False))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "reason": str(error)[:1200],
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
