from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from urllib.parse import urlsplit

import httpx

from .config import MonitorLimits
from .models import FetchResult, FetchValidators
from .normalize import SitemapUrlError, normalize_sitemap_url


class SitemapFetchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _is_public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _resolve_public_via_doh(hostname: str) -> bool:
    """Resolve through a fixed public DoH endpoint when local DNS is synthetic."""

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        responses = await asyncio.gather(
            *(
                client.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": hostname, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                )
                for record_type in ("A", "AAAA")
            )
        )
    addresses: set[str] = set()
    for response in responses:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("Status") != 0:
            return False
        answers = payload.get("Answer") or []
        if not isinstance(answers, list):
            return False
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("type") not in {1, 28}:
                continue
            value = str(answer.get("data") or "").strip()
            try:
                ipaddress.ip_address(value)
            except ValueError:
                return False
            addresses.add(value)
    return bool(addresses) and all(_is_public_address(address) for address in addresses)


async def assert_public_http_target(
    url: str,
    *,
    allow_synthetic_dns_doh: bool = False,
) -> None:
    """Best-effort SSRF guard before each external request and redirect result."""

    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise SitemapFetchError("blocked_target", "Localhost sitemap targets are not allowed.", retryable=False)
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise SitemapFetchError("dns_error", f"Could not resolve sitemap host {hostname}.", retryable=True) from error
    addresses = {record[4][0] for record in records}
    if addresses and all(_is_public_address(address) for address in addresses):
        return
    if allow_synthetic_dns_doh:
        try:
            if await _resolve_public_via_doh(hostname):
                return
        except (httpx.HTTPError, ValueError):
            pass
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise SitemapFetchError(
            "blocked_target",
            "Sitemap target resolved to a non-public network address.",
            retryable=False,
        )


def _decompress_gzip(payload: bytes, max_bytes: int) -> bytes:
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        output = bytearray()
        cursor = 0
        while cursor < len(payload):
            chunk = payload[cursor : cursor + 64 * 1024]
            cursor += len(chunk)
            output.extend(decompressor.decompress(chunk, max_bytes - len(output) + 1))
            if len(output) > max_bytes:
                raise SitemapFetchError(
                    "decompressed_limit_exceeded",
                    "Sitemap exceeds the configured decompressed byte limit.",
                    retryable=False,
                )
        output.extend(decompressor.flush(max_bytes - len(output) + 1))
    except zlib.error as error:
        raise SitemapFetchError("invalid_gzip", "Sitemap gzip payload is malformed.", retryable=False) from error
    if len(output) > max_bytes:
        raise SitemapFetchError(
            "decompressed_limit_exceeded",
            "Sitemap exceeds the configured decompressed byte limit.",
            retryable=False,
        )
    return bytes(output)


def decode_sitemap_payload(
    payload: bytes,
    *,
    content_encoding: str | None,
    final_url: str,
    max_decompressed_bytes: int,
) -> bytes:
    encoding = (content_encoding or "").strip().lower()
    if encoding not in {"", "gzip", "identity"}:
        raise SitemapFetchError(
            "unsupported_content_encoding",
            f"Unsupported sitemap Content-Encoding: {encoding}.",
            retryable=False,
        )
    decoded = _decompress_gzip(payload, max_decompressed_bytes) if encoding == "gzip" else payload

    # A .xml.gz object may itself be transported with HTTP gzip. Decode the file
    # layer only when gzip magic remains, so ordinary XML is never double-decoded.
    if decoded.startswith(b"\x1f\x8b") and (final_url.lower().endswith(".gz") or encoding != "gzip"):
        decoded = _decompress_gzip(decoded, max_decompressed_bytes)
    if len(decoded) > max_decompressed_bytes:
        raise SitemapFetchError(
            "decompressed_limit_exceeded",
            "Sitemap exceeds the configured decompressed byte limit.",
            retryable=False,
        )
    return decoded


