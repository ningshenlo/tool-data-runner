from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit


_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


class SitemapUrlError(ValueError):
    pass


def _normalize_percent_encoding(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        character = chr(byte)
        return character if character in _UNRESERVED else f"%{byte:02X}"

    return _PERCENT_ESCAPE.sub(replace, value)


def _normalized_netloc(parts: SplitResult) -> str:
    if parts.username is not None or parts.password is not None:
        raise SitemapUrlError("Sitemap URLs must not contain credentials.")
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as error:
        raise SitemapUrlError("Sitemap URL has an invalid host or port.") from error
    if not host:
        raise SitemapUrlError("Sitemap URL must have a host.")

    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SitemapUrlError("Sitemap URL must not target a non-public IP address.")

    display_host = f"[{host}]" if ":" in host else host
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    return display_host if port is None or default_port else f"{display_host}:{port}"


def normalize_sitemap_url(
    value: str,
    *,
    base_url: str | None = None,
    max_length: int = 8_192,
) -> str:
    raw = (value or "").strip()
    if not raw:
        raise SitemapUrlError("Sitemap URL must not be empty.")
    if base_url:
        raw = urljoin(base_url, raw)
    if len(raw) > max_length:
        raise SitemapUrlError("Sitemap URL exceeds the configured length limit.")
    try:
        parts = urlsplit(raw)
    except ValueError as error:
        raise SitemapUrlError("Sitemap URL is malformed.") from error
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SitemapUrlError("Sitemap URL must use HTTP or HTTPS.")
    netloc = _normalized_netloc(parts)
    path = _normalize_percent_encoding(parts.path or "/")
    query = _normalize_percent_encoding(parts.query)
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if len(normalized) > max_length:
        raise SitemapUrlError("Normalized sitemap URL exceeds the configured length limit.")
    return normalized


def sitemap_url_hash(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
