from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from io import BytesIO

from .config import MonitorLimits
from .models import SitemapDocument, SitemapEntry, SitemapKind
from .normalize import SitemapUrlError, normalize_sitemap_url, sitemap_url_hash


_XML_DECLARATION = re.compile(br"^\s*<\?xml\b", re.IGNORECASE)
_HTML_DOCTYPE = re.compile(br"^\s*<!\s*DOCTYPE\s+html\b", re.IGNORECASE)
_FORBIDDEN_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class SitemapParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, child_name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == child_name:
            text = (child.text or "").strip()
            return text or None
    return None


def _deduplicate(entries: Iterable[SitemapEntry]) -> tuple[SitemapEntry, ...]:
    grouped: dict[str, list[SitemapEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.normalized_url, []).append(entry)

    stable: list[SitemapEntry] = []
    for normalized_url, candidates in grouped.items():
        raw_url = min(candidate.raw_url for candidate in candidates)
        lastmods = [candidate.lastmod for candidate in candidates if candidate.lastmod]
        stable.append(
            SitemapEntry(
                normalized_url=normalized_url,
                raw_url=raw_url,
                url_hash=sitemap_url_hash(normalized_url),
                lastmod=max(lastmods) if lastmods else None,
            )
        )
    return tuple(sorted(stable, key=lambda item: (item.url_hash, item.normalized_url)))


def _entry(raw_url: str, lastmod: str | None, source_url: str, limits: MonitorLimits) -> SitemapEntry:
    try:
        normalized = normalize_sitemap_url(
            raw_url,
            base_url=source_url,
            max_length=limits.max_url_length,
        )
    except SitemapUrlError as error:
        raise SitemapParseError("invalid_url", str(error)) from error
    return SitemapEntry(
        normalized_url=normalized,
        raw_url=raw_url.strip(),
        url_hash=sitemap_url_hash(normalized),
        lastmod=(lastmod or "").strip() or None,
    )


def _parse_xml(body: bytes, source_url: str, limits: MonitorLimits) -> SitemapDocument:
    if _HTML_DOCTYPE.match(body):
        raise SitemapParseError(
            "unsupported_xml_root",
            "Expected urlset or sitemapindex, received html.",
        )
    if _FORBIDDEN_XML.search(body):
        raise SitemapParseError("unsafe_xml", "DOCTYPE and ENTITY declarations are not allowed.")
    root_name: str | None = None
    item_name: str | None = None
    kind: SitemapKind | None = None
    entries: list[SitemapEntry] = []
    try:
        for event, element in ET.iterparse(BytesIO(body), events=("start", "end")):
            element_name = _local_name(element.tag)
            if event == "start" and root_name is None:
                root_name = element_name
                if root_name == "urlset":
                    item_name = "url"
                    kind = "urlset"
                elif root_name == "sitemapindex":
                    item_name = "sitemap"
                    kind = "sitemap_index"
                else:
                    raise SitemapParseError(
                        "unsupported_xml_root",
                        f"Expected urlset or sitemapindex, received {root_name or 'unknown'}.",
                    )
                continue
            if event != "end" or element_name != item_name:
                continue
            loc = _child_text(element, "loc")
            if not loc:
                raise SitemapParseError("missing_loc", f"A {item_name} entry is missing loc.")
            entries.append(_entry(loc, _child_text(element, "lastmod"), source_url, limits))
            element.clear()
            if len(entries) > limits.max_url_count:
                raise SitemapParseError(
                    "url_limit_exceeded",
                    "Sitemap URL count exceeds the configured limit.",
                )
    except ET.ParseError as error:
        raise SitemapParseError("malformed_xml", f"Malformed sitemap XML: {error}.") from error
    if kind is None:
        raise SitemapParseError("malformed_xml", "Sitemap XML does not contain a root element.")
    return SitemapDocument(
        entries=_deduplicate(entries),
        kind=kind,
        source_url=source_url,
        raw_content=body,
    )


def _looks_like_xml(body: bytes, content_type: str | None) -> bool:
    media_type = (content_type or "").partition(";")[0].strip().lower()
    prefix = body.lstrip()[:64].lower()
    return (
        bool(_XML_DECLARATION.match(body))
        or prefix.startswith(b"<")
        or media_type in {"application/xml", "text/xml"}
        or media_type.endswith("+xml")
    )


def _parse_text(body: bytes, source_url: str, limits: MonitorLimits) -> SitemapDocument:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SitemapParseError("invalid_encoding", "Text sitemap must be UTF-8.") from error

    entries: list[SitemapEntry] = []
    for line in text.splitlines():
        raw_url = line.strip()
        if not raw_url or raw_url.startswith("#"):
            continue
        entries.append(_entry(raw_url, None, source_url, limits))
        if len(entries) > limits.max_url_count:
            raise SitemapParseError("url_limit_exceeded", "Sitemap URL count exceeds the configured limit.")
    if not entries:
        raise SitemapParseError("empty_sitemap", "Sitemap does not contain any URL entries.")
    return SitemapDocument(
        entries=_deduplicate(entries),
        kind="text",
        source_url=source_url,
        raw_content=body,
    )


def parse_sitemap(
    body: bytes,
    source_url: str,
    *,
    content_type: str | None = None,
    limits: MonitorLimits | None = None,
) -> SitemapDocument:
    active_limits = limits or MonitorLimits()
    if len(body) > active_limits.max_decompressed_bytes:
        raise SitemapParseError(
            "decompressed_limit_exceeded",
            "Sitemap exceeds the configured decompressed byte limit.",
        )
    return (
        _parse_xml(body, source_url, active_limits)
        if _looks_like_xml(body, content_type)
        else _parse_text(body, source_url, active_limits)
    )
