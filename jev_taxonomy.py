"""JEV execution inside the existing durable taxonomy request/job state machine."""
import asyncio
import json
import os
from typing import Any

import httpx

from jev import JEV_MODEL, JevError
from jev_capabilities import PROMPT_VERSION, build_capability_plan, evaluate_capability_plan
from taxonomy_shadow import profile_evidence_text, utc_now_iso


def enabled(config: Any) -> bool:
    provider = str(getattr(config, "taxonomy_capability_provider", "openai")).strip().lower()
    has_jev_key = bool(str(getattr(config, "jev_api_key", "") or "").strip())
    has_openrouter_key = bool(str(getattr(config, "openrouter_api_key", "") or os.getenv("OPENROUTER_API_KEY", "") or "").strip())
    if provider in {"jev", "openrouter", "pplx", "perplexity"}:
        return has_jev_key or has_openrouter_key
    return has_openrouter_key


def number(config: Any, name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(getattr(config, name, default))
        return max(low, min(high, value)) if value == value else default
    except (ValueError, TypeError):
        return default


def usage_from(responses: list[dict]) -> dict[str, int]:
    usage = dict(input_tokens=0, cached_input_tokens=0, cache_write_tokens=0,
                 output_tokens=0, reasoning_tokens=0, total_tokens=0)
    for response in responses:
        usage["input_tokens"] += response["usage"]["input_tokens"]
        usage["output_tokens"] += response["usage"]["output_tokens"]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


async def recover_interrupted_jobs(d1, config, catalog) -> int:
    import taxonomy_batch as batch
    # A pass has <= 10 products, each bounded to 120 seconds; one hour is beyond that bound.
    jobs = await d1.query("""
        SELECT id FROM taxonomy_batch_jobs WHERE model LIKE 'jev-%'
          AND (status IN ('building', 'in_progress') OR (status='failed' AND error='jev_interrupted'))
          AND updated_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour')
          AND EXISTS (SELECT 1 FROM taxonomy_batch_requests r WHERE r.job_id=taxonomy_batch_jobs.id AND r.status IN ('queued','submitted'))
          LIMIT 10
    """)
    recovered = 0
    for job in jobs:
        claimed = await d1.run("""
            UPDATE taxonomy_batch_jobs SET status='failed', error='jev_interrupted', completed_at=?, updated_at=?
            WHERE id=? AND (status IN ('building','in_progress') OR (status='failed' AND error='jev_interrupted'))
              AND updated_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour')
        """, [utc_now_iso(), utc_now_iso(), job["id"]])
        if not int(claimed.get("changes") or 0):
            continue
        rows = await d1.query("SELECT * FROM taxonomy_batch_requests WHERE job_id=? AND status IN ('queued','submitted')", [job["id"]])
        for row in rows:
            await d1.run("UPDATE taxonomy_batch_requests SET model=?, updated_at=? WHERE id=?", [JEV_MODEL, utc_now_iso(), row["id"]])
            await batch.apply_request_result(d1, config, catalog, batch.ParsedBatchResult(
                custom_id=row["custom_id"], ok=False, response_body={"provider": "jev", "interrupted": True},
                structured_output={}, usage=usage_from([]), error="jev_interrupted"))
            recovered += 1
    return recovered


async def execute_queued_capabilities(d1, config, catalog) -> dict[str, int]:
    import taxonomy_batch as batch
    counts = {"jev_recovered": await recover_interrupted_jobs(d1, config, catalog),
              "jev_completed": 0, "jev_fallback": 0, "jev_requests": 0, "jev_input_tokens": 0}
    if not enabled(config):
        return counts
    rows = await d1.query("""
        SELECT r.* FROM taxonomy_batch_requests r JOIN taxonomy_batch_items i ON i.id=r.item_id
        WHERE r.stage='capability' AND r.status='queued' AND r.job_id IS NULL
          AND i.current_stage='capability' AND i.status='running'
          AND NOT EXISTS (SELECT 1 FROM taxonomy_batch_requests prior
            WHERE prior.item_id=r.item_id AND prior.stage='capability' AND prior.model LIKE 'jev-%')
        ORDER BY r.id LIMIT ?
    """, [int(number(config, "jev_capability_limit", 6, 1, 10))])
    if not rows:
        return counts
    now = utc_now_iso()
    created = await d1.run("""
        INSERT INTO taxonomy_batch_jobs (stage,model,reasoning_effort,status,request_count,created_at,updated_at)
        VALUES ('capability',?,'none','building',?,?,?)
    """, [JEV_MODEL, len(rows), now, now])
    job_id = int(created.get("last_row_id") or 0)
    try:
        await batch._reserve_requests_for_job(d1, job_id=job_id, requests=rows, now=now)
    except Exception:
        await d1.run("UPDATE taxonomy_batch_jobs SET status='failed',error='jev_reservation_failed',completed_at=?,updated_at=? WHERE id=?", [now, now, job_id])
        return counts
    await d1.run("UPDATE taxonomy_batch_jobs SET status='in_progress',submitted_at=?,updated_at=? WHERE id=?", [now, now, job_id])
    await d1.run("UPDATE taxonomy_batch_requests SET model=?,reasoning_effort='none',status='submitted',submitted_at=?,updated_at=? WHERE job_id=? AND status='queued'", [JEV_MODEL, now, now, job_id])
    semaphore = asyncio.Semaphore(int(number(config, "taxonomy_concurrency", 3, 1, 3)))

    async def one(row):
        responses = []
        attempts = []
        plan = {}
        output = {}
        error = ""
        async with semaphore:
            try:
                item = await batch.load_batch_item(d1, row["item_id"])
                if item.get("status") != "running" or item.get("current_stage") != "capability":
                    raise JevError("jev_stale_item")
                profile = batch._json_object(item.get("profile_json"))
                candidates = batch.capability_candidates_for_item(item, catalog, int(getattr(config, "taxonomy_capability_candidate_limit", 96)))
                plan = build_capability_plan(profile, candidates, str(item.get("source_text") or profile_evidence_text(profile)))
                # Persist the actual closed-set request, without the credential, before provider I/O.
                await d1.run("UPDATE taxonomy_batch_requests SET request_json=?,updated_at=? WHERE id=? AND job_id=? AND status='submitted'", [batch._json(plan), utc_now_iso(), row["id"], job_id])
                async with asyncio.timeout(120):
                    async with httpx.AsyncClient() as client:
                        api_key = str(getattr(config, "openrouter_api_key", "") or os.getenv("OPENROUTER_API_KEY", "") or config.jev_api_key)
                        output = await evaluate_capability_plan(plan, client, api_key,
                            threshold=number(config, "jev_min_confidence", 0.85, 0.5, 1),
                            timeout=number(config, "jev_timeout_seconds", 8, 1, 10), responses=responses, attempts=attempts)
            except JevError as exc:
                error = str(exc)
            except TimeoutError:
                error = "jev_timeout"
            except Exception:
                error = "jev_execution_error"
            usage = usage_from(responses)
            result = batch.ParsedBatchResult(custom_id=row["custom_id"], ok=not error,
                response_body={"provider": "jev", "model": JEV_MODEL, "prompt_version": PROMPT_VERSION,
                    "request_sha256": plan.get("request_sha256"), "responses": responses, "attempts": attempts,
                    "usage": usage, "fallback_reason": error or None},
                structured_output=output, usage=usage, error=error)
            # Existing result fencing, grounding, whitelist and manual-assignment protection still apply.
            applied = await batch.apply_request_result(d1, config, catalog, result)
            return applied, error, len(attempts), usage["input_tokens"]

    results = await asyncio.gather(*(one(row) for row in rows))
    for applied, error, calls, tokens in results:
        counts["jev_requests"] += calls
        counts["jev_input_tokens"] += tokens
        if applied:
            counts["jev_fallback" if error else "jev_completed"] += 1
    await d1.run("""
        UPDATE taxonomy_batch_jobs SET status='completed',completed_count=?,failed_count=?,raw_json=?,completed_at=?,updated_at=?
        WHERE id=? AND status='in_progress'
    """, [counts["jev_completed"], counts["jev_fallback"], json.dumps(counts), utc_now_iso(), utc_now_iso(), job_id])
    return counts
