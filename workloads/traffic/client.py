from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from config.constants import (
    SIMILARWEB_EXTENSION_ID,
    SIMILARWEB_EXTENSION_UPDATE_URL,
)
from libs.domains import normalize_domain
from libs.logging_util import log_error, log_info
from workloads.traffic.parse import (
    latest_observed_traffic_month,
    parse_monthly_rows,
    requested_month_has_traffic_data,
)
from workloads.traffic.types import FetchResult

if TYPE_CHECKING:
    from runner import Config

# The unified service retries once per domain and caps a request at 90 seconds.
TRAFFIC_SERVICE_TIMEOUT_SECONDS = 100.0


async def fetch_similarweb_extension_version(timeout: float = 10.0) -> str | None:
    """Diagnostics only: the unified traffic service owns the request protocol."""
    params = {
        "response": "updatecheck",
        "prodversion": "120.0",
        "acceptformat": "crx2,crx3",
        "x": f"id={SIMILARWEB_EXTENSION_ID}&installsource=ondemand&uc",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(SIMILARWEB_EXTENSION_UPDATE_URL, params=params)
    response.raise_for_status()
    match = re.search(r'<updatecheck[^>]*\bversion="([^"]+)"', response.text)
    return match.group(1) if match else None


class SimilarWebClient:
    """Thin client of the unified traffic service (traffic-desk-executor).

    Similarweb signing, Bright Data proxy sessions, TLS fingerprinting, 429
    backoff and the shared 24h cache all live in the service. This client only
    asks for the raw Similarweb payload and keeps the existing local parsing, so
    parse/store/pipeline behavior is unchanged.
    """

    def __init__(self, config: Config):
        self.service_url = (getattr(config, "traffic_service_url", "") or "https://will-api.sigpik.com").rstrip("/")
        self.service_token = getattr(config, "traffic_service_token", "") or ""
        log_info(
            "similarweb.client.config",
            traffic_service_url=self.service_url,
            traffic_service_token_configured=bool(self.service_token),
        )

    async def _query(self, clean_domain: str, bypass_cache: bool) -> tuple[dict[str, Any] | None, FetchResult | None]:
        """Return (service result item, None) or (None, failure FetchResult)."""
        body: dict[str, Any] = {"url": clean_domain, "include_raw": True}
        if bypass_cache:
            body["bypass_cache"] = True
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=TRAFFIC_SERVICE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.service_url}/v1/traffic",
                    headers={"Authorization": f"Bearer {self.service_token}"},
                    json=body,
                )
        except httpx.HTTPError as error:
            log_error(
                "similarweb.fetch.request_error",
                domain=clean_domain,
                error_type=type(error).__name__,
                error=str(error)[:500],
            )
            return None, FetchResult(status="failed", monthly_rows=[], error=f"request_error:{type(error).__name__}")
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if response.status_code != 200:
            log_error(
                "similarweb.fetch.service_http_error",
                domain=clean_domain,
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            return None, FetchResult(
                status="failed",
                monthly_rows=[],
                error=f"traffic_service_http_{response.status_code}",
            )
        try:
            item = response.json()["results"][0]
            if not isinstance(item, dict):
                raise TypeError("result item is not an object")
        except (ValueError, KeyError, IndexError, TypeError):
            log_error("similarweb.fetch.invalid_service_response", domain=clean_domain)
            return None, FetchResult(status="failed", monthly_rows=[], error="traffic_service_invalid_response")
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        log_info(
            "similarweb.fetch.response",
            domain=clean_domain,
            status=item.get("status"),
            cached=bool(item.get("cached")),
            fetched_at=item.get("fetched_at"),
            error_code=error.get("code"),
            elapsed_ms=elapsed_ms,
        )
        return item, None

    async def fetch(self, domain: str, requested_month: str) -> FetchResult:
        clean_domain = normalize_domain(domain)
        if not clean_domain:
            log_info("similarweb.invalid_domain", domain=domain, requested_month=requested_month)
            return FetchResult(status="failed", monthly_rows=[], error="invalid_domain")
        if not self.service_token:
            log_error("similarweb.fetch.service_not_configured", domain=clean_domain)
            return FetchResult(status="failed", monthly_rows=[], error="traffic_service_not_configured")

        log_info("similarweb.fetch.start", domain=clean_domain, requested_month=requested_month)
        item, failure = await self._query(clean_domain, bypass_cache=False)
        if failure:
            return failure
        result = self._to_fetch_result(item, clean_domain, requested_month)
        # A cached payload may predate Similarweb's release of the requested
        # month. Re-check upstream once before reporting the month unavailable.
        if item.get("cached") and result.status == "no_data":
            item, failure = await self._query(clean_domain, bypass_cache=True)
            if failure:
                return failure
            result = self._to_fetch_result(item, clean_domain, requested_month)
        return result

    def _to_fetch_result(self, item: dict[str, Any], clean_domain: str, requested_month: str) -> FetchResult:
        status = item.get("status")
        if status == "no_data":
            return FetchResult(status="no_data", monthly_rows=[], error="similarweb_no_data")
        if status == "invalid_url":
            return FetchResult(status="failed", monthly_rows=[], error="invalid_domain")
        if status != "ok":
            error = item.get("error") if isinstance(item.get("error"), dict) else {}
            code = str(error.get("code") or "unknown")
            if code == "upstream_http_403":
                return FetchResult(status="forbidden", monthly_rows=[], error="similarweb_forbidden")
            return FetchResult(status="failed", monthly_rows=[], error=f"traffic_service:{code}"[:300])
        payload = item.get("raw")
        if not isinstance(payload, dict):
            log_error("similarweb.fetch.missing_raw_payload", domain=clean_domain)
            return FetchResult(status="failed", monthly_rows=[], error="traffic_service_missing_raw")

        monthly_rows = parse_monthly_rows(payload, clean_domain, requested_month)
        observed_latest_month = latest_observed_traffic_month(monthly_rows)
        has_requested_month = requested_month_has_traffic_data(monthly_rows, requested_month)
        status = "done" if has_requested_month else "no_data"
        error = None if has_requested_month else f"requested_month_unavailable:latest={observed_latest_month or 'none'}"
        log_info(
            "similarweb.fetch.parsed",
            domain=clean_domain,
            status=status,
            requested_month=requested_month,
            observed_latest_month=observed_latest_month,
            monthly_rows=len(monthly_rows),
            cached=bool(item.get("cached")),
        )
        return FetchResult(
            status=status,
            monthly_rows=monthly_rows,
            error=error,
            observed_latest_month=observed_latest_month,
            raw_payload=payload,
        )
