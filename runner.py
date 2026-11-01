from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import random
import re
import string
import sys
import time
import uuid
import xml.etree.ElementTree as ET

# `python runner.py` loads this file as `__main__`. Workloads still do
# `from runner import ...`; alias so Dokploy entrypoints share one module object.
sys.modules.setdefault("runner", sys.modules[__name__])
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from dotenv import load_dotenv
from report_exports import export_ready_reports, shift_month
from market_snapshot_refresh import refresh_completed_market_snapshot
from search_demand_refresh import refresh_completed_search_demand
from anti_bot_signatures import detect_anti_bot_page
from taxonomy_precision import MIN_AUTO_PRIMARY_CONFIDENCE
from taxonomy_schema import strict_json_schema
from shared.readiness import (
    accepted_primary_taxonomy_predicate,
    tool_auto_publish_admission_predicate,
    tool_live_ready_predicate,
)

from config.constants import (
    AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
    AHREFS_DOMAIN_RATING_URL,
    AHREFS_MAX_REQUESTS_PER_MINUTE,
    ASSET_DB_STORAGE_BUCKET,
    ASSET_DEAD_LETTER_REVIVE_HOURS,
    ASSET_REQUIREMENT_ORDER,
    ASSET_SOURCE,
    AUTO_APPROVE_TOOL_NAME_CONFIDENCE,
    BROWSER_USER_AGENT,
    CATALOG_AUTO_PUBLISH_MAX_LIMIT,
    CATALOG_AUTO_PUBLISH_POLICY_VERSION,
    COMMON_THREE_LABEL_SUFFIXES,
    D1_API_BASE,
    DEFAULT_R2_BUCKET,
    DEFAULT_TAXONOMY_CLASSIFICATION_FALLBACK_MODEL,
    DEFAULT_TAXONOMY_CLASSIFICATION_MODEL,
    DOMAIN_STATE_SOURCE,
    DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL,
    DOMAIN_TRAFFIC_COUNTRY_REFRESH_ESTIMATES_SQL,
    DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL,
    DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL,
    HOMEPAGE_BROWSER_6000_RETRY_DELAY_SECONDS,
    HOMEPAGE_MAX_REDIRECTS,
    IANA_RDAP_DNS,
    MAX_HOMEPAGE_HTML_BYTES,
    RDAP_USER_AGENT,
    REVIEW_TOOL_NAME_CONFIDENCE,
    SIMILARWEB_API_BASE,
    SIMILARWEB_EXTENSION_ID,
    SIMILARWEB_EXTENSION_UPDATE_URL,
    SIMILARWEB_EXTENSION_VERSION_FALLBACK,
    SIMILARWEB_USER_AGENT,
    TRAFFIC_BACKFILL_MAX_SNAPSHOT_STATEMENTS,
    TRAFFIC_METRICS_SCHEMA_VERSION,
    TRAFFIC_SOURCE,
)


@dataclass(frozen=True)
class Config:
    cloudflare_account_id: str
    cloudflare_d1_database_id: str
    cloudflare_api_token: str
    ahref_api_key: str
    ahrefs_requests_per_minute: int
    brightdata_proxy_host: str
    brightdata_proxy_port: int
    brightdata_proxy_user: str
    brightdata_proxy_password: str
    limit: int
    concurrency: int
    traffic_concurrency: int
    asset_concurrency: int
    domain_state_concurrency: int
    max_retries: int
    poll_interval_seconds: int
    traffic_release_probe_domain: str
    traffic_release_probe_start_day: int
    traffic_release_probe_interval_seconds: int
    traffic_release_queue_limit: int
    asset_limit: int
    enrichment_reconcile_limit: int
    enrichment_reconcile_concurrency: int
    catalog_auto_publish_enabled: bool
    catalog_auto_publish_limit: int
    domain_state_limit: int
    domain_state_max_age_days: int
    domain_state_poll_interval_seconds: int
    taxonomy_auto_enabled: bool
    taxonomy_recheck_auto_non_product: bool
    taxonomy_capabilities_enabled: bool
    taxonomy_capability_backfill_enabled: bool
    taxonomy_capability_candidate_limit: int
    taxonomy_limit: int
    taxonomy_interval_seconds: int
    taxonomy_concurrency: int
    taxonomy_auto_accept_confidence: float
    taxonomy_provider_backoff_seconds: int
    taxonomy_batch_enabled: bool
    taxonomy_batch_model: str
    taxonomy_batch_escalation_model: str
    taxonomy_batch_profile_reasoning_effort: str
    taxonomy_batch_l1_reasoning_effort: str
    taxonomy_batch_leaf_reasoning_effort: str
    taxonomy_batch_capability_reasoning_effort: str
    taxonomy_batch_escalation_reasoning_effort: str
    taxonomy_batch_max_output_tokens: int
    taxonomy_batch_request_limit: int
    taxonomy_batch_timeout_seconds: int
    taxonomy_batch_max_attempts: int
    taxonomy_batch_retry_base_seconds: int
    taxonomy_batch_leaf_min_confidence: float
    taxonomy_batch_l1_min_gap: float
    openai_api_key: str
    browser_rendering_api_token: str
    browser_rendering_enabled: bool
    browser_rendering_timeout_seconds: int
    taxonomy_classification_api_token: str
    taxonomy_classification_deepseek_api_key: str
    taxonomy_classification_model: str
    taxonomy_classification_fallback_model: str
    taxonomy_main_content_max_chars: int
    taxonomy_deepseek_max_output_tokens: int
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_public_base_url: str
    runner_instance_id: str
    runner_version: str
    runner_service_name: str
    runner_workloads: tuple[str, ...]
    jev_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = "perplexity/pplx-decider-v1.2"
    taxonomy_capability_provider: str = "openai"
    jev_min_confidence: float = 0.85
    jev_timeout_seconds: int = 8
    jev_capability_limit: int = 6