class SitemapHttpFetcher:
    def __init__(
        self,
        limits: MonitorLimits | None = None,
        *,
        user_agent: str = "SigpikSitemapMonitor/1.0 (+https://sigpik.com)",
        validate_public_targets: bool = True,
        allow_synthetic_dns_doh: bool = False,
    ):
        self.limits = limits or MonitorLimits()
        self.user_agent = user_agent
        self.validate_public_targets = validate_public_targets
        self.allow_synthetic_dns_doh = allow_synthetic_dns_doh
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.limits.request_timeout_seconds),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        url: str,
        validators: FetchValidators | None = None,
        *,
        max_download_bytes: int | None = None,
    ) -> FetchResult:
        try:
            normalized_url = normalize_sitemap_url(url, max_length=self.limits.max_url_length)
        except SitemapUrlError as error:
            raise SitemapFetchError("invalid_url", str(error), retryable=False) from error
        headers = {
            "Accept": "application/xml,text/xml,text/plain,application/gzip;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
        }
        active_validators = validators or FetchValidators()
        if active_validators.etag:
            headers["If-None-Match"] = active_validators.etag
        if active_validators.last_modified:
            headers["If-Modified-Since"] = active_validators.last_modified

        byte_limit = max_download_bytes or self.limits.max_download_bytes
        current_url = normalized_url
        try:
            client = self._http_client()
            for redirect_count in range(self.limits.redirect_limit + 1):
                if self.validate_public_targets:
                    await assert_public_http_target(
                        current_url,
                        allow_synthetic_dns_doh=self.allow_synthetic_dns_doh,
                    )
                async with client.stream("GET", current_url, headers=headers) as response:
                    final_url = normalize_sitemap_url(str(response.url), max_length=self.limits.max_url_length)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return FetchResult(
                                body=None,
                                bytes_downloaded=0,
                                content_type=response.headers.get("content-type"),
                                etag=response.headers.get("etag"),
                                final_url=final_url,
                                last_modified=response.headers.get("last-modified"),
                                retry_after=response.headers.get("retry-after"),
                                status_code=response.status_code,
                            )
                        if redirect_count >= self.limits.redirect_limit:
                            raise SitemapFetchError(
                                "too_many_redirects",
                                "Sitemap exceeded the redirect limit.",
                                retryable=False,
                            )
                        try:
                            current_url = normalize_sitemap_url(
                                location,
                                base_url=final_url,
                                max_length=self.limits.max_url_length,
                            )
                        except SitemapUrlError as error:
                            raise SitemapFetchError("invalid_redirect", str(error), retryable=False) from error
                        continue

                    common = {
                        "bytes_downloaded": 0,
                        "content_type": response.headers.get("content-type"),
                        "etag": response.headers.get("etag"),
                        "final_url": final_url,
                        "last_modified": response.headers.get("last-modified"),
                        "retry_after": response.headers.get("retry-after"),
                        "status_code": response.status_code,
                    }
                    if response.status_code == 304:
                        return FetchResult(body=None, **common)
                    if response.status_code != 200:
                        return FetchResult(body=None, **common)

                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > byte_limit:
                        raise SitemapFetchError(
                            "download_limit_exceeded",
                            "Sitemap Content-Length exceeds the configured byte limit.",
                            retryable=False,
                        )
                    payload = bytearray()
                    async for chunk in response.aiter_raw():
                        payload.extend(chunk)
                        if len(payload) > byte_limit:
                            raise SitemapFetchError(
                                "download_limit_exceeded",
                                "Sitemap download exceeds the configured byte limit.",
                                retryable=False,
                            )
                    body = decode_sitemap_payload(
                        bytes(payload),
                        content_encoding=response.headers.get("content-encoding"),
                        final_url=final_url,
                        max_decompressed_bytes=self.limits.max_decompressed_bytes,
                    )
                    return FetchResult(body=body, **{**common, "bytes_downloaded": len(payload)})
            raise SitemapFetchError(
                "too_many_redirects",
                "Sitemap exceeded the redirect limit.",
                retryable=False,
            )
        except SitemapFetchError:
            raise
        except httpx.TimeoutException as error:
            raise SitemapFetchError("timeout", "Sitemap request timed out.", retryable=True) from error
        except httpx.RequestError as error:
            raise SitemapFetchError("request_error", f"Sitemap request failed: {error}.", retryable=True) from error
