"""Lightweight static pricing extractor powered by Cloudflare Workers AI Clairic."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from libs.logging_util import log_error, log_info
from shared.db.client import PostgresClient, PostgresStatement
from workloads.enrichment.pricing_locator import LocatedPricingPage, locate_pricing_page

CLAIRIC_MODEL = "@cf/cloudflare/clairic"
CLAIRIC_API_BASE = "https://api.cloudflare.com/client/v4"
CLAIRIC_MAX_P_NULL = 0.35

CURRENCY_USD_RATE: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.72,
    "AUD": 0.65,
}


@dataclass(frozen=True)
class StaticPricingResult:
    pricing_model: str
    pricing_interval: str
    pricing_currency_code: str | None
    starting_price_minor: int | None
    starting_price_usd_minor: int | None
    has_free_plan: int
    has_free_trial: int
    pricing_notes: str | None
    pricing_url: str | None
    locator_status: str
    locator_method: str
    price_evidence: str | None = None
    p_null_amount: float | None = None


def current_snapshot_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def build_clairic_pricing_request(page_text: str, page_url: str) -> dict[str, Any]:
    """Build a bounded @cf/cloudflare/clairic extraction payload."""
    bounded_text = f"URL: {page_url}\n\n{(page_text or '').strip()[:6000]}"
    return {
        "model": "clairic",
        "state": bounded_text,
        "fields": {
            "pricing_model": {
                "type": "enum",
                "enum": ["free", "freemium", "paid", "usage_based", "contact", "unknown"],
                "instructions": (
                    "Product pricing model: free if completely free, freemium if both free tier and paid plans exist, "
                    "paid if only paid plans exist, usage_based if pay-as-you-go/credits/per-token without fixed tier, "
                    "contact if custom/contact-sales only, unknown if unclear."
                ),
            },
            "has_free_plan": {
                "type": "boolean",
                "instructions": "True only if a permanent free tier/plan ($0) is explicitly offered.",
            },
            "has_free_trial": {
                "type": "boolean",
                "instructions": "True only if a time-limited free trial or starter trial credits are explicitly offered.",
            },
            "starting_price_amount": {
                "type": "number",
                "minimum": 0.01,
                "instructions": (
                    "Lowest non-zero paid plan recurring/starting price amount shown on the page "
                    "(ignore $0 free plan, crossed-out original prices, or enterprise custom quotes)."
                ),
            },
            "pricing_currency": {
                "type": "text",
                "instructions": "Currency symbol ($, €, £) or 3-letter ISO code (USD, EUR, GBP) of starting_price_amount.",
            },
            "billing_interval": {
                "type": "enum",
                "enum": ["month", "year", "one_time", "usage", "unknown"],
                "instructions": "Billing interval corresponding to starting_price_amount.",
            },
            "price_evidence": {
                "type": "text",
                "instructions": "Short verbatim span from state proving the starting price or pricing model.",
            },
        },
    }


def normalize_currency_code(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if "$" in text or "USD" in text or "US$" in text:
        return "USD"
    if "€" in text or "EUR" in text:
        return "EUR"
    if "£" in text or "GBP" in text:
        return "GBP"
    if "CAD" in text or "CA$" in text:
        return "CAD"
    if "AUD" in text or "A$" in text:
        return "AUD"
    match = re.fullmatch(r"[A-Z]{3}", text)
    return text if match else None


def _read_answer(answers: dict[str, Any], key: str) -> tuple[Any, float | None, bool]:
    item = answers.get(key)
    if not isinstance(item, dict):
        return None, None, False
    valid = item.get("valid", True) is not False
    p_null_raw = item.get("p_null")
    p_null = float(p_null_raw) if isinstance(p_null_raw, (int, float)) else None
    return item.get("value"), p_null, valid


def normalize_db_pricing_interval(raw_interval: Any, *, pricing_model: str = "unknown") -> str:
    """Map extracted billing_interval to PostgreSQL `tools_pricing_interval_check` enum."""
    val = str(raw_interval or "").strip().lower()
    if val in {"month", "monthly"}:
        return "monthly"
    if val in {"year", "yearly", "annual"}:
        return "yearly"
    if val in {"usage", "usage_based"}:
        return "usage"
    if val == "custom" or pricing_model == "contact":
        return "custom"
    return "none"


def format_pricing_notes(
    *,
    pricing_model: str,
    pricing_interval: str,
    currency_code: str | None,
    starting_price_minor: int | None,
    has_free_plan: bool,
    has_free_trial: bool,
    snapshot_month: str,
) -> str | None:
    if pricing_model == "unknown" and not has_free_plan and not has_free_trial and starting_price_minor is None:
        return None

    parts: list[str] = []
    if pricing_model == "free":
        parts.append("Free")
    elif pricing_model == "freemium":
        parts.append("Freemium (free tier available)")
    elif pricing_model == "usage_based":
        parts.append("Usage-based pricing")
    elif pricing_model == "contact":
        parts.append("Contact sales / custom pricing")
    elif pricing_model == "paid":
        parts.append("Paid plans")
    elif has_free_plan:
        parts.append("Free tier available")

    if starting_price_minor is not None and starting_price_minor > 0:
        amount = starting_price_minor / 100.0
        symbol = "$" if (currency_code or "USD") == "USD" else f"{currency_code} "
        num_str = f"{amount:.0f}" if amount.is_integer() else f"{amount:.2f}"
        suffix = {
            "monthly": "/mo",
            "yearly": "/yr",
            "month": "/mo",
            "year": "/yr",
            "one_time": " one-time",
            "usage": "",
        }.get(pricing_interval, "")
        parts.append(f"from {symbol}{num_str}{suffix}")

    if has_free_trial and pricing_model not in {"free", "contact"}:
        parts.append("free trial")

    if not parts:
        return None
    return f"{' · '.join(parts)} (checked {snapshot_month})."


def normalize_clairic_pricing_result(
    raw_response: dict[str, Any],
    *,
    locator: LocatedPricingPage,
    snapshot_month: str | None = None,
    max_p_null: float = CLAIRIC_MAX_P_NULL,
) -> StaticPricingResult:
    """Normalize raw Clairic response with strict p_null guards against hallucinations."""
    month = snapshot_month or current_snapshot_month()
    payload = raw_response.get("result") if isinstance(raw_response.get("result"), dict) else raw_response
    answers = payload.get("answers") if isinstance(payload, dict) and isinstance(payload.get("answers"), dict) else {}

    model_val, model_p_null, model_valid = _read_answer(answers, "pricing_model")
    pricing_model = str(model_val or "unknown").strip().lower()
    if not model_valid or pricing_model not in {"free", "freemium", "paid", "usage_based", "contact", "unknown"}:
        pricing_model = "unknown"
    if model_p_null is not None and model_p_null > 0.55:
        pricing_model = "unknown"

    free_val, free_p_null, free_valid = _read_answer(answers, "has_free_plan")
    has_free_plan = bool(free_valid and free_val is True and (free_p_null is None or free_p_null <= 0.45))

    trial_val, trial_p_null, trial_valid = _read_answer(answers, "has_free_trial")
    has_free_trial = bool(trial_valid and trial_val is True and (trial_p_null is None or trial_p_null <= 0.45))

    amt_val, amt_p_null, amt_valid = _read_answer(answers, "starting_price_amount")
    cur_val, cur_p_null, cur_valid = _read_answer(answers, "pricing_currency")
    int_val, int_p_null, int_valid = _read_answer(answers, "billing_interval")
    ev_val, _, ev_valid = _read_answer(answers, "price_evidence")

    starting_price_minor: int | None = None
    starting_price_usd_minor: int | None = None
    currency_code: str | None = None
    note_interval = "none"

    if (
        amt_valid
        and isinstance(amt_val, (int, float))
        and not isinstance(amt_val, bool)
        and 0 < float(amt_val) <= 100_000
        and (amt_p_null is None or amt_p_null <= max_p_null)
    ):
        starting_price_minor = int(round(float(amt_val) * 100))
        currency_code = normalize_currency_code(cur_val) if cur_valid and (cur_p_null is None or cur_p_null <= 0.5) else "USD"
        if currency_code is None:
            currency_code = "USD"
        rate = CURRENCY_USD_RATE.get(currency_code)
        if rate is not None:
            starting_price_usd_minor = int(round(starting_price_minor * rate))

        raw_interval = str(int_val or "").strip().lower() if int_valid and (int_p_null is None or int_p_null <= 0.5) else "none"
        note_interval = raw_interval if raw_interval in {"month", "monthly", "year", "yearly", "one_time", "usage"} else "none"

    if locator.status == "contact_sales" and starting_price_minor is None and pricing_model == "unknown":
        pricing_model = "contact"

    if pricing_model == "free" and starting_price_minor is not None:
        pricing_model = "freemium"
        has_free_plan = True
    elif pricing_model == "paid" and has_free_plan:
        pricing_model = "freemium"
    elif pricing_model == "free":
        has_free_plan = True

    pricing_interval = normalize_db_pricing_interval(note_interval, pricing_model=pricing_model)

    notes = format_pricing_notes(
        pricing_model=pricing_model,
        pricing_interval=note_interval,
        currency_code=currency_code,
        starting_price_minor=starting_price_minor,
        has_free_plan=has_free_plan,
        has_free_trial=has_free_trial,
        snapshot_month=month,
    )
    evidence = str(ev_val).strip()[:200] if ev_valid and isinstance(ev_val, str) and ev_val.strip() else None

    return StaticPricingResult(
        pricing_model=pricing_model,
        pricing_interval=pricing_interval,
        pricing_currency_code=currency_code,
        starting_price_minor=starting_price_minor,
        starting_price_usd_minor=starting_price_usd_minor,
        has_free_plan=1 if has_free_plan else 0,
        has_free_trial=1 if has_free_trial else 0,
        pricing_notes=notes,
        pricing_url=locator.final_url if locator.status != "not_found" else None,
        locator_status=locator.status,
        locator_method=locator.method,
        price_evidence=evidence,
        p_null_amount=round(amt_p_null, 4) if amt_p_null is not None else None,
    )


def _run_clairic_via_cf_cli(body: dict[str, Any]) -> dict[str, Any]:
    """Run `@cf/cloudflare/clairic` through authenticated `cf` CLI when REST token lacks AI scope."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
        json.dump(body, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    try:
        arg_path = tmp_path.replace("\\", "/")
        # Run from temp dir without CLOUDFLARE_API_TOKEN so `cf` uses its OAuth session instead of project `.env`.
        blocked = {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY", "CLOUDFLARE_EMAIL"}
        child_env = {k: v for k, v in os.environ.items() if k.upper() not in blocked}
        proc = subprocess.run(
            ["cf", "ai", "run", CLAIRIC_MODEL, "--body", f"@{arg_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            shell=(os.name == "nt"),
            cwd=tempfile.gettempdir(),
            env=child_env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cf_ai_run_failed:{(proc.stderr or proc.stdout)[:240]}")
        stdout = (proc.stdout or "").strip()
        start = stdout.find("{")
        if start < 0:
            raise RuntimeError(f"cf_ai_run_non_json:{stdout[:200]}")
        parsed = json.loads(stdout[start:])
        if not isinstance(parsed, dict):
            raise RuntimeError("cf_ai_run_invalid_json_type")
        return parsed
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


async def call_clairic_pricing(
    body: dict[str, Any],
    *,
    account_id: str,
    api_token: str,
    http_client: httpx.AsyncClient,
    model_id: str = CLAIRIC_MODEL,
) -> dict[str, Any]:
    """Invoke Cloudflare Clairic via REST API, falling back to `cf` CLI if locally authenticated."""
    transport_pref = os.getenv("CLAIRIC_TRANSPORT", "").strip().lower()
    if transport_pref == "cf_cli":
        return await asyncio.to_thread(_run_clairic_via_cf_cli, body)

    if account_id and api_token:
        url = f"{CLAIRIC_API_BASE}/accounts/{account_id}/ai/run/{model_id}"
        resp = await http_client.post(
            url,
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json=body,
            timeout=35.0,
        )
        if 200 <= resp.status_code < 300:
            return resp.json()
        if resp.status_code in {401, 403} and transport_pref != "rest_only":
            return await asyncio.to_thread(_run_clairic_via_cf_cli, body)
        raise RuntimeError(f"clairic_http_{resp.status_code}:{resp.text[:240]}")

    if transport_pref == "rest_only":
        raise RuntimeError("Missing CLOUDFLARE_ACCOUNT_ID or Workers AI token.")
    return await asyncio.to_thread(_run_clairic_via_cf_cli, body)


async def select_static_pricing_candidates(
    pg: PostgresClient,
    limit: int,
    *,
    tool_ids: list[int] | None = None,
    after_tool_id: int = 0,
) -> list[dict[str, Any]]:
    effective_limit = max(1, min(500, int(limit)))
    id_filter = ""
    params: list[Any] = []
    order_clause = (
        "CASE WHEN t.seo_index_status = 'indexable' THEN 0 ELSE 1 END ASC,\n"
        "          CASE WHEN kp.known_pricing_url IS NOT NULL THEN 0 ELSE 1 END ASC,\n"
        "          t.id ASC"
    )
    if tool_ids:
        clean_ids = [int(tid) for tid in tool_ids if int(tid) > 0]
        if not clean_ids:
            return []
        id_filter = "AND t.id = ANY(%s)"
        params.append(clean_ids)
    elif after_tool_id > 0:
        id_filter = "AND t.id > %s"
        params.append(int(after_tool_id))
        order_clause = "t.id ASC"
    params.append(effective_limit)

    sql = f"""
        WITH known_pricing AS (
          SELECT DISTINCT ON (tool_id)
            tool_id,
            url AS known_pricing_url
          FROM pricing_sources
          WHERE url IS NOT NULL
            AND url <> ''
            AND discovery_status = 'found'
          ORDER BY tool_id, id DESC
        )
        SELECT
          t.id AS tool_id,
          t.canonical_slug,
          t.normalized_domain,
          t.official_url,
          t.seo_index_status,
          kp.known_pricing_url
        FROM tools t
        LEFT JOIN known_pricing kp ON kp.tool_id = t.id
        WHERE t.status IN ('published', 'pending_enrich')
          AND t.duplicate_of_tool_id IS NULL
          AND coalesce(t.pricing_model, 'unknown') = 'unknown'
          {id_filter}
        ORDER BY
          {order_clause}
        LIMIT %s
    """
    return await pg.query(PostgresStatement(sql=sql, params=tuple(params), label="static_pricing.select_candidates"))


async def persist_static_pricing(
    pg: PostgresClient,
    *,
    tool_id: int,
    result: StaticPricingResult,
) -> None:
    """Write lightweight static pricing fields to `tools` and `tool_localizations` only."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    statements = [
        PostgresStatement(
            sql="""
                UPDATE tools
                SET pricing_model = %s,
                    pricing_interval = %s,
                    pricing_currency_code = %s,
                    starting_price_minor = %s,
                    starting_price_usd_minor = %s,
                    has_free_plan = %s,
                    has_free_trial = %s,
                    updated_at = %s
                WHERE id = %s
            """,
            params=(
                result.pricing_model,
                result.pricing_interval,
                result.pricing_currency_code,
                result.starting_price_minor,
                result.starting_price_usd_minor,
                result.has_free_plan,
                result.has_free_trial,
                now,
                tool_id,
            ),
            label="static_pricing.update_tool",
        ),
    ]
    if result.pricing_notes:
        statements.append(
            PostgresStatement(
                sql="""
                    UPDATE tool_localizations
                    SET pricing_notes = %s,
                        updated_at = %s
                    WHERE tool_id = %s
                      AND locale_code = 'en'
                """,
                params=(result.pricing_notes, now, tool_id),
                label="static_pricing.update_en_notes",
            )
        )
    await pg.batch(statements)


async def run_static_pricing_extraction(
    config: Any,
    *,
    limit: int = 20,
    concurrency: int = 4,
    dry_run: bool = False,
    tool_ids: list[int] | None = None,
    after_tool_id: int = 0,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Locate pricing pages via multi-rule scoring and extract lightweight pricing with Clairic."""
    resolved_db_url = (database_url or os.getenv("DATABASE_URL") or "").strip()
    if not resolved_db_url:
        raise RuntimeError("Missing DATABASE_URL for static pricing extraction.")

    account_id = str(getattr(config, "cloudflare_account_id", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")).strip()
    ai_token = (
        os.getenv("CLOUDFLARE_WORKERS_AI_API_TOKEN", "").strip()
        or str(getattr(config, "taxonomy_classification_api_token", "") or "").strip()
        or str(getattr(config, "cloudflare_api_token", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")).strip()
    )
    model_id = os.getenv("CLAIRIC_MODEL", CLAIRIC_MODEL).strip() or CLAIRIC_MODEL
    semaphore = asyncio.Semaphore(max(1, min(8, int(concurrency))))

    summary: dict[str, Any] = {
        "selected": 0,
        "located_known": 0,
        "located_discovered": 0,
        "located_homepage": 0,
        "located_contact": 0,
        "not_found": 0,
        "extracted": 0,
        "updated": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "samples": [],
    }

    async with PostgresClient(resolved_db_url, max_connections=min(4, max(1, int(concurrency)))) as pg:
        candidates = await select_static_pricing_candidates(
            pg,
            limit,
            tool_ids=tool_ids,
            after_tool_id=after_tool_id,
        )
        summary["selected"] = len(candidates)
        summary["last_tool_id"] = max((int(r["tool_id"]) for r in candidates), default=0)
        if not candidates:
            return summary

        async with httpx.AsyncClient(timeout=15.0) as http_client:
            async def process_one(row: dict[str, Any]) -> dict[str, Any]:
                tool_id = int(row["tool_id"])
                domain = str(row["normalized_domain"])
                official_url = str(row["official_url"] or f"https://{domain}")
                known_url = row.get("known_pricing_url")
                async with semaphore:
                    try:
                        located = await locate_pricing_page(
                            official_url,
                            known_pricing_url=str(known_url) if known_url else None,
                            client=http_client,
                        )
                        if located.status == "not_found" or not located.text:
                            return {
                                "status": "not_found",
                                "tool_id": tool_id,
                                "domain": domain,
                                "locator_status": located.status,
                                "locator_method": located.method,
                                "error": located.error,
                            }

                        if located.status == "contact_sales" and located.quality_score <= 2:
                            result = StaticPricingResult(
                                pricing_model="contact",
                                pricing_interval="custom",
                                pricing_currency_code=None,
                                starting_price_minor=None,
                                starting_price_usd_minor=None,
                                has_free_plan=0,
                                has_free_trial=0,
                                pricing_notes=f"Contact sales / custom pricing (checked {current_snapshot_month()}).",
                                pricing_url=located.final_url,
                                locator_status=located.status,
                                locator_method=located.method,
                            )
                        else:
                            req_body = build_clairic_pricing_request(located.text, located.final_url)
                            raw_clairic = await call_clairic_pricing(
                                req_body,
                                account_id=account_id,
                                api_token=ai_token,
                                http_client=http_client,
                                model_id=model_id,
                            )
                            result = normalize_clairic_pricing_result(raw_clairic, locator=located)

                        if not dry_run and result.pricing_model != "unknown":
                            await persist_static_pricing(pg, tool_id=tool_id, result=result)
                        return {
                            "status": "extracted",
                            "tool_id": tool_id,
                            "domain": domain,
                            "persisted": bool((not dry_run) and result.pricing_model != "unknown"),
                            **asdict(result),
                        }
                    except Exception as error:
                        log_error("static_pricing.item_failed", tool_id=tool_id, domain=domain, error=str(error)[:300])
                        return {
                            "status": "failed",
                            "tool_id": tool_id,
                            "domain": domain,
                            "error": str(error)[:240],
                        }

            results = await asyncio.gather(*(process_one(row) for row in candidates))

    for item in results:
        lm = item.get("locator_method")
        if lm == "known_source":
            summary["located_known"] += 1
        elif lm == "candidate_scored":
            summary["located_discovered"] += 1
        elif lm == "homepage_section":
            summary["located_homepage"] += 1
        elif lm == "contact_or_demo":
            summary["located_contact"] += 1

        if item.get("status") == "not_found":
            summary["not_found"] += 1
        elif item.get("status") == "extracted":
            summary["extracted"] += 1
            if item.get("persisted"):
                summary["updated"] += 1
        else:
            summary["failed"] += 1
        if len(summary["samples"]) < 20:
            summary["samples"].append(item)

    log_info("static_pricing.done", **{k: v for k, v in summary.items() if k != "samples"})
    return summary