class D1RequestError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        reason: str,
        status_code: int | None,
        request_id: str,
        request_kind: str,
        statement_count: int,
        sql_verbs: list[str],
        sql_fingerprint: str,
        body_sample: str,
    ):
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        self.request_id = request_id
        self.request_kind = request_kind
        self.statement_count = statement_count
        self.sql_verbs = sql_verbs
        self.sql_fingerprint = sql_fingerprint
        self.body_sample = body_sample
        details = [
            f"operation={operation}",
            f"reason={reason}",
            f"request_kind={request_kind}",
            f"statement_count={statement_count}",
            f"sql_verbs={','.join(sql_verbs) or 'unknown'}",
            f"sql_fingerprint={sql_fingerprint}",
        ]
        if status_code is not None:
            details.append(f"http_status={status_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        if body_sample:
            details.append(f"response_body={body_sample}")
        super().__init__("D1 request failed: " + " ".join(details))

    def log_fields(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "reason": self.reason,
            "http_status": self.status_code,
            "request_id": self.request_id,
            "request_kind": self.request_kind,
            "statement_count": self.statement_count,
            "sql_verbs": self.sql_verbs,
            "sql_fingerprint": self.sql_fingerprint,
            "response_body": self.body_sample,
        }


from libs.env import read_bool_env, read_float_env, read_int_env
from libs.logging_util import (
    LOG_WORKLOAD as _LOG_WORKLOAD,
    configure_logging,
    emit_log,
    log_debug,
    log_error,
    log_info,
    log_task_result,
    mask_value,
    redact_log_text,
)
from libs.timeutil import iso_delta, parse_iso_timestamp, utc_now_iso
from libs.domains import get_registrable_domain, normalize_domain, normalize_rdap_domain
from libs.numbers import (
    country_name,
    estimate_visits_from_bps,
    normalize_country_code,
    parse_country_rank,
    parse_unit_ratio,
    to_integer,
    to_month_start,
    to_number,
)
from libs.http import (
    AsyncRequestPacer,
    browser_request_headers,
    parse_retry_after_seconds,
    random_browser_user_agent,
    response_body_sample,
    response_header_summary,
)
from libs.text import d1_request_metadata, generate_session_id, sha256_text

# parse_monthly_rows lives with traffic parsers; re-exported after late import.

def load_config(require_brightdata: bool = True) -> Config:
    load_dotenv()
    shared_concurrency = max(1, read_int_env("RUNNER_CONCURRENCY", 5))
    return Config(
        cloudflare_account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        cloudflare_d1_database_id=os.environ["CLOUDFLARE_D1_DATABASE_ID"],
        cloudflare_api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        ahref_api_key=(os.getenv("AHREF_API_KEY") or os.getenv("AHREFS_API_KEY", "")).strip(),
        ahrefs_requests_per_minute=min(
            AHREFS_MAX_REQUESTS_PER_MINUTE,
            max(
                1,
                read_int_env(
                    "RUNNER_AHREFS_REQUESTS_PER_MINUTE",
                    AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
                ),
            ),
        ),
        brightdata_proxy_host=os.getenv("BRIGHTDATA_PROXY_HOST", "brd.superproxy.io"),
        brightdata_proxy_port=read_int_env("BRIGHTDATA_PROXY_PORT", 33335),
        brightdata_proxy_user=os.environ["BRIGHTDATA_PROXY_USER"] if require_brightdata else os.getenv("BRIGHTDATA_PROXY_USER", ""),
        brightdata_proxy_password=os.environ["BRIGHTDATA_PROXY_PASSWORD"] if require_brightdata else os.getenv("BRIGHTDATA_PROXY_PASSWORD", ""),
        limit=read_int_env("RUNNER_LIMIT", 20),
        concurrency=shared_concurrency,
        traffic_concurrency=max(
            1, read_int_env("RUNNER_TRAFFIC_CONCURRENCY", shared_concurrency)
        ),
        asset_concurrency=max(
            1, read_int_env("RUNNER_ASSET_CONCURRENCY", shared_concurrency)
        ),
        domain_state_concurrency=max(
            1, read_int_env("RUNNER_DOMAIN_CONCURRENCY", shared_concurrency)
        ),
        max_retries=read_int_env("RUNNER_MAX_RETRIES", 2),
        poll_interval_seconds=read_int_env("RUNNER_POLL_INTERVAL_SECONDS", 300),
        traffic_release_probe_domain=normalize_domain(os.getenv("TRAFFIC_RELEASE_PROBE_DOMAIN", "chatgpt.com")) or "chatgpt.com",
        traffic_release_probe_start_day=min(max(read_int_env("TRAFFIC_RELEASE_PROBE_START_DAY", 7), 1), 28),
        traffic_release_probe_interval_seconds=max(900, read_int_env("TRAFFIC_RELEASE_PROBE_INTERVAL_SECONDS", 21600)),
        traffic_release_queue_limit=max(1, read_int_env("TRAFFIC_RELEASE_QUEUE_LIMIT", 5000)),
        asset_limit=max(1, read_int_env("RUNNER_ASSET_LIMIT", 25)),
        enrichment_reconcile_limit=min(
            500,
            max(1, read_int_env("RUNNER_ENRICHMENT_RECONCILE_LIMIT", 100)),
        ),
        enrichment_reconcile_concurrency=min(
            10,
            max(1, read_int_env("RUNNER_ENRICHMENT_RECONCILE_CONCURRENCY", 5)),
        ),
        catalog_auto_publish_enabled=read_bool_env("CATALOG_AUTO_PUBLISH_ENABLED", True),
        catalog_auto_publish_limit=min(
            CATALOG_AUTO_PUBLISH_MAX_LIMIT,
            max(1, read_int_env("CATALOG_AUTO_PUBLISH_LIMIT", 25)),
        ),
        domain_state_limit=read_int_env("RUNNER_DOMAIN_STATE_LIMIT", 50),
        domain_state_max_age_days=max(
            1, read_int_env("RUNNER_DOMAIN_STATE_MAX_AGE_DAYS", 30)
        ),
        domain_state_poll_interval_seconds=max(
            1, read_int_env("RUNNER_DOMAIN_POLL_INTERVAL_SECONDS", 1)
        ),
        jev_api_key=(os.getenv("JEV_API_KEY") or os.getenv("jev_api_key") or os.getenv("TYPESAFE_API_KEY") or "").strip(),
        openrouter_api_key=(os.getenv("OPENROUTER_API_KEY") or os.getenv("openrouter_api_key") or "").strip(),
        openrouter_model=(os.getenv("OPENROUTER_MODEL") or "perplexity/pplx-decider-v1.2").strip(),
        taxonomy_capability_provider=os.getenv("TAXONOMY_CAPABILITY_PROVIDER", ("openrouter" if (os.getenv("OPENROUTER_API_KEY") or os.getenv("openrouter_api_key")) else "openai")).strip().lower(),
        jev_min_confidence=min(1.0, max(0.5, read_float_env("JEV_MIN_CONFIDENCE", 0.85))),
        jev_timeout_seconds=min(10, max(1, read_int_env("JEV_TIMEOUT_SECONDS", 8))),
        jev_capability_limit=min(10, max(1, read_int_env("JEV_CAPABILITY_LIMIT", 6))),
        taxonomy_auto_enabled=read_bool_env("TAXONOMY_AUTO_ENABLED", True),
        taxonomy_recheck_auto_non_product=read_bool_env(
            "TAXONOMY_RECHECK_AUTO_NON_PRODUCT", False
        ),
        taxonomy_capabilities_enabled=read_bool_env(
            "TAXONOMY_CAPABILITIES_ENABLED", True
        ),
        taxonomy_capability_backfill_enabled=read_bool_env(
            "TAXONOMY_CAPABILITY_BACKFILL_ENABLED", True
        ),
        taxonomy_capability_candidate_limit=min(
            160,
            max(12, read_int_env("TAXONOMY_CAPABILITY_CANDIDATE_LIMIT", 96)),
        ),
        taxonomy_limit=max(1, read_int_env("RUNNER_TAXONOMY_LIMIT", 50)),
        taxonomy_interval_seconds=max(
            300, read_int_env("RUNNER_TAXONOMY_INTERVAL_SECONDS", 300)
        ),
        taxonomy_concurrency=min(
            8, max(1, read_int_env("RUNNER_TAXONOMY_CONCURRENCY", 3))
        ),
        taxonomy_auto_accept_confidence=min(
            1.0,
            max(MIN_AUTO_PRIMARY_CONFIDENCE, read_float_env("TAXONOMY_AUTO_ACCEPT_CONFIDENCE", MIN_AUTO_PRIMARY_CONFIDENCE)),
        ),
        taxonomy_provider_backoff_seconds=max(
            900, read_int_env("RUNNER_TAXONOMY_PROVIDER_BACKOFF_SECONDS", 21600)
        ),
        taxonomy_batch_enabled=read_bool_env("TAXONOMY_BATCH_ENABLED", True),
        taxonomy_batch_model=(
            os.getenv("OPENAI_TAXONOMY_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna"
        ),
        taxonomy_batch_escalation_model=(
            os.getenv("OPENAI_TAXONOMY_ESCALATION_MODEL", "gpt-5.6-terra").strip()
            or "gpt-5.6-terra"
        ),
        taxonomy_batch_profile_reasoning_effort=os.getenv(
            "OPENAI_TAXONOMY_PROFILE_REASONING_EFFORT", "medium"
        ).strip(),
        taxonomy_batch_l1_reasoning_effort=os.getenv(
            "OPENAI_TAXONOMY_L1_REASONING_EFFORT", "low"
        ).strip(),
        taxonomy_batch_leaf_reasoning_effort=os.getenv(
            "OPENAI_TAXONOMY_LEAF_REASONING_EFFORT", "high"
        ).strip(),
        taxonomy_batch_capability_reasoning_effort=os.getenv(
            "OPENAI_TAXONOMY_CAPABILITY_REASONING_EFFORT", "medium"
        ).strip(),
        taxonomy_batch_escalation_reasoning_effort=os.getenv(
            "OPENAI_TAXONOMY_ESCALATION_REASONING_EFFORT", "high"
        ).strip(),
        taxonomy_batch_max_output_tokens=min(
            16384,
            max(1024, read_int_env("OPENAI_TAXONOMY_MAX_OUTPUT_TOKENS", 4096)),
        ),
        taxonomy_batch_request_limit=min(
            50000,
            max(1, read_int_env("OPENAI_TAXONOMY_BATCH_REQUEST_LIMIT", 500)),
        ),
        taxonomy_batch_timeout_seconds=max(
            30, read_int_env("OPENAI_TAXONOMY_TIMEOUT_SECONDS", 90)
        ),
        taxonomy_batch_max_attempts=min(
            10, max(1, read_int_env("OPENAI_TAXONOMY_MAX_ATTEMPTS", 3))
        ),
        taxonomy_batch_retry_base_seconds=min(
            21600,
            max(30, read_int_env("OPENAI_TAXONOMY_RETRY_BASE_SECONDS", 300)),
        ),
        taxonomy_batch_leaf_min_confidence=min(
            1.0,
            max(
                0.35,
                read_float_env("OPENAI_TAXONOMY_LEAF_MIN_CONFIDENCE", 0.60),
            ),
        ),
        taxonomy_batch_l1_min_gap=min(
            1.0,
            max(0.0, read_float_env("OPENAI_TAXONOMY_L1_MIN_GAP", 0.08)),
        ),
        openai_api_key=os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API", ""),
        browser_rendering_api_token=os.getenv("CLOUDFLARE_BROWSER_RENDERING_API_TOKEN") or os.environ["CLOUDFLARE_API_TOKEN"],
        browser_rendering_enabled=read_bool_env("CLOUDFLARE_BROWSER_RENDERING_ENABLED", False),
        browser_rendering_timeout_seconds=read_int_env("CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS", 45),
        taxonomy_classification_api_token=(
            os.getenv("CLOUDFLARE_WORKERS_AI_API_TOKEN")
            or os.getenv("CLOUDFLARE_BROWSER_RENDERING_API_TOKEN")
            or os.environ["CLOUDFLARE_API_TOKEN"]
        ),
        taxonomy_classification_deepseek_api_key=(
            os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("TAXONOMY_CLASSIFICATION_DEEPSEEK_API_KEY")
            or ""
        ).strip(),
        taxonomy_classification_model=normalize_taxonomy_model_id(
            os.getenv(
                "TAXONOMY_CLASSIFICATION_MODEL",
                DEFAULT_TAXONOMY_CLASSIFICATION_MODEL,
            )
        ),
        taxonomy_classification_fallback_model=normalize_taxonomy_model_id(
            os.getenv(
                "TAXONOMY_CLASSIFICATION_FALLBACK_MODEL",
                DEFAULT_TAXONOMY_CLASSIFICATION_FALLBACK_MODEL,
            )
        ),
        taxonomy_main_content_max_chars=min(
            20000,
            max(2000, read_int_env("TAXONOMY_MAIN_CONTENT_MAX_CHARS", 10000)),
        ),
        taxonomy_deepseek_max_output_tokens=min(
            4096,
            max(256, read_int_env("TAXONOMY_DEEPSEEK_MAX_OUTPUT_TOKENS", 1024)),
        ),
        r2_access_key_id=os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", ""),
        r2_bucket=os.getenv("CLOUDFLARE_R2_BUCKET", DEFAULT_R2_BUCKET),
        r2_public_base_url=os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/"),
        runner_instance_id=os.getenv("RUNNER_INSTANCE_ID") or f"runner-{uuid.uuid4().hex[:16]}",
        runner_version=(
            os.getenv("RUNNER_VERSION")
            or os.getenv("DOKPLOY_COMMIT_SHA")
            or os.getenv("GIT_COMMIT_SHA")
            or os.getenv("SOURCE_COMMIT")
            or "dev"
        ).strip(),
        runner_service_name=(os.getenv("RUNNER_SERVICE_NAME") or "tool-data-runner").strip(),
        runner_workloads=(),
    )


# Types used by D1Client annotations must resolve on Python 3.12 at class body
# time unless postponed; import the lightweight dataclasses before D1Client.
from workloads.traffic.types import FetchResult, TrafficReleaseGateResult, TrafficTask


class D1Client:
    def __init__(self, config: Config):
        from postgres_legacy import transport_for
        self._postgres = transport_for(config.cloudflare_d1_database_id)
        self._market_country_schema_available: bool | None = None
        if self._postgres is not None:
            return
        self.url = (
            f"{D1_API_BASE}/accounts/{config.cloudflare_account_id}"
            f"/d1/database/{config.cloudflare_d1_database_id}/query"
        )
        self.headers = {
            "Authorization": f"Bearer {config.cloudflare_api_token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=30.0)
        self._market_country_schema_available: bool | None = None

    async def close(self) -> None:
        if self._postgres is not None:
            await self._postgres.close()
            return
        await self.client.aclose()

    async def __aenter__(self) -> "D1Client":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    def _response_error(
        self,
        response: httpx.Response,
        request_meta: dict[str, Any],
        reason: str,
    ) -> D1RequestError:
        return D1RequestError(
            operation=str(request_meta["operation"]),
            reason=reason,
            status_code=response.status_code,
            request_id=str(response.headers.get("cf-ray") or response.headers.get("x-request-id") or ""),
            request_kind=str(request_meta["request_kind"]),
            statement_count=int(request_meta["statement_count"]),
            sql_verbs=list(request_meta["sql_verbs"]),
            sql_fingerprint=str(request_meta["sql_fingerprint"]),
            body_sample=redact_log_text(response_body_sample(response, 1200)),
        )

    async def _request(
        self,
        body: dict[str, Any],
        retry_transient: bool = False,
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._postgres is not None:
            return await self._postgres.request(body)
        response: httpx.Response | None = None
        last_error: Exception | None = None
        max_attempts = 4 if retry_transient else 1
        request_meta = d1_request_metadata(body, operation)
        for attempt in range(max_attempts):
            try:
                response = await self.client.post(self.url, headers=self.headers, json=body)
                if response.status_code not in {429, 502, 503, 504}:
                    if response.is_error:
                        request_error = self._response_error(response, request_meta, "http_error")
                        log_error(
                            "d1.request.failed",
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                            **request_error.log_fields(),
                        )
                        raise request_error
                    break
                last_error = self._response_error(response, request_meta, "transient_http_error")
            except D1RequestError:
                raise
            except httpx.RequestError as error:
                last_error = error
            if attempt < max_attempts - 1:
                retry_fields = {
                    "operation": request_meta["operation"],
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "error_type": type(last_error).__name__,
                    "error": str(last_error)[:1500],
                }
                log_info("d1.request.retrying", **retry_fields)
                await asyncio.sleep(min(8.0, (2**attempt) + random.uniform(0.0, 0.5)))

        if response is None or response.status_code in {429, 502, 503, 504}:
            if isinstance(last_error, D1RequestError):
                log_error(
                    "d1.request.failed",
                    attempt=max_attempts,
                    max_attempts=max_attempts,
                    **last_error.log_fields(),
                )
                raise last_error
            message = (
                f"D1 request failed after {max_attempts} attempt(s): "
                f"operation={request_meta['operation']} request_kind={request_meta['request_kind']} "
                f"statement_count={request_meta['statement_count']} "
                f"sql_fingerprint={request_meta['sql_fingerprint']} error={last_error}"
            )
            log_error(
                "d1.request.failed",
                operation=request_meta["operation"],
                reason="request_error",
                request_kind=request_meta["request_kind"],
                statement_count=request_meta["statement_count"],
                sql_verbs=request_meta["sql_verbs"],
                sql_fingerprint=request_meta["sql_fingerprint"],
                error_type=type(last_error).__name__ if last_error else "unknown",
                error=str(last_error)[:1200],
            )
            raise RuntimeError(message) from last_error

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as error:
            request_error = self._response_error(response, request_meta, "invalid_json_response")
            log_error("d1.request.failed", **request_error.log_fields())
            raise request_error from error
        if not payload.get("success", False):
            request_error = self._response_error(response, request_meta, "api_unsuccessful")
            log_error("d1.request.failed", **request_error.log_fields())
            raise request_error
        result = payload.get("result")
        results = result if isinstance(result, list) else [result or {}]
        for query_result in results:
            if isinstance(query_result, dict) and not query_result.get("success", True):
                request_error = self._response_error(response, request_meta, "query_unsuccessful")
                log_error("d1.request.failed", **request_error.log_fields())
                raise request_error
        return [query_result for query_result in results if isinstance(query_result, dict)]

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> Any:
        normalized_sql = sql.lstrip().upper()
        retry_transient = normalized_sql.startswith(("SELECT", "PRAGMA", "EXPLAIN"))
        results = await self._request(
            {"sql": sql, "params": params or []},
            retry_transient=retry_transient,
            operation=operation,
        )
        return results[0] if results else {}

    async def batch(
        self,
        statements: list[tuple[str, list[Any]]],
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        if not statements:
            return []
        return await self._request(
            {
                "batch": [
                    {"sql": sql, "params": params}
                    for sql, params in statements
                ]
            },
            operation=operation,
        )

    async def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.execute(sql, params, operation=operation)
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            return result["results"]
        return []

    async def run(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        result = await self.execute(sql, params, operation=operation)
        if isinstance(result, dict) and isinstance(result.get("meta"), dict):
            return result["meta"]
        return {}

    async def insert_snapshot(
        self,
        domain: str,
        task_month: str,
        status: str,
        row: dict[str, Any],
        error: str | None,
        raw_payload: dict[str, Any] | None = None,
    ) -> int | None:
        meta = await self.run(
            """
            INSERT INTO domain_traffic_snapshots (
              normalized_domain, source, website, query_date, traffic_month, status,
              visits, engagement_visits, global_rank, country_rank_country, country_rank,
              country_rank_text, bounce_rate, pages_per_visit, avg_visit_duration_seconds,
              social_traffic_share, paid_referrals_traffic_share, mail_traffic_share,
              search_traffic_share, direct_traffic_share, referrals_traffic_share,
              top_country_1, top_country_1_traffic_share, top_country_2, top_country_2_traffic_share,
              top_country_3, top_country_3_traffic_share, top_country_4, top_country_4_traffic_share,
              top_country_5, top_country_5_traffic_share, fetched_at, raw_payload, last_error
            )
            VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              CURRENT_TIMESTAMP, ?, ?
            )
            """,
            [
                domain,
                TRAFFIC_SOURCE,
                row.get("website") or domain,
                row.get("query_date"),
                row.get("traffic_month") or task_month,
                status,
                row.get("visits"),
                row.get("engagement_visits"),
                row.get("global_rank"),
                row.get("country_rank_country"),
                row.get("country_rank"),
                row.get("country_rank_text"),
                row.get("bounce_rate"),
                row.get("pages_per_visit"),
                row.get("avg_visit_duration_seconds"),
                row.get("social_traffic_share"),
                row.get("paid_referrals_traffic_share"),
                row.get("mail_traffic_share"),
                row.get("search_traffic_share"),
                row.get("direct_traffic_share"),
                row.get("referrals_traffic_share"),
                row.get("top_country_1"),
                row.get("top_country_1_traffic_share"),
                row.get("top_country_2"),
                row.get("top_country_2_traffic_share"),
                row.get("top_country_3"),
                row.get("top_country_3_traffic_share"),
                row.get("top_country_4"),
                row.get("top_country_4_traffic_share"),
                row.get("top_country_5"),
                row.get("top_country_5_traffic_share"),
                json.dumps(raw_payload or {}, ensure_ascii=False),
                error,
            ],
            operation="traffic.snapshot.insert",
        )
        last_row_id = to_integer(meta.get("last_row_id"))
        return last_row_id if last_row_id and last_row_id > 0 else None

    async def insert_result(self, task: TrafficTask, result: FetchResult) -> None:
        rows = result.monthly_rows or [{"traffic_month": task.traffic_month}]
        log_debug(
            "d1.insert_result.start",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
            rows=len(rows),
        )
        projection_rows: list[dict[str, Any]] = []
        for row in rows:
            snapshot_payload = (
                result.raw_payload
                if result.raw_payload
                and (
                    row.get("traffic_month") == result.observed_latest_month
                    or (len(rows) == 1 and result.observed_latest_month is None)
                )
                else None
            )
            snapshot_id = await self.insert_snapshot(
                task.normalized_domain,
                task.traffic_month,
                result.status,
                row,
                result.error,
                snapshot_payload,
            )
            if result.monthly_rows:
                projection_rows.append({**row, "_source_snapshot_id": snapshot_id})
        if result.monthly_rows:
            await self.upsert_domain_traffic_monthly(
                task.normalized_domain,
                projection_rows,
            )
            await self.upsert_tool_traffic_monthly(task.normalized_domain, result.monthly_rows)
        log_debug(
            "d1.insert_result.done",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
            rows=len(rows),
        )

    async def upsert_domain_traffic_monthly(
        self,
        domain: str,
        rows: list[dict[str, Any]],
        *,
        captured_at: str | None = None,
    ) -> None:
        projection_captured_at = captured_at or utc_now_iso()
        statements = [
            build_domain_traffic_monthly_upsert(
                domain,
                row,
                captured_at=projection_captured_at,
                source_snapshot_id=to_integer(row.get("_source_snapshot_id")),
            )
            for row in rows
            if row.get("traffic_month")
        ]
        if statements:
            await self.batch(statements, operation="traffic.domain_monthly.upsert")
            await self.upsert_domain_traffic_country_monthly(
                domain,
                rows,
                captured_at=projection_captured_at,
            )

    async def upsert_domain_traffic_country_monthly(
        self,
        domain: str,
        rows: list[dict[str, Any]],
        *,
        captured_at: str,
    ) -> None:
        if getattr(self, "_market_country_schema_available", None) is False:
            return
        statements = build_domain_traffic_country_upsert_statements(
            domain,
            rows,
            captured_at=captured_at,
        )
        if not statements:
            return

        try:
            await self.batch(
                statements,
                operation="traffic.domain_country_monthly.upsert",
            )
        except Exception as error:
            if not is_missing_market_country_schema_error(error):
                raise
            # 0041 can be deployed after this runner version. Keep the canonical
            # monthly write and the legacy compatibility write healthy, then
            # probe again when the process restarts after the migration rollout.
            setattr(self, "_market_country_schema_available", False)
            log_info(
                "d1.domain_traffic_country_monthly.schema_unavailable",
                domain=domain,
                error=str(error)[:300],
            )
            return

        setattr(self, "_market_country_schema_available", True)

    async def upsert_tool_traffic_monthly(self, domain: str, rows: list[dict[str, Any]]) -> None:
        tools = await self.query(
            """
            SELECT id
            FROM tools
            WHERE normalized_domain = ?
              AND status IN ('published', 'pending_enrich', 'pending_review')
              AND duplicate_of_tool_id IS NULL
            """,
            [domain],
            operation="traffic.tool_monthly.lookup",
        )
        if not tools:
            log_info("d1.tool_traffic_monthly.no_matching_tools", domain=domain)
            return

        captured_at = utc_now_iso()
        for tool in tools:
            tool_id = int(tool.get("id") or 0)
            if tool_id <= 0:
                continue
            for row in rows:
                traffic_month = row.get("traffic_month")
                if not traffic_month:
                    continue
                await self.run(
                    """
                    INSERT INTO tool_traffic_monthly (
                      tool_id, normalized_domain, source, traffic_month, visits,
                      global_rank, country_rank_country, country_rank, bounce_rate,
                      pages_per_visit, avg_visit_duration_seconds, captured_at, raw_payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tool_id, source, traffic_month) DO UPDATE
                    SET normalized_domain = excluded.normalized_domain,
                        visits = excluded.visits,
                        global_rank = excluded.global_rank,
                        country_rank_country = excluded.country_rank_country,
                        country_rank = excluded.country_rank,
                        bounce_rate = excluded.bounce_rate,
                        pages_per_visit = excluded.pages_per_visit,
                        avg_visit_duration_seconds = excluded.avg_visit_duration_seconds,
                        captured_at = excluded.captured_at,
                        raw_payload = json_patch(tool_traffic_monthly.raw_payload, excluded.raw_payload),
                        updated_at = ?
                    """,
                    [
                        tool_id,
                        domain,
                        TRAFFIC_SOURCE,
                        traffic_month,
                        row.get("visits"),
                        row.get("global_rank") or None,
                        row.get("country_rank_country"),
                        row.get("country_rank") or None,
                        row.get("bounce_rate"),
                        row.get("pages_per_visit"),
                        row.get("avg_visit_duration_seconds"),
                        captured_at,
                        json.dumps(row, ensure_ascii=False),
                        captured_at,
                    ],
                    operation="traffic.tool_monthly.upsert",
                )


class RunnerTelemetry:
    BASE_WORKLOADS = [
        "assets",
        "traffic",
        "domain_state",
        "enrichment",
        "catalog_publish",
    ]

    def __init__(self, d1: D1Client, config: Config, workload: str | None = None):
        self.d1 = d1
        self.instance_id = config.runner_instance_id
        self.version = config.runner_version
        self.service = getattr(config, "runner_service_name", "tool-data-runner")
        self.workload = workload
        configured_workloads = tuple(getattr(config, "runner_workloads", ()) or ())
        self.workloads = list(configured_workloads or ([workload] if workload else self.BASE_WORKLOADS))
        if not configured_workloads and workload is None and getattr(config, "taxonomy_auto_enabled", False):
            self.workloads.insert(-1, "taxonomy")

    async def register(self) -> None:
        now = utc_now_iso()
        await self.d1.run(
            """
            INSERT INTO runner_instances (
              instance_id, service, version, status, workloads_json,
              started_at, last_heartbeat_at, last_error, metadata_json, updated_at
            )
            VALUES (?, ?, ?, 'healthy', ?, ?, ?, NULL, '{}', ?)
            ON CONFLICT(instance_id) DO UPDATE SET
              service = excluded.service,
              version = excluded.version,
              workloads_json = excluded.workloads_json,
              last_heartbeat_at = excluded.last_heartbeat_at,
              stopped_at = NULL,
              updated_at = excluded.updated_at
            """,
            [self.instance_id, self.service, self.version, json.dumps(self.workloads), now, now, now],
        )

    async def start(self, workload: str) -> int:
        now = utc_now_iso()
        await self.register()
        rows = await self.d1.query(
            """
            INSERT INTO runner_runs (instance_id, workload, status, started_at, counts_json)
            VALUES (?, ?, 'running', ?, '{}')
            RETURNING id
            """,
            [self.instance_id, workload, now],
        )
        if not rows:
            raise RuntimeError("Runner telemetry did not return a run id")
        return int(rows[0]["id"])

    async def finish(self, run_id: int, counts: dict[str, int] | None = None, error: str | None = None) -> None:
        now = utc_now_iso()
        counts = counts or {}
        counts_json = json.dumps(counts, sort_keys=True)
        degraded_counts = {
            key: int(counts.get(key) or 0)
            for key in (
                "failed",
                "materialization_failed",
                "stale",
                "auto_publish_failed",
            )
            if int(counts.get(key) or 0) > 0
        }
        health_error = error
        if not health_error and degraded_counts:
            health_error = "Batch completed with " + ", ".join(
                f"{key}={value}" for key, value in degraded_counts.items()
            )
        status = "failed" if health_error else "succeeded"
        workload_values = ", ".join("(?)" for _ in self.workloads)
        latest_health_sql = f"""
                WITH workloads(workload) AS (
                  VALUES {workload_values}
                ),
                latest_health AS (
                  SELECT EXISTS (
                    SELECT 1
                    FROM workloads workload
                    WHERE (
                      SELECT latest.status
                      FROM runner_runs latest
                      WHERE latest.instance_id = ?
                        AND latest.workload = workload.workload
                      ORDER BY latest.id DESC
                      LIMIT 1
                    ) = 'failed'
                  ) AS has_failed
                )
                UPDATE runner_instances
                SET status = CASE
                      WHEN ? IS NOT NULL THEN 'degraded'
                      WHEN (SELECT has_failed FROM latest_health) = 1 THEN 'degraded'
                      ELSE 'healthy'
                    END,
                    last_heartbeat_at = ?,
                    last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                    last_error = CASE
                      WHEN ? IS NOT NULL THEN ?
                      WHEN (SELECT has_failed FROM latest_health) = 1 THEN last_error
                      ELSE NULL
                    END,
                    updated_at = ?
                WHERE instance_id = ?
                """
        statements: list[tuple[str, list[Any]]] = [
            (
                """
                UPDATE runner_runs
                SET status = ?, finished_at = ?, counts_json = ?, error = ?
                WHERE id = ? AND instance_id = ? AND status = 'running'
                """,
                [status, now, counts_json, health_error, run_id, self.instance_id],
            ),
            (
                latest_health_sql,
                [
                    *self.workloads,
                    self.instance_id,
                    health_error,
                    now,
                    health_error,
                    now,
                    health_error,
                    health_error,
                    now,
                    self.instance_id,
                ],
            ),
        ]
        await self.d1.batch(statements)

    async def heartbeat(self, metadata: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        heartbeat_metadata: dict[str, Any] = {"process_heartbeat_at": now}
        heartbeat_metadata.update(metadata or {})
        metadata_patch = json.dumps(heartbeat_metadata, sort_keys=True)
        await self.d1.run(
            """
            UPDATE runner_instances
            SET last_heartbeat_at = ?,
                metadata_json = CASE
                  WHEN ? IS NULL THEN json_patch(coalesce(metadata_json, '{}'), ?)
                  ELSE json_set(
                    json_patch(coalesce(metadata_json, '{}'), ?),
                    '$.workload_heartbeats.' || ?,
                    ?
                  )
                END,
                updated_at = ?
            WHERE instance_id = ?
            """,
            [
                now,
                self.workload,
                metadata_patch,
                metadata_patch,
                self.workload,
                now,
                now,
                self.instance_id,
            ],
        )


async def run_with_telemetry(
    config: Config,
    d1: D1Client,
    workload: str,
    operation: Any,
) -> dict[str, int]:
    log_token = _LOG_WORKLOAD.set(workload)
    try:
        telemetry = RunnerTelemetry(d1, config, workload)
        run_id = await telemetry.start(workload)
        await telemetry.heartbeat()

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await telemetry.heartbeat()
                except Exception as error:
                    log_error("runner.telemetry.heartbeat_failed", error=str(error)[:300])

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            counts = await operation()
        except asyncio.CancelledError:
            try:
                await telemetry.finish(run_id, error="cancelled")
            except Exception as telemetry_error:
                log_error("runner.telemetry.cancel_finish_failed", error=str(telemetry_error)[:300])
            raise
        except Exception as error:
            try:
                await telemetry.finish(run_id, error=str(error)[:2000])
            except Exception as telemetry_error:
                log_error(
                    "runner.telemetry.finish_failed",
                    run_id=run_id,
                    error=str(telemetry_error)[:300],
                )
            raise
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        await telemetry.finish(run_id, counts=counts)
        return counts
    finally:
        _LOG_WORKLOAD.reset(log_token)


async def run_with_service_heartbeat(
    config: Config,
    operation: Any,
    heartbeat_interval_seconds: int = 30,
) -> Any:
    """Keep process liveness independent from bounded workload runs and sleeps."""
    async with D1Client(config) as heartbeat_d1:
        telemetry = RunnerTelemetry(heartbeat_d1, config)

        async def heartbeat_loop() -> None:
            registered = False
            while True:
                try:
                    if not registered:
                        await telemetry.register()
                        registered = True
                    await telemetry.heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    registered = False
                    log_error(
                        "runner.telemetry.service_heartbeat_failed",
                        error=str(error)[:300],
                    )
                await asyncio.sleep(max(1, heartbeat_interval_seconds))

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        # Let the first registration attempt begin without making workload
        # startup depend on telemetry availability.
        await asyncio.sleep(0)
        try:
            return await operation(telemetry)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task


async def report_service_schedule(
    telemetry: RunnerTelemetry | None,
    delay_seconds: int,
    *,
    backoff_reason: str | None = None,
) -> None:
    if telemetry is None:
        return
    delay_seconds = max(0, int(delay_seconds))
    try:
        await telemetry.heartbeat(
            {
                "next_poll_at": iso_delta(seconds=delay_seconds),
                "backoff_until": iso_delta(seconds=delay_seconds) if backoff_reason else None,
                "backoff_reason": backoff_reason,
            }
        )
    except Exception as error:
        log_error("runner.telemetry.schedule_failed", error=str(error)[:300])



# Traffic / domain_state / market workloads (late imports for patchability).
from workloads.traffic.types import FetchResult, TrafficReleaseGateResult, TrafficTask
from workloads.traffic.parse import (
    parse_monthly_rows,
    build_domain_traffic_monthly_upsert,
    build_traffic_metrics,
    extract_brightdata_zone,
    latest_observed_traffic_month,
    parse_ai_traffic,
    parse_top_countries,
    parse_top_keywords,
    parse_traffic_sources,
    previous_traffic_month,
    requested_month_has_traffic_data,
    traffic_share_to_bps,
)
from workloads.traffic.client import SimilarWebClient, fetch_similarweb_extension_version
from workloads.traffic.country import (
    build_domain_traffic_country_upsert_statements,
    is_missing_market_country_schema_error,
)
from workloads.traffic.backfill import (
    add_legacy_snapshot_countries,
    backfill_domain_traffic_monthly,
    backfill_domain_traffic_monthly_from_d1,
)
from workloads.traffic.release import D1TrafficReleaseStore, traffic_release_probe_window_open
from workloads.traffic.store import D1TaskStore
from workloads.traffic.pipeline import _run_once, process_task, run_once

from workloads.domain_state.types import DomainStateResult, DomainStateTask
from workloads.domain_state.client import DomainStateClient, extract_rdap_created_at, find_rdap_base_urls
from workloads.domain_state.store import D1DomainStateStore
from workloads.domain_state.pipeline import (
    _run_domain_state_once,
    process_domain_state,
    run_domain_state_once,
)

from workloads.market.snapshot import (
    MARKET_DR_MAX_BASELINE_DAYS,
    MARKET_DR_MIN_BASELINE_DAYS,
    MARKET_DR_TARGET_BASELINE_DAYS,
    MARKET_MIN_ABSOLUTE_COVERAGE_BPS,
    MARKET_SNAPSHOT_PUBLIC_TOOL_PREDICATE,
    MARKET_VISIBLE_TOOLS_CTES,
    activate_market_snapshot,
    activate_market_snapshot_from_d1,
    build_market_snapshot,
    build_market_snapshot_facet_rollups_from_d1,
    build_market_snapshot_from_d1,
    ensure_market_catalog_eligibility_revision,
    get_market_snapshot_coverage,
    market_snapshot_coverage_payload,
    preview_market_snapshot,
    preview_market_snapshot_from_d1,
    resolve_market_snapshot_months,
    run_market_snapshot_pages,
    validate_market_snapshot_facet_rollups,
)

# Assets workload implementation (stores, fetch, publish). Imported after D1Client
# and shared helpers exist so workloads.assets.* can import them safely.
from workloads.assets.types import (
    AssetFetchResult,
    AssetPipelineError,
    AssetTask,
    ContentSafetyAssessment,
    ContentSafetyError,
    FaviconAsset,
    PageQualityAssessment,
    PageQualityError,
    ToolNameResolution,
)
from workloads.assets.content import (
    assess_content_safety,
    asset_extension,
    asset_mime_type,
    asset_page_url,
    asset_public_url,
    augment_prompt_for_response_format,
    build_browser_response_format,
    canonical_slug_fallback_tool_name,
    classify_page_state,
    clean_asset_text,
    clean_key_features,
    clean_public_slug,
    deterministic_fallback_description,
    deterministic_fallback_key_features,
    extract_favicon_href,
    extract_homepage_main_text,
    find_favicon_href,
    invalid_tool_name_reason,
    normalize_structured_json_payload,
    normalize_taxonomy_model_id,
    numbered_public_slug,
    page_visible_text,
    public_tool_slug_base,
    read_html_meta,
    read_html_title,
    resolve_tool_name,
    taxonomy_model_auth_token,
)
from workloads.assets.browser import CloudflareBrowserRunAssetClient
from workloads.assets.r2 import R2AssetUploader
from workloads.assets.publish import D1CatalogPublisher
from workloads.assets.enrichment import D1EnrichmentStore
from workloads.assets.store import D1AssetStore
from workloads.assets.pipeline import (
    fetch_favicon_asset,
    generate_letter_svg_icon,
    process_asset_task,
    run_assets_once,
)

from workloads.assets.loops import assets_next_delay_seconds, run_assets_loop
from workloads.traffic.loops import run_loop
from workloads.domain_state.loops import (
    DomainDiscoveryBackoff,
    domain_state_next_delay_seconds,
    run_domain_state_loop,
)
from workloads.taxonomy.loops import (
    run_taxonomy_loop,
    run_taxonomy_once,
    taxonomy_batch_has_activity,
    taxonomy_idle_heartbeat_due,
    taxonomy_next_delay_seconds,
)
from workloads.periodic import run_all_loop, run_periodic_facts_loop, run_periodic_facts_once


def runtime_profile_for_args(args: argparse.Namespace) -> tuple[str, tuple[str, ...]]:
    if args.periodic_facts:
        return "periodic-facts-worker", ("traffic", "domain_state")
    if args.assets:
        return "assets-worker", ("assets", "enrichment", "catalog_publish")
    if args.taxonomy:
        return "taxonomy-worker", ("taxonomy",)
    if args.domain_state:
        return "domain-facts-worker", ("domain_state",)
    if args.all:
        workloads = [
            "assets",
            "traffic",
            "domain_state",
            "taxonomy",
            "enrichment",
            "catalog_publish",
        ]
        return "tool-data-runner-legacy-all", tuple(workloads)
    if any(
        (
            args.backfill_traffic_monthly,
            args.build_market_snapshot,
            args.activate_market_snapshot_id is not None,
            args.shadow_taxonomy,
            args.eval_gold,
        )
    ):
        return "tool-data-maintenance", ()
    return "traffic-worker", ("traffic",)


def apply_runtime_profile(config: Config, args: argparse.Namespace) -> Config:
    default_service, workloads = runtime_profile_for_args(args)
    if args.all and not getattr(config, "taxonomy_auto_enabled", False):
        workloads = tuple(workload for workload in workloads if workload != "taxonomy")
    service_name = (os.getenv("RUNNER_SERVICE_NAME") or default_service).strip()
    if not hasattr(config, "__dataclass_fields__"):
        setattr(config, "runner_service_name", service_name)
        setattr(config, "runner_workloads", workloads)
        if not hasattr(config, "runner_instance_id"):
            setattr(config, "runner_instance_id", f"runner-{uuid.uuid4().hex[:16]}")
        return config
    return replace(
        config,
        runner_service_name=service_name,
        runner_workloads=workloads,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled traffic, assets, domain-state, and taxonomy runner")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process one batch and exit")
    mode.add_argument("--loop", action="store_true", help="poll tasks forever")
    parser.add_argument("--assets", action="store_true", help="process asset_tasks instead of traffic_tasks")
    parser.add_argument("--domain-state", action="store_true", help="process domain rating and whois creation date tasks")
    parser.add_argument(
        "--periodic-facts",
        action="store_true",
        help="run traffic and domain-state together as the periodic facts worker",
    )
    parser.add_argument(
        "--taxonomy",
        action="store_true",
        help="run production taxonomy automation as an isolated worker",
    )
    parser.add_argument(
        "--backfill-traffic-monthly",
        action="store_true",
        help=(
            "rebuild domain_traffic_monthly and domain_traffic_country_monthly "
            "from raw Similarweb snapshots"
        ),
    )
    parser.add_argument(
        "--after-snapshot-id",
        type=int,
        default=0,
        help=(
            "with --backfill-traffic-monthly: resume after this completed "
            "domain_traffic_snapshots id"
        ),
    )
    parser.add_argument(
        "--build-market-snapshot",
        action="store_true",
        help="build a complete market serving candidate from release-gated traffic data",
    )
    parser.add_argument(
        "--market-traffic-month",
        default=None,
        help="with --build-market-snapshot: explicit traffic month (default: latest release-gated month)",
    )
    parser.add_argument(
        "--activate-market-snapshot",
        action="store_true",
        help="with --build-market-snapshot: atomically activate the validated candidate",
    )
    parser.add_argument(
        "--activate-market-snapshot-id",
        type=int,
        default=None,
        help=(
            "WRITE: apply coverage gates, then atomically activate an existing "
            "candidate by id without rebuilding it; mutually exclusive with "
            "--build-market-snapshot"
        ),
    )
    parser.add_argument(
        "--shadow-taxonomy",
        action="store_true",
        help="P2A Shadow Mode: entity-gated primary leaf + optional capabilities (no legacy category writes)",
    )
    parser.add_argument(
        "--allow-unresolved-entity",
        action="store_true",
        help="with --shadow-taxonomy: include entity_kind=unresolved published tools",
    )
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="with --shadow-taxonomy: classify entity + primary leaf and defer capabilities",
    )
    parser.add_argument(
        "--after-tool-id",
        type=int,
        default=0,
        help="with --shadow-taxonomy: select only tools after this id (resume cursor)",
    )
    parser.add_argument(
        "--shadow-concurrency",
        type=int,
        default=1,
        help="with --shadow-taxonomy: concurrent tool workers, 1-8 (default: 1)",
    )
    parser.add_argument(
        "--eval-gold",
        action="store_true",
        help="P2B: evaluate Shadow vs Gold CSV and write metrics/diff report (read-only for legacy tables)",
    )
    parser.add_argument(
        "--gold-csv",
        default=None,
        help="path to Gold dataset CSV (default: docs/taxonomy/gold-dataset-seed-draft.csv)",
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="directory for Gold eval reports (default: docs/taxonomy/reports)",
    )
    parser.add_argument(
        "--auto-accepted-threshold",
        type=float,
        default=None,
        help="simulate auto_accepted when provisional confidence >= threshold (default 0.85)",
    )
    parser.add_argument("--all", action="store_true", help="legacy: run every automatic workload in one process")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="for market snapshot or Shadow taxonomy: run without writes",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        action="append",
        default=[],
        help="published tool id with Shadow taxonomy/eval modes; can be repeated",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=None)
    args = parser.parse_args(argv)
    selected = [
        args.assets,
        args.domain_state,
        args.periodic_facts,
        args.taxonomy,
        args.backfill_traffic_monthly,
        args.build_market_snapshot,
        args.activate_market_snapshot_id is not None,
        args.shadow_taxonomy,
        args.eval_gold,
        args.all,
    ]
    if sum(1 for value in selected if value) > 1:
        parser.error(
            "--assets, --domain-state, --periodic-facts, --taxonomy, "
            "--backfill-traffic-monthly, "
            "--build-market-snapshot, --activate-market-snapshot-id, "
            "--shadow-taxonomy, "
            "--eval-gold, and --all are mutually exclusive"
        )
    if args.all and not args.loop:
        parser.error("--all requires --loop")
    if args.interval_seconds is not None and args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.task_id and not (
        args.shadow_taxonomy
        or args.eval_gold
    ):
        parser.error(
            "--task-id requires --shadow-taxonomy, or --eval-gold"
        )
    if args.dry_run and not (
        args.build_market_snapshot
        or args.shadow_taxonomy
    ):
        parser.error(
            "--dry-run requires --build-market-snapshot, or --shadow-taxonomy"
        )
    if args.market_traffic_month and not args.build_market_snapshot:
        parser.error("--market-traffic-month requires --build-market-snapshot")
    if args.after_snapshot_id and not args.backfill_traffic_monthly:
        parser.error("--after-snapshot-id requires --backfill-traffic-monthly")
    if args.after_snapshot_id < 0:
        parser.error("--after-snapshot-id must be non-negative")
    if args.activate_market_snapshot and not args.build_market_snapshot:
        parser.error("--activate-market-snapshot requires --build-market-snapshot")
    if args.activate_market_snapshot and args.dry_run:
        parser.error("--activate-market-snapshot cannot be combined with --dry-run")
    if args.activate_market_snapshot_id is not None and args.activate_market_snapshot_id <= 0:
        parser.error("--activate-market-snapshot-id must be positive")
    if args.allow_unresolved_entity and not args.shadow_taxonomy:
        parser.error("--allow-unresolved-entity requires --shadow-taxonomy")
    if args.primary_only and not args.shadow_taxonomy:
        parser.error("--primary-only requires --shadow-taxonomy")
    if args.after_tool_id and not args.shadow_taxonomy:
        parser.error("--after-tool-id requires --shadow-taxonomy")
    if args.after_tool_id < 0:
        parser.error("--after-tool-id must be non-negative")
    if args.after_tool_id and args.task_id:
        parser.error("--after-tool-id cannot be combined with --task-id")
    if args.shadow_concurrency != 1 and not args.shadow_taxonomy:
        parser.error("--shadow-concurrency requires --shadow-taxonomy")
    if not 1 <= args.shadow_concurrency <= 8:
        parser.error("--shadow-concurrency must be between 1 and 8")
    if args.backfill_traffic_monthly and args.loop:
        parser.error("--backfill-traffic-monthly cannot be combined with --loop")
    if args.build_market_snapshot and args.loop:
        parser.error("--build-market-snapshot cannot be combined with --loop")
    if args.activate_market_snapshot_id is not None and args.loop:
        parser.error("--activate-market-snapshot-id cannot be combined with --loop")
    if args.shadow_taxonomy and args.loop:
        parser.error("--shadow-taxonomy cannot be combined with --loop")
    if args.eval_gold and args.loop:
        parser.error("--eval-gold cannot be combined with --loop")
    if args.task_id and args.eval_gold:
        pass  # allowed: filter gold subset
    if args.gold_csv and not args.eval_gold:
        parser.error("--gold-csv requires --eval-gold")
    if args.report_dir and not args.eval_gold:
        parser.error("--report-dir requires --eval-gold")
    if args.auto_accepted_threshold is not None and not args.eval_gold:
        parser.error("--auto-accepted-threshold requires --eval-gold")
    return args


def main() -> None:
    args = parse_args()
    config = load_config(
        require_brightdata=not (
            args.assets
            or args.domain_state
            or args.taxonomy
            or args.backfill_traffic_monthly
            or args.build_market_snapshot
            or args.activate_market_snapshot_id is not None
            or args.shadow_taxonomy
            or args.eval_gold
        )
    )
    config = apply_runtime_profile(config, args)
    configure_logging(
        config.runner_service_name,
        getattr(config, "runner_instance_id", "runner-cli"),
        config.runner_workloads,
    )
    interval_seconds = args.interval_seconds or config.poll_interval_seconds
    if args.activate_market_snapshot_id is not None:
        result = asyncio.run(
            activate_market_snapshot(config, args.activate_market_snapshot_id)
        )
        log_info("market_snapshot.activate.summary", **result)
        return
    if args.build_market_snapshot:
        if args.dry_run:
            result = asyncio.run(
                preview_market_snapshot(config, args.market_traffic_month)
            )
        else:
            result = asyncio.run(
                build_market_snapshot(
                    config,
                    args.market_traffic_month,
                    activate=args.activate_market_snapshot,
                )
            )
        log_info("market_snapshot.build.summary", **result)
        return
    if args.backfill_traffic_monthly:
        counts = asyncio.run(
            backfill_domain_traffic_monthly(
                config,
                args.limit,
                after_snapshot_id=args.after_snapshot_id,
            )
        )
        log_info("traffic_projection_backfill.summary", **counts)
        return
    if args.shadow_taxonomy:
        from taxonomy_shadow import run_shadow_taxonomy

        counts = asyncio.run(
            run_shadow_taxonomy(
                config,
                args.limit,
                dry_run=args.dry_run,
                tool_ids=args.task_id or None,
                allow_unresolved_entity=args.allow_unresolved_entity,
                after_tool_id=args.after_tool_id,
                include_capabilities=not args.primary_only,
                capability_candidate_limit=config.taxonomy_capability_candidate_limit,
                concurrency=args.shadow_concurrency,
                auto_accept_threshold=config.taxonomy_auto_accept_confidence,
            )
        )
        log_info("shadow_taxonomy.summary", **counts)
        return
    if args.eval_gold:
        from ops.taxonomy_eval import (
            DEFAULT_AUTO_ACCEPTED_THRESHOLD,
            default_gold_csv_path,
            default_report_dir,
            run_gold_evaluation,
        )

        gold_csv = args.gold_csv or str(default_gold_csv_path())
        report_dir = args.report_dir or str(default_report_dir())
        threshold = (
            DEFAULT_AUTO_ACCEPTED_THRESHOLD
            if args.auto_accepted_threshold is None
            else float(args.auto_accepted_threshold)
        )
        result = asyncio.run(
            run_gold_evaluation(
                config,
                gold_csv=gold_csv,
                report_dir=report_dir,
                auto_accepted_threshold=threshold,
                tool_ids=args.task_id or None,
            )
        )
        log_info("gold_eval.done", **result["summary"])
        return
    if args.periodic_facts:
        if args.loop:
            asyncio.run(
                run_with_service_heartbeat(
                    config,
                    lambda telemetry: run_periodic_facts_loop(
                        config,
                        args.limit,
                        interval_seconds,
                    ),
                )
            )
            return
        counts = asyncio.run(run_periodic_facts_once(config, args.limit))
        log_info("periodic_facts_runner.batch.summary", **counts)
        return
    if args.taxonomy:
        if args.loop:
            asyncio.run(
                run_with_service_heartbeat(
                    config,
                    lambda telemetry: run_taxonomy_loop(config, telemetry),
                )
            )
            return
        counts = asyncio.run(run_taxonomy_once(config))
        log_info("taxonomy_runner.batch.summary", **counts)
        return
    if args.all:
        log_info("all_runner.deprecated", replacement="split Dokploy worker profiles")
        asyncio.run(
            run_with_service_heartbeat(
                config,
                lambda telemetry: run_all_loop(
                    config, args.limit, interval_seconds, telemetry
                ),
            )
        )
        return

    if args.assets:
        if args.loop:
            asyncio.run(
                run_with_service_heartbeat(
                    config,
                    lambda telemetry: run_assets_loop(config, args.limit, interval_seconds),
                )
            )
            return

        counts = asyncio.run(run_assets_once(config, args.limit))
        log_info("assets_runner.batch.summary", **counts)
        return

    if args.domain_state:
        if args.loop:
            asyncio.run(
                run_with_service_heartbeat(
                    config,
                    lambda telemetry: run_domain_state_loop(
                        config,
                        args.limit,
                        args.interval_seconds or config.domain_state_poll_interval_seconds,
                    ),
                )
            )
            return True

        counts = asyncio.run(run_domain_state_once(config, args.limit))
        log_info("domain_state_runner.batch.summary", **counts)
        return


    if args.loop:
        asyncio.run(
            run_with_service_heartbeat(
                config,
                lambda telemetry: run_loop(config, args.limit, interval_seconds),
            )
        )
        return

    counts = asyncio.run(run_once(config, args.limit))
    log_info("traffic_runner.batch.summary", **counts)


if __name__ == "__main__":
    main()
