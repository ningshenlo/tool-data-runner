from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from .config import MonitorLimits
from .normalize import SitemapUrlError, normalize_sitemap_url


_SITEMAP_DIRECTIVE = re.compile(r"^\s*sitemap\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
COMMON_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")


def site_origin(homepage_url: str, limits: MonitorLimits | None = None) -> str:
    active_limits = limits or MonitorLimits()
    normalized = normalize_sitemap_url(homepage_url, max_length=active_limits.max_url_length)
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def robots_url(homepage_url: str, limits: MonitorLimits | None = None) -> str:
    return f"{site_origin(homepage_url, limits)}/robots.txt"


def parse_robots_sitemaps(
    body: bytes,
    robots_source_url: str,
    *,
    limits: MonitorLimits | None = None,
) -> tuple[str, ...]:
    active_limits = limits or MonitorLimits()
    try:
        text = body.decode("utf-8-sig", errors="replace")
    except Exception:
        return ()
    found: list[str] = []
    for line in text.splitlines():
        match = _SITEMAP_DIRECTIVE.match(line)
        if not match:
            continue
        try:
            candidate = normalize_sitemap_url(
                match.group(1),
                base_url=robots_source_url,
                max_length=active_limits.max_url_length,
            )
        except SitemapUrlError:
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def discovery_candidates(
    homepage_url: str,
    *,
    explicit_sitemaps: Iterable[str] = (),
    robots_sitemaps: Iterable[str] = (),
    include_common_paths: bool = True,
    limits: MonitorLimits | None = None,
) -> tuple[str, ...]:
    active_limits = limits or MonitorLimits()
    origin = site_origin(homepage_url, active_limits)
    ordered = [*explicit_sitemaps, *robots_sitemaps]
    if include_common_paths:
        ordered.extend(f"{origin}{path}" for path in COMMON_SITEMAP_PATHS)
    result: list[str] = []
    for raw in ordered:
        try:
            normalized = normalize_sitemap_url(
                raw,
                base_url=origin,
                max_length=active_limits.max_url_length,
            )
        except SitemapUrlError:
            continue
        if normalized not in result:
            result.append(normalized)
    return tuple(result)
