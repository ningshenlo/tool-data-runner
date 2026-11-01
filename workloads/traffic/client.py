from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from runner import (
    Config,
    log_debug,
    log_error,
    log_info,
    normalize_domain,
    parse_monthly_rows,
    redact_log_text,
    response_body_sample,
    response_header_summary,
)
from workloads.traffic.parse import (
    build_traffic_metrics,
    extract_brightdata_zone,
    latest_observed_traffic_month,
    parse_ai_traffic,
    parse_top_countries,
    parse_top_keywords,
    parse_traffic_sources,
    requested_month_has_traffic_data,
)
from workloads.traffic.types import FetchResult

async def fetch_similarweb_extension_version(timeout: float = 10.0) -> str | None:
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
    def __init__(self, config: Config):
        self.proxy_host = config.brightdata_proxy_host
        self.proxy_port = config.brightdata_proxy_port
        self.proxy_user = config.brightdata_proxy_user
        self.proxy_password = config.brightdata_proxy_password
        self.proxy_user_summary = mask_value(config.brightdata_proxy_user)
        self.extension_version: str | None = None
        log_info(
            "similarweb.client.config",
            proxy_host=self.proxy_host,
            proxy_port=self.proxy_port,
            proxy_user=self.proxy_user_summary,
            proxy_zone=extract_brightdata_zone(config.brightdata_proxy_user),
            proxy_user_has_session="-session-" in config.brightdata_proxy_user,
            proxy_session_per_fetch=True,
        )

    def build_proxy_url(self) -> str:
        session_id = generate_session_id()
        username = f"{self.proxy_user}-session-{session_id}"
        return f"http://{username}:{self.proxy_password}@{self.proxy_host}:{self.proxy_port}"

    async def get_extension_version(self) -> str:
        if self.extension_version:
            return self.extension_version
        try:
            version = await fetch_similarweb_extension_version()
        except Exception as error:
            log_error(
                "similarweb.extension_version.fetch_error",
                error_type=type(error).__name__,
                error=str(error)[:500],
            )
            version = None
        self.extension_version = version or SIMILARWEB_EXTENSION_VERSION_FALLBACK
        return self.extension_version

    async def fetch(self, domain: str, requested_month: str) -> FetchResult:
        clean_domain = normalize_domain(domain)
        if not clean_domain:
            log_info("similarweb.invalid_domain", domain=domain, requested_month=requested_month)
            return FetchResult(status="failed", monthly_rows=[], error="invalid_domain")

        extension_version = await self.get_extension_version()
        headers = {
            "User-Agent": SIMILARWEB_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
            "X-Extension-Version": extension_version,
        }
        url = f"{SIMILARWEB_API_BASE}?domain={clean_domain}"
        log_info("similarweb.fetch.start", domain=clean_domain, requested_month=requested_month)

        try:
            started_at = time.perf_counter()
            async with CurlAsyncSession() as client:
                response = await client.get(
                    url,
                    proxy=self.build_proxy_url(),
                    headers=headers,
                    timeout=25.0,
                    verify=False,
                    impersonate="chrome",
                    default_headers=True,
                )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        except Exception as error:
            log_error(
                "similarweb.fetch.request_error",
                domain=clean_domain,
                error_type=type(error).__name__,
                error=str(error)[:500],
                proxy_host=self.proxy_host,
                proxy_port=self.proxy_port,
                proxy_user=self.proxy_user_summary,
            )
            return FetchResult(status="failed", monthly_rows=[], error=f"request_error:{str(error)[:300]}")

        is_success = 200 <= response.status_code < 300
        response_history = getattr(response, "history", []) or []
        log_info(
            "similarweb.fetch.response",
            domain=clean_domain,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            final_host=urlsplit(str(response.url)).netloc,
            http_version=str(getattr(response, "http_version", "")),
            history_statuses=[item.status_code for item in response_history],
            **response_header_summary(response),
        )
        if not is_success:
            log_info(
                "similarweb.fetch.response_body",
                domain=clean_domain,
                status_code=response.status_code,
                body_sample=response_body_sample(response),
            )

        if response.status_code == 404:
            return FetchResult(status="no_data", monthly_rows=[], error="similarweb_no_data")
        if response.status_code == 403:
            return FetchResult(status="forbidden", monthly_rows=[], error="similarweb_forbidden")
        if response.status_code in (407, 429) or response.status_code >= 500:
            return FetchResult(
                status="failed",
                monthly_rows=[],
                error=f"similarweb_http_{response.status_code}:{response.text[:300]}",
            )
        if not is_success:
            return FetchResult(
                status="failed",
                monthly_rows=[],
                error=f"similarweb_http_{response.status_code}:{response.text[:300]}",
            )

        try:
            payload = response.json()
        except json.JSONDecodeError:
            log_error("similarweb.fetch.invalid_json", domain=clean_domain)
            return FetchResult(status="failed", monthly_rows=[], error="similarweb_invalid_json")

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
        )
        return FetchResult(
            status=status,
            monthly_rows=monthly_rows,
            error=error,
            observed_latest_month=observed_latest_month,
            raw_payload=payload,
        )
