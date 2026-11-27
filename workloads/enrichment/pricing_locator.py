"""Multi-rule pricing & contact-sales page locator extracted from legacy runner."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from libs.http import browser_request_headers

COMMON_PRICING_PATHS = (
    "/pricing",
    "/pricing/",
    "/plans",
    "/plans/",
    "/subscribe",
    "/subscribe/",
    "/subscription",
    "/subscription/",
    "/subscriptions",
    "/subscriptions/",
    "/upgrade",
    "/upgrade/",
    "/pricing-page",
    "/pricing-plans",
    "/plans-pricing",
    "/billing",
)

COMMON_CONTACT_SALES_PATHS = (
    "/contact-sales",
    "/book-a-demo",
    "/book-a-demo-call",
    "/request-demo",
    "/demo",
    "/contact",
    "/contact-us",
    "/enterprise",
)

BAD_PRICING_PATH_PARTS = {
    "article",
    "articles",
    "blog",
    "buy",
    "cart",
    "careers",
    "case-study",
    "case-studies",
    "community",
    "docs",
    "guide",
    "help",
    "help-center",
    "issues",
    "legal",
    "news",
    "policy",
    "privacy",
    "privacy-policy",
    "resources",
    "release-note",
    "release-notes",
    "refund",
    "search",
    "shop",
    "store",
    "support",
    "terms",
    "terms-of-use",
}

PRICING_PATH_PARTS = {
    "pricing",
    "prices",
    "plans",
    "pricing-plans",
    "plans-pricing",
    "billing",
    "subscribe",
    "subscription",
    "subscriptions",
    "upgrade",
}

CONTACT_SALES_PATH_PARTS = {
    "book-a-demo",
    "book-a-demo-call",
    "contact-sales",
    "request-demo",
    "schedule-demo",
    "demo",
    "contact",
    "contact-us",
    "enterprise",
    "sales",
}

MAX_PRICING_HTML_BYTES = 450_000
MAX_SITEMAP_BYTES = 450_000


class PricingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._text_chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"script", "style", "noscript", "svg", "template"}:
            self._skip_depth += 1
            return
        if lower_tag == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self.links.append(value.strip())
                    break

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"script", "style", "noscript", "svg", "template"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if lower_tag in {"p", "div", "li", "section", "article", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self._text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data and data.strip():
            self._text_chunks.append(data)

    @property
    def text(self) -> str:
        joined = " ".join(self._text_chunks)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def normalize_pricing_url(value: str) -> str:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid URL: {value}")
    return parsed.geturl()


def pricing_url_origin(value: str) -> str:
    parsed = urlsplit(normalize_pricing_url(value))
    return f"{parsed.scheme}://{parsed.netloc}"


def is_bad_pricing_url(value: str) -> bool:
    parsed = urlsplit(value)
    parts = {part.lower() for part in parsed.path.split("/") if part}
    if parts & BAD_PRICING_PATH_PARTS:
        return True
    if any(part.endswith("-policy") or part.endswith("-terms") for part in parts):
        return True
    return any(part.startswith("api-") or part.endswith("-api") for part in parts)


def is_pricing_path_part(part: str) -> bool:
    normalized = part.lower()
    return (
        normalized in PRICING_PATH_PARTS
        or "pricing" in normalized
        or normalized in {"price", "plans", "billing", "upgrade"}
    )


def is_pricing_fragment(fragment: str) -> bool:
    normalized = fragment.lower().strip()
    return normalized in {"pricing", "plans", "price", "billing", "subscribe", "subscription"} or "pricing" in normalized


def is_strict_pricing_url(value: str) -> bool:
    parsed = urlsplit(value)
    parts = {part.lower() for part in parsed.path.split("/") if part}
    if is_bad_pricing_url(value):
        return False
    if is_pricing_fragment(parsed.fragment):
        return True
    if not parts:
        return False
    return any(is_pricing_path_part(part) for part in parts)


def is_contact_sales_url(value: str) -> bool:
    parsed = urlsplit(value)
    parts = {part.lower() for part in parsed.path.split("/") if part}
    if not parts or is_bad_pricing_url(value):
        return False
    return bool(parts & CONTACT_SALES_PATH_PARTS)


def pricing_url_score(value: str) -> int:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return -1000
    parts = [part.lower() for part in parsed.path.split("/") if part]
    if not parts:
        return 75 if is_pricing_fragment(parsed.fragment) else -50
    if is_bad_pricing_url(value):
        return -200

    score = 0
    depth = len(parts)
    if depth == 1:
        score += 35
    elif depth == 2:
        score += 20
    elif depth >= 4:
        score -= 25

    for part in parts:
        if part in {"pricing", "pricing-page", "pricing-plans", "plans-pricing"}:
            score += 100
        elif "pricing" in part:
            score += 85
        elif part in {"plans", "billing", "upgrade", "subscribe", "subscription", "subscriptions"}:
            score += 55
        elif part in {"enterprise", "contact-sales", "contact"}:
            score -= 20

    if is_pricing_fragment(parsed.fragment):
        score += 75
    if parsed.query:
        score -= 5
    return score


def contact_sales_url_score(value: str) -> int:
    if not is_contact_sales_url(value):
        return -1000
    parsed = urlsplit(value)
    parts = [part.lower() for part in parsed.path.split("/") if part]
    score = 0
    for part in parts:
        if part in {"contact-sales", "book-a-demo", "book-a-demo-call", "request-demo", "schedule-demo"}:
            score += 80
        elif part in {"demo", "sales", "enterprise"}:
            score += 45
        elif part in {"contact", "contact-us"}:
            score += 25
    if len(parts) == 1:
        score += 20
    return score


def parse_pricing_html(value: str) -> PricingHtmlParser:
    parser = PricingHtmlParser()
    parser.feed(value or "")
    return parser


def pricing_text_quality(text: str) -> int:
    lower = text.lower()
    score = 0
    score += len(re.findall(r"(?:\$|€|£|usd\s?)\s?\d", lower)) * 3
    score += len(re.findall(r"\b(?:pricing|plans?|free tier|free plan|freemium|monthly|yearly|annual|per month|per user|contact sales|enterprise|credits?)\b", lower))
    score -= len(re.findall(r"\b(?:blog|privacy|terms|careers|cookie|shopping|cart)\b", lower)) * 2
    return score


def extract_sitemap_locs(sitemap_body: str) -> list[str]:
    body = sitemap_body.strip()
    if not body:
        return []
    locs: list[str] = []
    try:
        root = ET.fromstring(body)
        for element in root.iter():
            if element.tag.endswith("loc") and element.text:
                locs.append(element.text.strip())
    except ET.ParseError:
        locs.extend(match.group(1).strip() for match in re.finditer(r"<loc>\s*([^<]+?)\s*</loc>", body, re.I))
    return [loc for loc in locs if loc.startswith(("http://", "https://"))]


def add_pricing_candidate(urls: list[str], seen: set[str], candidate: str, origin: str) -> None:
    try:
        normalized = normalize_pricing_url(candidate)
    except ValueError:
        return
    if urlsplit(normalized).netloc != urlsplit(origin).netloc:
        return
    if not is_pricing_fragment(urlsplit(normalized).fragment):
        normalized = normalized.split("#", 1)[0]
    key = normalized.rstrip("/")
    if key in seen or not is_strict_pricing_url(normalized):
        return
    seen.add(key)
    urls.append(normalized)


def add_contact_sales_candidate(urls: list[str], seen: set[str], candidate: str, origin: str) -> None:
    try:
        normalized = normalize_pricing_url(candidate).split("#", 1)[0]
    except ValueError:
        return
    if urlsplit(normalized).netloc != urlsplit(origin).netloc:
        return
    key = normalized.rstrip("/")
    if key in seen or not is_contact_sales_url(normalized):
        return
    seen.add(key)
    urls.append(normalized)


def discover_pricing_urls(base_url: str, html_body: str, sitemap_body: str = "") -> list[str]:
    origin = pricing_url_origin(base_url)
    parser = parse_pricing_html(html_body)
    urls: list[str] = []
    seen: set[str] = set()
    for href in parser.links:
        add_pricing_candidate(urls, seen, urljoin(base_url, href), origin)
    if re.search(r"\bpricing|plans?\b", parser.text, re.I):
        add_pricing_candidate(urls, seen, urljoin(origin, "/#pricing"), origin)
    for loc in extract_sitemap_locs(sitemap_body):
        add_pricing_candidate(urls, seen, loc, origin)
    for path in COMMON_PRICING_PATHS:
        add_pricing_candidate(urls, seen, urljoin(origin, path), origin)
    urls.sort(key=pricing_url_score, reverse=True)
    return urls[:12]


def discover_contact_sales_urls(base_url: str, html_body: str, sitemap_body: str = "") -> list[str]:
    origin = pricing_url_origin(base_url)
    parser = parse_pricing_html(html_body)
    urls: list[str] = []
    seen: set[str] = set()
    for href in parser.links:
        add_contact_sales_candidate(urls, seen, urljoin(base_url, href), origin)
    for loc in extract_sitemap_locs(sitemap_body):
        add_contact_sales_candidate(urls, seen, loc, origin)
    for path in COMMON_CONTACT_SALES_PATHS:
        add_contact_sales_candidate(urls, seen, urljoin(origin, path), origin)
    urls.sort(key=contact_sales_url_score, reverse=True)
    return urls[:8]


def clip_pricing_text(text: str, max_chars: int = 6000) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    match = re.search(r"(?:\$\s?\d|pricing|plans?|free\s+(?:plan|tier|trial)|enterprise)", clean, re.I)
    if match and match.start() > 1200:
        start = max(0, match.start() - 600)
        head = clean[:600]
        tail = clean[start : start + (max_chars - 620)]
        return f"{head}\n...\n{tail}"
    return clean[:max_chars]


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    html: str
    text: str
    error: str = ""


@dataclass(frozen=True)
class LocatedPricingPage:
    url: str
    final_url: str
    status: str  # found | contact_sales | homepage_pricing | not_found
    method: str  # known_source | candidate_scored | homepage_section | contact_or_demo | none
    quality_score: int
    text: str
    error: str = ""


async def _fetch_static_page(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_PRICING_HTML_BYTES) -> FetchedPage:
    try:
        resp = await client.get(url, headers=browser_request_headers(url), follow_redirects=True)
    except Exception as exc:
        return FetchedPage(url=url, final_url=url, status_code=0, html="", text="", error=str(exc)[:200])

    content_type = (resp.headers.get("content-type") or "").lower()
    if resp.status_code != 200:
        return FetchedPage(url=url, final_url=str(resp.url), status_code=resp.status_code, html="", text="", error=f"http_{resp.status_code}")
    if content_type and not any(tok in content_type for tok in ("html", "xml", "text")):
        return FetchedPage(url=url, final_url=str(resp.url), status_code=resp.status_code, html="", text="", error=f"non_html:{content_type[:40]}")

    raw = resp.content[:max_bytes].decode(resp.encoding or "utf-8", errors="replace")
    parsed_text = parse_pricing_html(raw).text
    return FetchedPage(url=url, final_url=str(resp.url), status_code=resp.status_code, html=raw, text=parsed_text)


async def _fetch_sitemap(client: httpx.AsyncClient, base_url: str) -> str:
    try:
        origin = pricing_url_origin(base_url)
    except ValueError:
        return ""
    try:
        resp = await client.get(urljoin(origin, "/sitemap.xml"), headers=browser_request_headers(origin), timeout=5.0, follow_redirects=True)
        if resp.status_code == 200 and "<loc" in resp.text[:2000].lower():
            return resp.text[:MAX_SITEMAP_BYTES]
    except Exception:
        pass
    return ""


async def locate_pricing_page(
    official_url: str,
    *,
    known_pricing_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    max_candidates: int = 5,
) -> LocatedPricingPage:
    """Locate a credible pricing or contact-sales page using known URLs + multi-rule scoring."""
    async def _run(http_client: httpx.AsyncClient) -> LocatedPricingPage:
        # 1. Fast path: reuse stored URL from `pricing_sources`
        if known_pricing_url and known_pricing_url.strip().startswith(("http://", "https://")):
            known = await _fetch_static_page(http_client, known_pricing_url.strip())
            q_known = pricing_text_quality(known.text)
            if known.status_code == 200 and q_known >= 8 and len(known.text) >= 60:
                return LocatedPricingPage(
                    url=known.url,
                    final_url=known.final_url,
                    status="found",
                    method="known_source",
                    quality_score=q_known,
                    text=clip_pricing_text(known.text),
                )

        # 2. Fetch homepage + discover candidate pricing URLs
        try:
            home_url = normalize_pricing_url(official_url)
        except ValueError as exc:
            return LocatedPricingPage(url=official_url, final_url=official_url, status="not_found", method="none", quality_score=0, text="", error=str(exc))

        home = await _fetch_static_page(http_client, home_url)
        if home.status_code != 200 or not home.html:
            return LocatedPricingPage(
                url=home_url,
                final_url=home.final_url,
                status="not_found",
                method="none",
                quality_score=0,
                text="",
                error=home.error or "homepage_fetch_failed",
            )

        candidates = discover_pricing_urls(home.final_url or home_url, home.html, "")
        # Only fetch sitemap if homepage links didn't yield a direct /pricing or /plans candidate
        has_strong_nav_candidate = any(pricing_url_score(c) >= 120 for c in candidates)
        if not has_strong_nav_candidate:
            sitemap_body = await _fetch_sitemap(http_client, home.final_url or home_url)
            if sitemap_body:
                candidates = discover_pricing_urls(home.final_url or home_url, home.html, sitemap_body)

        best_page: FetchedPage | None = None
        best_score = -1000
        seen_urls = {home.final_url.rstrip("/")}

        for candidate in candidates[:max_candidates]:
            base_cand = candidate.split("#", 1)[0].rstrip("/")
            if base_cand in seen_urls and "#" not in candidate:
                continue
            if "#" in candidate and base_cand == home.final_url.rstrip("/"):
                page = home
            else:
                page = await _fetch_static_page(http_client, candidate)
            if page.status_code != 200 or not is_strict_pricing_url(page.final_url if "#" not in candidate else candidate):
                continue
            q = pricing_text_quality(page.text)
            total = pricing_url_score(candidate if "#" in candidate else page.final_url) + q
            if q > 0 and total > best_score and len(page.text) >= 60:
                best_page = page
                best_score = total

        if best_page is not None:
            return LocatedPricingPage(
                url=best_page.url,
                final_url=best_page.final_url,
                status="found",
                method="candidate_scored",
                quality_score=pricing_text_quality(best_page.text),
                text=clip_pricing_text(best_page.text),
            )

        # 3. Single-page SaaS tools often embed pricing directly on homepage without #pricing href
        home_quality = pricing_text_quality(home.text)
        if home_quality >= 14 and re.search(r"(?:\$\s?\d|€\s?\d|£\s?\d|\bfreemium\b|\bfree plan\b|\bper month\b|/mo\b)", home.text, re.I):
            return LocatedPricingPage(
                url=home.url,
                final_url=home.final_url,
                status="homepage_pricing",
                method="homepage_section",
                quality_score=home_quality,
                text=clip_pricing_text(home.text),
            )

        # 4. Contact-sales fallback
        contact_candidates = discover_contact_sales_urls(home.final_url or home_url, home.html, "")
        for candidate in contact_candidates[:3]:
            page = await _fetch_static_page(http_client, candidate)
            if page.status_code == 200 and len(page.text) >= 80:
                return LocatedPricingPage(
                    url=page.url,
                    final_url=page.final_url,
                    status="contact_sales",
                    method="contact_or_demo",
                    quality_score=pricing_text_quality(page.text),
                    text=clip_pricing_text(page.text),
                )

        return LocatedPricingPage(
            url=home_url,
            final_url=home.final_url,
            status="not_found",
            method="none",
            quality_score=home_quality,
            text="",
            error="no_credible_pricing_page",
        )

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=10.0) as owned_client:
        return await _run(owned_client)
