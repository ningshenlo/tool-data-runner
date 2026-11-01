from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from runner import (
    AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
    AHREFS_DOMAIN_RATING_URL,
    AHREFS_MAX_REQUESTS_PER_MINUTE,
    AsyncRequestPacer,
    Config,
    IANA_RDAP_DNS,
    RDAP_USER_AGENT,
    log_debug,
    log_error,
    log_info,
    normalize_domain,
    normalize_rdap_domain,
    parse_iso_timestamp,
    parse_retry_after_seconds,
    to_number,
    utc_now_iso,
)
from workloads.domain_state.types import DomainStateResult

def extract_rdap_created_at(payload: dict[str, Any]) -> str | None:
    events = payload.get("events")
    if not isinstance(events, list):
        return None

    action_groups = [
        {"registration"},
        {"created", "creation", "registered"},
    ]
    for actions in action_groups:
        for event in events:
            if not isinstance(event, dict):
                continue
            action = str(event.get("eventAction") or "").lower()
            event_date = parse_iso_timestamp(event.get("eventDate"))
            if action in actions and event_date:
                return event_date
    return None

def find_rdap_base_urls(domain: str, bootstrap: dict[str, Any]) -> list[str]:
    labels = [part for part in domain.split(".") if part]
    suffixes = [".".join(labels[index:]) for index in range(len(labels))]
    best_match = ""
    best_urls: list[str] = []

    services = bootstrap.get("services")
    if not isinstance(services, list):
        return []

    for service in services:
        if not isinstance(service, list) or len(service) != 2:
            continue
        tlds, urls = service
        if not isinstance(tlds, list) or not isinstance(urls, list):
            continue
        tld_set = {str(tld).lower().lstrip(".") for tld in tlds}
        for suffix in suffixes:
            if suffix in tld_set and len(suffix) > len(best_match):
                best_match = suffix
                best_urls = [str(url).strip() for url in urls if str(url).strip()]

    return best_urls

class DomainStateClient:
    def __init__(
        self,
        ahref_api_key: str,
        requests_per_minute: int = AHREFS_DEFAULT_REQUESTS_PER_MINUTE,
        request_pacer: Any | None = None,
    ) -> None:
        self.ahref_api_key = ahref_api_key.strip()
        self.request_pacer = request_pacer or AsyncRequestPacer(requests_per_minute)

    async def fetch(
        self,
        domain: str,
        *,
        fetch_domain_rating: bool = True,
        fetch_rdap: bool = True,
    ) -> DomainStateResult:
        async def safely(operation: Any) -> DomainStateResult:
            try:
                return await operation
            except Exception as error:
                return DomainStateResult(
                    status="failed",
                    domain_rating=None,
                    domain_created_at=None,
                    error=str(error)[:300],
                )

        ahrefs_result: DomainStateResult | None = None
        rdap_result: DomainStateResult | None = None
        if fetch_domain_rating and fetch_rdap:
            ahrefs_result, rdap_result = await asyncio.gather(
                safely(self.fetch_ahrefs_domain_rating(domain)),
                safely(self.fetch_domain_created_at(domain)),
            )
        elif fetch_domain_rating:
            ahrefs_result = await safely(self.fetch_ahrefs_domain_rating(domain))
        elif fetch_rdap:
            rdap_result = await safely(self.fetch_domain_created_at(domain))

        # DR owns the recurring task outcome and retry policy. RDAP is a one-time
        # observation: even a failed/no-data response is persisted and not retried.
        status = ahrefs_result.status if ahrefs_result is not None else "done"
        error = ahrefs_result.error if ahrefs_result is not None else None
        return DomainStateResult(
            status=status,
            domain_rating=ahrefs_result.domain_rating if ahrefs_result is not None else None,
            domain_created_at=rdap_result.domain_created_at if rdap_result is not None else None,
            error=error,
            rdap_status=rdap_result.status if rdap_result is not None else None,
            rdap_error=rdap_result.error if rdap_result is not None else None,
            retry_after_seconds=(
                ahrefs_result.retry_after_seconds if ahrefs_result is not None else None
            ),
        )

    async def fetch_ahrefs_domain_rating(self, domain: str) -> DomainStateResult:
        clean_domain = normalize_domain(domain)
        if not clean_domain:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error="invalid_domain")
        if not self.ahref_api_key:
            raise RuntimeError("AHREF_API_KEY is required for Ahrefs Domain Rating requests")

        endpoint = httpx.URL(AHREFS_DOMAIN_RATING_URL).copy_add_param("target", clean_domain)
        await self.request_pacer.wait()
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.get(
                    endpoint,
                    headers={
                        "Accept": "application/json,text/plain,*/*",
                        "Authorization": f"Bearer {self.ahref_api_key}",
                    },
                )
        except Exception as error:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error=str(error)[:300])

        if response.status_code == 404:
            return DomainStateResult(status="no_data", domain_rating=None, domain_created_at=None, error="ahrefs_not_found")
        if response.status_code == 429:
            return DomainStateResult(
                status="failed",
                domain_rating=None,
                domain_created_at=None,
                error=f"Ahrefs HTTP 429: {response.text[:300]}",
                retry_after_seconds=parse_retry_after_seconds(
                    response.headers.get("retry-after")
                ),
            )
        if not response.is_success:
            raise RuntimeError(f"Ahrefs HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise RuntimeError("Ahrefs returned invalid JSON")

        raw_rating = (payload.get("domain_rating") or {}).get("domain_rating") if isinstance(payload.get("domain_rating"), dict) else payload.get("domain_rating")
        rating = to_number(raw_rating)
        created_at = parse_iso_timestamp(
            payload.get("domain_created_at")
            or payload.get("created_at")
            or ((payload.get("domain_rating") or {}).get("domain_created_at") if isinstance(payload.get("domain_rating"), dict) else None)
        )
        if rating is None:
            return DomainStateResult(status="no_data", domain_rating=None, domain_created_at=created_at, error="ahrefs_domain_rating_not_found")

        return DomainStateResult(
            status="done",
            domain_rating=min(max(rating, 0), 100),
            domain_created_at=created_at,
        )

    async def fetch_rdap_json(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/rdap+json, application/json",
                    "User-Agent": RDAP_USER_AGENT,
                },
            )
        if response.status_code == 404:
            raise FileNotFoundError("rdap_http_404")
        if not response.is_success:
            raise RuntimeError(f"RDAP HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise RuntimeError("RDAP returned invalid JSON")
        return payload if isinstance(payload, dict) else {}

    async def fetch_domain_created_at(self, domain: str) -> DomainStateResult:
        clean_domain = normalize_rdap_domain(domain)
        if not clean_domain:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error="invalid_domain")

        try:
            bootstrap = await self.fetch_rdap_json(IANA_RDAP_DNS)
            base_urls = find_rdap_base_urls(clean_domain, bootstrap)
        except Exception as error:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error=str(error)[:300])

        if not base_urls:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error=f"rdap_service_not_found:{clean_domain}")

        last_error = ""
        for base_url in base_urls:
            url = f"{base_url.rstrip('/')}/domain/{clean_domain}"
            try:
                payload = await self.fetch_rdap_json(url)
            except FileNotFoundError:
                return DomainStateResult(status="no_data", domain_rating=None, domain_created_at=None, error="rdap_http_404")
            except Exception as error:
                last_error = str(error)[:300]
                continue

            created_at = extract_rdap_created_at(payload)
            return DomainStateResult(
                status="done" if created_at else "no_data",
                domain_rating=None,
                domain_created_at=created_at,
                error=None if created_at else "created_at_not_found",
            )

        return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error=f"rdap_query_failed:{last_error}")
