import argparse
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import random
import re
import string
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from dotenv import load_dotenv

try:
    from fake_useragent import UserAgent
except ImportError:
    UserAgent = None


SIMILARWEB_API_BASE = "https://data.similarweb.com/api/v1/data"
SIMILARWEB_EXTENSION_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
SIMILARWEB_EXTENSION_ID = "hoklmmgfnpapgjgcpechhaamimifchmp"
SIMILARWEB_EXTENSION_VERSION_FALLBACK = "6.12.21"
SIMILARWEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
TRAFFIC_SOURCE = "similarweb"
TRAFFIC_METRICS_SCHEMA_VERSION = 2
ASSET_SOURCE = "site_scraper"
ASSET_DB_STORAGE_BUCKET = "sitesimgs"
DEFAULT_R2_BUCKET = "sitesimgs"
ASSET_REQUIREMENT_ORDER = ("screenshot", "favicon", "description", "key_features", "category")
D1_API_BASE = "https://api.cloudflare.com/client/v4"
DOMAIN_STATE_SOURCE = "ahrefs"
AHREFS_DOMAIN_RATING_URL = "https://api.ahrefs.com/v3/public/domain-rating-free"
IANA_RDAP_DNS = "https://data.iana.org/rdap/dns.json"
RDAP_USER_AGENT = "traffic-runner-domain-whois/0.1"
PRICING_EXTRACTOR_VERSION = "python-rule-pricing-v1"
OPENAI_PRICING_EXTRACTOR_VERSION = "openai-structured-pricing-v1"
OPENAI_API_BASE = "https://api.openai.com/v1"
DEFAULT_OPENAI_PRICING_MODEL = "gpt-5.4-mini"
DEFAULT_OPENAI_PRICING_FALLBACK_MODEL = ""
OPENAI_PRICING_MIN_CONFIDENCE = 60
DEFAULT_OPENAI_PRICING_TEXT_CHARS = 24000
BROWSER_RENDERING_TEXT_SCORE_THRESHOLD = 8
PRICING_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_PRICING_UA_GENERATOR: Any | None = None
MAX_PRICING_HTML_BYTES = 1_200_000
MAX_PRICING_TEXT_CHARS = 180_000
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
PRICING_PLAN_NAMES = (
    "Free",
    "Basic",
    "Starter",
    "Lite",
    "Plus",
    "Pro",
    "Professional",
    "Premium",
    "Creator",
    "Team",
    "Business",
    "Growth",
    "Scale",
    "Enterprise",
)
PRICE_RE = re.compile(
    r"(?:(?P<currency1>US\$|\$|₹|USD|EUR|GBP|INR)\s*(?P<amount1>\d{1,7}(?:,\d{2,3})*(?:\.\d{1,4})?)|"
    r"(?P<amount2>\d{1,7}(?:,\d{2,3})*(?:\.\d{1,4})?)\s*(?P<currency2>USD|EUR|GBP|INR))",
    re.I,
)
COMMON_THREE_LABEL_SUFFIXES = {
    "co.uk",
    "com.au",
    "co.jp",
    "co.in",
    "co.nz",
    "co.kr",
    "com.br",
    "com.cn",
    "com.sg",
    "com.hk",
    "co.za",
    "com.mx",
}


@dataclass(frozen=True)
class Config:
    cloudflare_account_id: str
    cloudflare_d1_database_id: str
    cloudflare_api_token: str
    ahref_api_key: str
    brightdata_proxy_host: str
    brightdata_proxy_port: int
    brightdata_proxy_user: str
    brightdata_proxy_password: str
    limit: int
    concurrency: int
    max_retries: int
    poll_interval_seconds: int
    traffic_release_probe_domain: str
    traffic_release_probe_start_day: int
    traffic_release_probe_interval_seconds: int
    traffic_release_queue_limit: int
    asset_limit: int
    domain_state_limit: int
    domain_state_max_age_days: int
    pricing_limit: int
    pricing_timeout_seconds: int
    openai_api_key: str
    openai_pricing_model: str
    openai_pricing_fallback_model: str
    openai_pricing_timeout_seconds: int
    openai_pricing_text_chars: int
    browser_rendering_api_token: str
    browser_rendering_enabled: bool
    browser_rendering_timeout_seconds: int
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_public_base_url: str
    runner_instance_id: str
    runner_version: str


@dataclass(frozen=True)
class TrafficTask:
    normalized_domain: str
    traffic_month: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str


@dataclass(frozen=True)
class AssetTask:
    tool_id: int
    canonical_slug: str
    normalized_domain: str
    official_url: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str


@dataclass(frozen=True)
class DomainStateTask:
    normalized_domain: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str


@dataclass(frozen=True)
class PricingTask:
    task_id: int
    pricing_source_id: int
    tool_id: int
    canonical_slug: str
    source_url: str
    official_url: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str


@dataclass(frozen=True)
class PricingSourceCandidate:
    tool_id: int
    canonical_slug: str
    official_url: str


@dataclass(frozen=True)
class ReviewedPricingExtraction:
    extraction_id: int
    pricing_task_id: int
    pricing_source_id: int
    tool_id: int
    canonical_slug: str
    source_url: str
    final_url: str
    http_status: int
    content_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FetchResult:
    status: str
    monthly_rows: list[dict[str, Any]]
    error: str | None = None
    observed_latest_month: str | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class TrafficReleaseGateResult:
    available: bool
    status: str
    probe_attempted: bool
    observed_latest_month: str | None = None


@dataclass(frozen=True)
class DomainStateResult:
    status: str
    domain_rating: float | None
    domain_created_at: str | None
    error: str | None = None


@dataclass(frozen=True)
class AssetFetchResult:
    final_url: str
    screenshot: bytes = b""
    html: str = ""
    title: str = ""
    description: str = ""
    favicon_href: str = ""
    category_l1: str = ""
    category_l2: str = ""
    key_features: list[dict[str, str]] | None = None
    metadata_error: str = ""
    metadata_retryable: bool = True


class AssetPipelineError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class FaviconAsset:
    body: bytes
    key: str
    mime_type: str


@dataclass(frozen=True)
class PricingFetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    html: str
    error: str = ""
    page_status: str = "found"
    discovery_method: str = "source_url"


def read_int_env(name: str, fallback: int) -> int:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def read_bool_env(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return fallback
    return value.strip().lower() not in {"0", "false", "no", "off"}


def log_info(message: str, **fields: Any) -> None:
    print(json.dumps({"level": "info", "message": message, **fields}, ensure_ascii=False), flush=True)


def log_error(message: str, **fields: Any) -> None:
    print(json.dumps({"level": "error", "message": message, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def mask_value(value: str, prefix: int = 18, suffix: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix + 3:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def extract_brightdata_zone(username: str) -> str | None:
    match = re.search(r"(?:^|-)zone-([A-Za-z0-9_]+)", username or "")
    return match.group(1) if match else None


def response_header_summary(response: httpx.Response) -> dict[str, str]:
    keys = [
        "content-type",
        "content-length",
        "server",
        "cf-ray",
        "x-cache",
        "via",
        "x-brd-error",
        "x-brd-ip",
    ]
    return {key.replace("-", "_"): response.headers[key] for key in keys if key in response.headers}


def response_body_sample(response: httpx.Response, limit: int = 500) -> str:
    try:
        text = response.text
    except Exception as error:
        return f"<unable_to_read_response_text:{type(error).__name__}>"
    return re.sub(r"\s+", " ", text).strip()[:limit]



def load_config(require_brightdata: bool = True) -> Config:
    load_dotenv()
    return Config(
        cloudflare_account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        cloudflare_d1_database_id=os.environ["CLOUDFLARE_D1_DATABASE_ID"],
        cloudflare_api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        ahref_api_key=(os.getenv("AHREF_API_KEY") or os.getenv("AHREFS_API_KEY", "")).strip(),
        brightdata_proxy_host=os.getenv("BRIGHTDATA_PROXY_HOST", "brd.superproxy.io"),
        brightdata_proxy_port=read_int_env("BRIGHTDATA_PROXY_PORT", 33335),
        brightdata_proxy_user=os.environ["BRIGHTDATA_PROXY_USER"] if require_brightdata else os.getenv("BRIGHTDATA_PROXY_USER", ""),
        brightdata_proxy_password=os.environ["BRIGHTDATA_PROXY_PASSWORD"] if require_brightdata else os.getenv("BRIGHTDATA_PROXY_PASSWORD", ""),
        limit=read_int_env("RUNNER_LIMIT", 20),
        concurrency=read_int_env("RUNNER_CONCURRENCY", 5),
        max_retries=read_int_env("RUNNER_MAX_RETRIES", 2),
        poll_interval_seconds=read_int_env("RUNNER_POLL_INTERVAL_SECONDS", 300),
        traffic_release_probe_domain=normalize_domain(os.getenv("TRAFFIC_RELEASE_PROBE_DOMAIN", "chatgpt.com")) or "chatgpt.com",
        traffic_release_probe_start_day=min(max(read_int_env("TRAFFIC_RELEASE_PROBE_START_DAY", 7), 1), 28),
        traffic_release_probe_interval_seconds=max(900, read_int_env("TRAFFIC_RELEASE_PROBE_INTERVAL_SECONDS", 21600)),
        traffic_release_queue_limit=max(1, read_int_env("TRAFFIC_RELEASE_QUEUE_LIMIT", 5000)),
        asset_limit=read_int_env("RUNNER_ASSET_LIMIT", 5),
        domain_state_limit=read_int_env("RUNNER_DOMAIN_STATE_LIMIT", 50),
        domain_state_max_age_days=read_int_env("RUNNER_DOMAIN_STATE_MAX_AGE_DAYS", 15),
        pricing_limit=read_int_env("RUNNER_PRICING_LIMIT", 20),
        pricing_timeout_seconds=read_int_env("RUNNER_PRICING_TIMEOUT_SECONDS", 20),
        openai_api_key=os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API", ""),
        openai_pricing_model=os.getenv("OPENAI_PRICING_MODEL", DEFAULT_OPENAI_PRICING_MODEL),
        openai_pricing_fallback_model=os.getenv("OPENAI_PRICING_FALLBACK_MODEL", DEFAULT_OPENAI_PRICING_FALLBACK_MODEL),
        openai_pricing_timeout_seconds=read_int_env("OPENAI_PRICING_TIMEOUT_SECONDS", 45),
        openai_pricing_text_chars=read_int_env("OPENAI_PRICING_TEXT_CHARS", DEFAULT_OPENAI_PRICING_TEXT_CHARS),
        browser_rendering_api_token=os.getenv("CLOUDFLARE_BROWSER_RENDERING_API_TOKEN") or os.environ["CLOUDFLARE_API_TOKEN"],
        browser_rendering_enabled=read_bool_env("CLOUDFLARE_BROWSER_RENDERING_ENABLED", False),
        browser_rendering_timeout_seconds=read_int_env("CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS", 45),
        r2_access_key_id=os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", ""),
        r2_bucket=os.getenv("CLOUDFLARE_R2_BUCKET", DEFAULT_R2_BUCKET),
        r2_public_base_url=os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/"),
        runner_instance_id=os.getenv("RUNNER_INSTANCE_ID") or f"runner-{uuid.uuid4().hex[:16]}",
        runner_version=os.getenv("RUNNER_VERSION", "dev"),
    )


def normalize_domain(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.netloc or parsed.path).split("@")[-1].split(":")[0].strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", host):
        return ""
    return host


def get_registrable_domain(domain: str) -> str:
    labels = [part.strip() for part in (domain or "").split(".") if part.strip()]
    if len(labels) >= 3 and ".".join(labels[-2:]) in COMMON_THREE_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


def normalize_rdap_domain(value: str) -> str:
    domain = normalize_domain(value)
    if not domain:
        return ""
    return get_registrable_domain(domain)


def parse_iso_timestamp(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def generate_session_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_delta(**kwargs: Any) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PricingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.jsonld_scripts: list[str] = []
        self._ignore_depth = 0
        self._jsonld_depth = 0
        self._jsonld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {name.lower(): value or "" for name, value in attrs}
        if tag == "script":
            if attr.get("type", "").lower() == "application/ld+json":
                self._jsonld_depth += 1
                self._jsonld_parts = []
            else:
                self._ignore_depth += 1
            return
        if tag in {"style", "noscript", "svg"}:
            self._ignore_depth += 1
            return
        if tag == "a" and attr.get("href"):
            self.links.append(attr["href"])
        if tag in {"br", "p", "div", "li", "tr", "td", "th", "section", "article", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._jsonld_depth:
            self._jsonld_depth -= 1
            script = "".join(self._jsonld_parts).strip()
            if script:
                self.jsonld_scripts.append(script)
            self._jsonld_parts = []
            return
        if tag in {"script", "style", "noscript", "svg"} and self._ignore_depth:
            self._ignore_depth -= 1
            return
        if tag in {"p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._jsonld_depth:
            self._jsonld_parts.append(data)
        elif not self._ignore_depth:
            self.text_parts.append(data)

    @property
    def text(self) -> str:
        lines = []
        for line in html.unescape("".join(self.text_parts)).splitlines():
            cleaned = re.sub(r"\s+", " ", line).strip()
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)[:MAX_PRICING_TEXT_CHARS]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


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
            score += 90
        elif part == "demo":
            score += 70
        elif part in {"enterprise", "sales"}:
            score += 55
        elif part in {"contact", "contact-us"}:
            score += 35
    score -= max(0, len(parts) - 2) * 10
    return score


def source_context_parts(source_url: str) -> set[str]:
    generic = PRICING_PATH_PARTS | {"feature", "features", "product", "products", "en", "us", "www"}
    return {
        part
        for part in (segment.lower() for segment in urlsplit(source_url).path.split("/") if segment)
        if part not in generic and len(part) > 2
    }


def final_url_matches_source_context(source_url: str, final_url: str) -> bool:
    required_parts = source_context_parts(source_url)
    if not required_parts:
        return True
    final_parts = {part.lower() for part in urlsplit(final_url).path.split("/") if part}
    return required_parts.issubset(final_parts)


def random_pricing_user_agent() -> str:
    global _PRICING_UA_GENERATOR
    if UserAgent is not None:
        try:
            if _PRICING_UA_GENERATOR is None:
                _PRICING_UA_GENERATOR = UserAgent(
                    browsers=["Chrome", "Edge"],
                    platforms=["desktop"],
                    fallback=PRICING_USER_AGENT,
                )
            user_agent = _PRICING_UA_GENERATOR.random
            if user_agent:
                return str(user_agent)
        except Exception:
            pass
    return PRICING_USER_AGENT


def pricing_request_headers(url: str) -> dict[str, str]:
    origin = pricing_url_origin(url)
    return {
        "User-Agent": random_pricing_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{origin}/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }


def asset_page_url(task: AssetTask) -> str:
    for raw in (task.official_url, f"https://{task.normalized_domain}", f"http://{task.normalized_domain}"):
        if not raw:
            continue
        candidate = raw if "://" in raw else f"https://{raw}"
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate
    return f"https://{task.normalized_domain}"


def read_html_attribute(tag: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag, re.I)
    if not match or not match.group(1):
        return ""
    return match.group(1).strip().strip("\"'").strip()


def clean_asset_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return html.unescape(text)[:limit]


def clean_category_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug) and slug != "uncategorized" else ""


def clean_public_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:120].rstrip("-")


def public_tool_slug_base(task: AssetTask, title: str = "") -> str:
    canonical_base = re.sub(r"-[0-9a-f]{8}$", "", task.canonical_slug.strip().lower())
    registrable_domain = get_registrable_domain(task.normalized_domain)
    domain_label = registrable_domain.split(".", 1)[0]
    for value in (canonical_base, domain_label, title, task.canonical_slug):
        slug = clean_public_slug(value)
        if slug:
            return slug
    return f"tool-{task.tool_id}"


def numbered_public_slug(base: str, number: int) -> str:
    suffix = "" if number <= 1 else f"-{number}"
    trimmed_base = base[: 120 - len(suffix)].rstrip("-") or "tool"
    return f"{trimmed_base}{suffix}"


def clean_key_features(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    features: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = clean_asset_text(raw.get("name") or raw.get("feature_name"), 120)
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        features.append({
            "name": name,
            "description": clean_asset_text(raw.get("description") or raw.get("feature_description"), 240),
        })
        if len(features) >= 6:
            break
    return features


def read_html_title(html_body: str) -> str:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_body or "", re.I)
    return clean_asset_text(match.group(1), 120) if match else ""


def read_html_meta(html_body: str, names: set[str]) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", html_body or "", re.I):
        key = (read_html_attribute(tag, "property") or read_html_attribute(tag, "name")).lower()
        content = read_html_attribute(tag, "content")
        if key in names and content:
            return clean_asset_text(content, 500)
    return ""


def extract_favicon_href(html_body: str, page_url: str) -> str | None:
    links = re.findall(r"<link\b[^>]*>", html_body or "", re.I)
    icon_links = []
    for tag in links:
        rel = read_html_attribute(tag, "rel").lower()
        href = read_html_attribute(tag, "href")
        if href and ("icon" in rel or "apple-touch-icon" in rel):
            icon_links.append((rel, href))
    preferred = next((href for rel, href in icon_links if "apple-touch-icon" in rel), None)
    preferred = preferred or next((href for _rel, href in icon_links), None)
    return urljoin(page_url, preferred) if preferred else None


def find_favicon_href(html_body: str, page_url: str) -> str:
    return extract_favicon_href(html_body, page_url) or urljoin(page_url, "/favicon.ico")


def asset_extension(asset_url: str, content_type: str) -> str:
    normalized = (content_type or "").lower()
    if "image/png" in normalized:
        return ".png"
    if "image/svg" in normalized:
        return ".svg"
    if "image/webp" in normalized:
        return ".webp"
    if "image/jpeg" in normalized or "image/jpg" in normalized:
        return ".jpg"
    if "image/x-icon" in normalized or "image/vnd.microsoft.icon" in normalized:
        return ".ico"
    try:
        match = re.search(r"\.(ico|png|svg|jpg|jpeg|webp)$", urlsplit(asset_url).path, re.I)
    except ValueError:
        match = None
    return f".{match.group(1).lower().replace('jpeg', 'jpg')}" if match else ".ico"


def asset_mime_type(asset_url: str, content_type: str) -> str:
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized.startswith("image/"):
        return normalized
    extension = asset_extension(asset_url, "")
    if extension == ".png":
        return "image/png"
    if extension == ".svg":
        return "image/svg+xml"
    if extension == ".webp":
        return "image/webp"
    if extension == ".jpg":
        return "image/jpeg"
    return "image/x-icon"


def asset_public_url(base_url: str, object_key: str) -> str | None:
    if not base_url:
        return None
    normalized_base = base_url.rstrip("/")
    if not normalized_base.startswith(("http://", "https://")):
        normalized_base = f"https://{normalized_base}"
    encoded_path = "/".join(quote(part, safe="") for part in object_key.split("/"))
    return f"{normalized_base}/{encoded_path}"


def parse_pricing_html(value: str) -> PricingHtmlParser:
    parser = PricingHtmlParser()
    parser.feed(value or "")
    return parser


def pricing_text_quality(text: str) -> int:
    lower = text.lower()
    score = 0
    score += len(re.findall(r"\$\s?\d|usd\s?\d", lower)) * 3
    score += len(re.findall(r"\bpricing|plans?|monthly|yearly|per month|per user|contact sales|enterprise\b", lower))
    score -= len(re.findall(r"\bblog|privacy|terms|careers|cookie|shopping|purchase|cart\b", lower)) * 2
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
    if key in seen:
        return
    if not is_strict_pricing_url(normalized):
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


def read_decimal(value: Any) -> str | None:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return format(amount.normalize(), "f")


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def normalize_currency(value: str | None) -> str:
    raw = (value or "$").upper().replace("US$", "USD").replace("$", "USD").replace("\u20b9", "INR")
    if raw in {"USD", "EUR", "GBP", "INR"}:
        return raw
    return "USD"


def clean_snippet(value: str, limit: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return cleaned[:limit].strip()


def infer_interval(context: str) -> str | None:
    lower = context.lower()
    if re.search(r"/\s*yr\b|per year|yearly|annually|annual|/year|\byr\b", lower):
        return "yearly"
    if re.search(r"/\s*mo\b|per month|monthly|/month|\bmo\b", lower):
        return "monthly"
    return None


def infer_unit(context: str) -> str | None:
    lower = context.lower()
    if "per user" in lower or "/user" in lower:
        return "user"
    if "per seat" in lower or "/seat" in lower:
        return "seat"
    return None


def is_polluted_context(context: str) -> bool:
    lower = context.lower()
    return bool(
        re.search(
            r"\b(under|shopping|purchase|cart|invoice|discount|save|coupon|refund|tax|blog|privacy|terms|"
            r"per image|token|credit|api call|api pricing|model price)\b",
            lower,
        )
    )


def choose_plan_name(context_before_price: str, fallback_index: int) -> str:
    before = clean_snippet(context_before_price, 220)
    lower = before.lower()
    last_name = ""
    last_pos = -1
    for name in PRICING_PLAN_NAMES:
        pos = lower.rfind(name.lower())
        if pos > last_pos:
            last_name = name
            last_pos = pos
    if last_name:
        return last_name

    lines = [clean_snippet(line, 80) for line in before.split("\n") if clean_snippet(line, 80)]
    for line in reversed(lines[-4:]):
        words = line.split()
        if 1 <= len(words) <= 4 and not is_polluted_context(line):
            return line
    return f"Plan {fallback_index}"


def price_sort_key(plan: dict[str, Any]) -> tuple[int, Decimal]:
    price = (plan.get("prices") or [{}])[0]
    amount = decimal_value(price.get("amount"))
    if amount == 0:
        return (0, amount)
    if price.get("billing_interval") == "monthly":
        return (1, amount)
    if price.get("billing_interval") == "yearly":
        return (2, amount)
    return (3, amount)


def display_text_has_explicit_price(value: str) -> bool:
    return bool(PRICE_RE.search(value or ""))


def validate_plan_price_integrity(plans: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for plan in plans:
        name = clean_snippet(str(plan.get("name") or plan.get("source_plan_key") or "Unknown"), 80)
        prices = list(plan.get("prices") or [])
        if not prices:
            errors.append(f"Plan has no price row: {name}")
            continue
        for price in prices:
            display_text = str(price.get("display_text") or "")
            has_display_price = display_text_has_explicit_price(display_text)
            amount = price.get("amount")
            currency = price.get("currency")
            is_custom_quote = bool(price.get("custom_quote")) or price.get("kind") == "custom_quote"
            if has_display_price and (amount in (None, "") or not currency):
                errors.append(f"Explicit price text missing structured amount/currency: {name}")
            if has_display_price and is_custom_quote:
                errors.append(f"Explicit price text marked as custom quote: {name}")
            if not is_custom_quote and amount not in (None, "") and not currency:
                errors.append(f"Structured price missing currency: {name}")
    return sorted(set(errors))


def comparable_plan_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    name = re.sub(r"\bplan\b", "", name).strip()
    return re.sub(r"\s+", " ", name)


def public_price_map(plans: list[dict[str, Any]]) -> dict[str, tuple[str, str, str]]:
    prices: dict[str, tuple[str, str, str]] = {}
    for plan in plans:
        name = comparable_plan_name(str(plan.get("name") or plan.get("source_plan_key") or ""))
        if not name:
            continue
        for price in list(plan.get("prices") or [])[:1]:
            if price.get("custom_quote"):
                continue
            amount = read_decimal(price.get("amount"))
            currency = str(price.get("currency") or "").upper()
            if amount is not None and currency:
                prices[name] = (amount, currency, str(plan.get("name") or name))
    return prices


def validate_jsonld_visible_price_conflicts(
    jsonld_plans: list[dict[str, Any]],
    visible_plans: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    jsonld_prices = public_price_map(jsonld_plans)
    visible_prices = public_price_map(visible_plans)
    for key, (jsonld_amount, jsonld_currency, display_name) in jsonld_prices.items():
        visible = visible_prices.get(key)
        if not visible:
            continue
        visible_amount, visible_currency, _ = visible
        if (jsonld_amount, jsonld_currency) != (visible_amount, visible_currency):
            errors.append(
                f"JSON-LD price conflicts with visible text for {display_name}: "
                f"{jsonld_amount} {jsonld_currency} vs {visible_amount} {visible_currency}"
            )
    return errors


def should_verify_rule_pricing_with_openai(
    payload: dict[str, Any],
    text_score: int,
    page_status: str,
) -> tuple[bool, list[str]]:
    if page_status != "found":
        return False, []
    plans = list(payload.get("plans") or [])
    reasons: list[str] = []
    if not plans:
        return True, ["rules_found_no_plans"]
    names = [str(plan.get("name") or "") for plan in plans]
    name_counts: dict[str, int] = {}
    for name in names:
        key = name.lower().strip()
        name_counts[key] = name_counts.get(key, 0) + 1
        if re.fullmatch(r"plan\s+\d+", key):
            reasons.append("generic_plan_name")
    if any(count > 1 and name not in {"free", "enterprise"} for name, count in name_counts.items()):
        reasons.append("duplicate_plan_names")
    if text_score < 18:
        reasons.append("low_text_quality")

    currencies = set()
    for plan in plans:
        for price in plan.get("prices", []):
            if price.get("currency"):
                currencies.add(str(price.get("currency")))
            display_text = str(price.get("display_text") or "")
            lower = display_text.lower()
            if len(display_text) > 140:
                reasons.append("long_price_context")
            if re.search(r"\b(raise[sd]?|funding|students?|graduates?|academy|this month only|additional cost|traditional)\b", lower):
                reasons.append("polluted_price_context")
            if "\u20b9" in display_text and price.get("currency") != "INR":
                reasons.append("currency_mismatch")
    if len(currencies) > 1:
        reasons.append("mixed_currencies")
    return bool(reasons), sorted(set(reasons))


def validate_extracted_plan_consistency(plans: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen_names: dict[str, int] = {}
    currencies = set()
    for plan in plans:
        name = str(plan.get("name") or "").strip().lower()
        if name:
            seen_names[name] = seen_names.get(name, 0) + 1
        for price in plan.get("prices", []):
            if price.get("currency"):
                currencies.add(str(price.get("currency")))
    duplicated_names = {name for name, count in seen_names.items() if count > 1 and name not in {"free", "enterprise", "custom"}}
    if duplicated_names:
        errors.append("Duplicate plan names in extracted pricing")
    if len(currencies) > 1:
        errors.append("Multiple currencies in extracted pricing")
    return errors


def normalize_pricing_plan(
    name: str,
    amount: str | None,
    currency: str = "USD",
    context: str = "",
    index: int = 0,
) -> dict[str, Any]:
    kind = "one_time" if re.search(r"one[- ]?time|lifetime", context, re.I) else "recurring"
    if amount is None:
        kind = "custom_quote"
    price = {
        "kind": kind,
        "amount": amount,
        "currency": currency if amount is not None else None,
        "billing_interval": infer_interval(context) if kind == "recurring" else None,
        "commitment_interval": None,
        "unit": infer_unit(context),
        "custom_quote": amount is None,
        "starting_at": bool(re.search(r"from|starting", context, re.I)),
        "display_text": clean_snippet(context, 180),
    }
    clean_name = clean_snippet(name, 80) or ("Enterprise" if amount is None else f"Plan {index}")
    return {
        "source_plan_key": re.sub(r"[^a-z0-9]+", "_", clean_name.lower()).strip("_")[:80],
        "name": clean_name,
        "audience": None,
        "description": None,
        "is_enterprise": 1 if re.search(r"enterprise|contact", clean_name, re.I) else 0,
        "prices": [price],
        "features": [],
        "display_order": index,
    }


def collect_jsonld_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            nodes.extend(collect_jsonld_nodes(item))
    elif isinstance(value, dict):
        nodes.append(value)
        if "@graph" in value:
            nodes.extend(collect_jsonld_nodes(value["@graph"]))
        if "offers" in value:
            nodes.extend(collect_jsonld_nodes(value["offers"]))
    return nodes


def extract_jsonld_plans(scripts: list[str]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for script in scripts:
        try:
            data = json.loads(html.unescape(script))
        except json.JSONDecodeError:
            continue
        for node in collect_jsonld_nodes(data):
            raw_price = node.get("price") or node.get("lowPrice")
            if raw_price is None and isinstance(node.get("priceSpecification"), dict):
                raw_price = node["priceSpecification"].get("price")
            amount = read_decimal(raw_price)
            if amount is None:
                continue
            name = clean_snippet(str(node.get("name") or node.get("description") or ""), 80) or "Listed plan"
            currency = normalize_currency(str(node.get("priceCurrency") or "USD"))
            plans.append(normalize_pricing_plan(name, amount, currency, json.dumps(node, ensure_ascii=False), len(plans)))
            if len(plans) >= 6:
                return plans
    return plans


def has_free_plan_signal(text: str) -> bool:
    lower = re.sub(r"\s+", " ", (text or "").lower())
    if re.search(r"\bfree\s+(trial|demo|consultation|call|account|signup|sign up|start|download)\b", lower):
        return False
    return bool(
        re.search(r"\bfree\s+(plan|tier|forever)\b|\b(plan|tier)\s+free\b", lower)
        or re.search(r"\bfree\b.{0,80}\$(?:\s*)0(?:\b|/)", lower)
        or re.search(r"\$(?:\s*)0(?:\b|/).{0,80}\bfree\b", lower)
    )


def extract_text_plans(text: str) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for match in PRICE_RE.finditer(text):
        amount = read_decimal(match.group("amount1") or match.group("amount2"))
        if amount is None:
            continue
        start = max(0, match.start() - 260)
        end = min(len(text), match.end() + 220)
        context = text[start:end]
        if is_polluted_context(context):
            continue
        currency = normalize_currency(match.group("currency1") or match.group("currency2"))
        name = choose_plan_name(text[start:match.start()], len(plans) + 1)
        plan = normalize_pricing_plan(name, amount, currency, context, len(plans))
        price = plan["prices"][0]
        key = (plan["name"].lower(), price["amount"], price["billing_interval"])
        if key in seen:
            continue
        seen.add(key)
        plans.append(plan)
        if len(plans) >= 6:
            break

    lower = text.lower()
    if has_free_plan_signal(text) and not any(plan["name"].lower() == "free" for plan in plans):
        plans.insert(0, normalize_pricing_plan("Free", "0", "USD", "Free", 0))
    if re.search(r"contact sales|custom pricing|talk to sales", lower) and not any(plan["prices"][0]["custom_quote"] for plan in plans):
        custom_plan = normalize_pricing_plan("Custom", None, "USD", "Contact sales", len(plans))
        custom_plan["is_enterprise"] = 1
        custom_plan["description"] = "No public prices; contact sales or book a demo."
        custom_plan["prices"][0]["billing_interval"] = "custom"
        plans.append(custom_plan)

    normalized = sorted(plans[:6], key=price_sort_key)
    for index, plan in enumerate(normalized):
        plan["display_order"] = index
    return normalized


def extract_pricing_payload(
    html_body: str,
    source_url: str,
    final_url: str,
    http_status: int,
    error: str,
    page_status: str = "found",
    discovery_method: str = "source_url",
) -> tuple[dict[str, Any], str, int, list[str]]:
    if page_status == "contact_sales":
        plan = normalize_pricing_plan("Custom", None, "USD", "Book a demo / contact sales", 0)
        plan["is_enterprise"] = 1
        plan["description"] = "No public prices; contact sales or book a demo."
        plan["prices"][0]["billing_interval"] = "custom"
        plan["prices"][0]["display_text"] = "Book a demo / contact sales"
        payload = {
            "plans": [plan],
            "plan_count": 1,
            "quality": {
                "ok": True,
                "reason": None,
                "text_score": pricing_text_quality(parse_pricing_html(html_body).text if html_body else ""),
                "final_url": final_url,
                "page_status": page_status,
                "discovery_method": discovery_method,
            },
            "extraction_method": "python_rule",
        }
        return payload, "approved", 78, []

    if page_status == "not_found":
        payload = {
            "plans": [],
            "plan_count": 0,
            "quality": {
                "ok": False,
                "reason": error or "no credible pricing page found",
                "text_score": 0,
                "final_url": final_url,
                "page_status": page_status,
                "discovery_method": discovery_method,
            },
            "extraction_method": "python_rule",
        }
        return payload, "manual_review", 10, [error or "No credible pricing page found"]

    parser = parse_pricing_html(html_body)
    text = parser.text
    jsonld_plans = extract_jsonld_plans(parser.jsonld_scripts)
    text_plans = extract_text_plans(text)
    plans = jsonld_plans or text_plans
    validation_errors: list[str] = []
    if http_status < 200 or http_status >= 400:
        validation_errors.append(error or f"HTTP {http_status}")
    if http_status == 200 and not is_strict_pricing_url(final_url):
        validation_errors.append(f"Final URL is not a strict pricing page: {final_url}")
    if http_status == 200 and not final_url_matches_source_context(source_url, final_url):
        validation_errors.append(f"Final URL lost source context: {final_url}")
    if not plans:
        validation_errors.append("No public pricing plans found")
    validation_errors.extend(validate_plan_price_integrity(plans))
    if jsonld_plans and text_plans:
        validation_errors.extend(validate_jsonld_visible_price_conflicts(jsonld_plans, text_plans))

    has_paid_or_quote = any(
        price.get("custom_quote") or decimal_value(price.get("amount")) > 0
        for plan in plans
        for price in plan.get("prices", [])
    )
    if plans and not has_paid_or_quote and not any(plan["name"].lower() == "free" for plan in plans):
        validation_errors.append("No paid, free, or custom-quote plan found")

    approved = not validation_errors and bool(plans)
    confidence = 82 if approved else 45 if plans else 25
    payload = {
        "plans": plans,
        "plan_count": len(plans),
        "quality": {
            "ok": approved,
            "reason": validation_errors[0] if validation_errors else None,
            "text_score": pricing_text_quality(text),
            "final_url": final_url,
            "page_status": page_status,
            "discovery_method": discovery_method,
        },
        "extraction_method": "python_rule",
    }
    return payload, "approved" if approved else "manual_review", confidence, validation_errors


def derive_final_pipeline_stage(
    payload: dict[str, Any],
    review_status: str,
    extractor_version: str,
    model_name: str | None,
    discovery_method: str,
) -> str:
    used_browser = "browser_run" in (discovery_method or "")
    if review_status != "approved":
        return "browser_run_manual_review" if used_browser else "manual_review"
    if model_name or extractor_version == OPENAI_PRICING_EXTRACTOR_VERSION or payload.get("extraction_method") == "openai_structured":
        return "browser_run_openai" if used_browser else "openai"
    page_status = ((payload.get("quality") or {}).get("page_status") or "").strip()
    if page_status == "contact_sales":
        return "contact_sales"
    return "browser_run_rule" if used_browser else "rule"


def openai_pricing_schema() -> dict[str, Any]:
    price_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["recurring", "one_time", "usage", "custom_quote"]},
            "amount": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"]},
            "billing_interval": {"type": ["string", "null"], "enum": ["monthly", "yearly", "one_time", "usage", "custom", None]},
            "commitment_interval": {"type": ["string", "null"], "enum": ["monthly", "yearly", "none", None]},
            "unit": {"type": ["string", "null"]},
            "custom_quote": {"type": "boolean"},
            "starting_at": {"type": "boolean"},
            "display_text": {"type": "string"},
        },
        "required": [
            "kind",
            "amount",
            "currency",
            "billing_interval",
            "commitment_interval",
            "unit",
            "custom_quote",
            "starting_at",
            "display_text",
        ],
    }
    plan_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_plan_key": {"type": "string"},
            "name": {"type": "string"},
            "audience": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "is_enterprise": {"type": "boolean"},
            "display_order": {"type": "integer"},
            "prices": {"type": "array", "items": price_schema},
            "features": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "source_plan_key",
            "name",
            "audience",
            "description",
            "is_enterprise",
            "display_order",
            "prices",
            "features",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "plans": {"type": "array", "items": plan_schema},
            "confidence": {"type": "integer"},
            "notes": {"type": "string"},
        },
        "required": ["plans", "confidence", "notes"],
    }


def normalize_openai_plan(plan: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    raw_prices = plan.get("prices") if isinstance(plan.get("prices"), list) else []
    raw_price = raw_prices[0] if raw_prices and isinstance(raw_prices[0], dict) else {}
    raw_amount = raw_price.get("amount")
    amount = read_decimal(raw_amount) if raw_amount not in (None, "") else None
    custom_quote = bool(raw_price.get("custom_quote")) or amount is None or raw_price.get("kind") == "custom_quote"
    kind = str(raw_price.get("kind") or ("custom_quote" if custom_quote else "recurring"))
    if kind not in {"recurring", "one_time", "usage", "custom_quote"}:
        kind = "custom_quote" if custom_quote else "recurring"

    billing_interval = raw_price.get("billing_interval")
    if billing_interval not in {"monthly", "yearly", "one_time", "usage", "custom", None}:
        billing_interval = None
    commitment_interval = raw_price.get("commitment_interval")
    if commitment_interval not in {"monthly", "yearly", "none", None}:
        commitment_interval = None

    name = clean_snippet(str(plan.get("name") or ""), 80)
    if not name:
        name = "Enterprise" if custom_quote else f"Plan {index + 1}"
    price = {
        "kind": kind,
        "amount": None if custom_quote else amount,
        "currency": normalize_currency(str(raw_price.get("currency") or "USD")) if not custom_quote else None,
        "billing_interval": billing_interval,
        "commitment_interval": None if commitment_interval == "none" else commitment_interval,
        "unit": clean_snippet(str(raw_price.get("unit") or ""), 40) or None,
        "custom_quote": custom_quote,
        "starting_at": bool(raw_price.get("starting_at")),
        "display_text": clean_snippet(str(raw_price.get("display_text") or ""), 180),
    }
    features = [
        clean_snippet(str(feature), 120)
        for feature in (plan.get("features") if isinstance(plan.get("features"), list) else [])
        if clean_snippet(str(feature), 120)
    ][:12]
    source_key = clean_snippet(str(plan.get("source_plan_key") or ""), 80)
    if not source_key:
        source_key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:80]
    return {
        "source_plan_key": source_key,
        "name": name,
        "audience": clean_snippet(str(plan.get("audience") or ""), 80) or None,
        "description": clean_snippet(str(plan.get("description") or ""), 220) or None,
        "is_enterprise": 1 if bool(plan.get("is_enterprise")) or re.search(r"enterprise|contact", name, re.I) else 0,
        "prices": [price],
        "features": features,
        "display_order": index,
    }


def validate_pricing_plans(
    plans: list[dict[str, Any]],
    source_url: str,
    final_url: str,
    http_status: int,
    error: str,
) -> list[str]:
    validation_errors: list[str] = []
    if http_status < 200 or http_status >= 400:
        validation_errors.append(error or f"HTTP {http_status}")
    if http_status == 200 and not is_strict_pricing_url(final_url):
        validation_errors.append(f"Final URL is not a strict pricing page: {final_url}")
    if http_status == 200 and not final_url_matches_source_context(source_url, final_url):
        validation_errors.append(f"Final URL lost source context: {final_url}")
    if not plans:
        validation_errors.append("No public pricing plans found")
    validation_errors.extend(validate_plan_price_integrity(plans))

    has_paid_or_quote = any(
        price.get("custom_quote") or decimal_value(price.get("amount")) > 0
        for plan in plans
        for price in plan.get("prices", [])
    )
    if plans and not has_paid_or_quote and not any(plan["name"].lower() == "free" for plan in plans):
        validation_errors.append("No paid, free, or custom-quote plan found")
    return validation_errors


class OpenAIPricingExtractor:
    def __init__(self, api_key: str, model: str, timeout_seconds: int, text_chars: int):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.text_chars = text_chars

    async def extract(
        self,
        html_body: str,
        source_url: str,
        final_url: str,
        http_status: int,
        error: str,
    ) -> tuple[dict[str, Any], str, int, list[str]] | None:
        if not self.api_key or http_status != 200 or not html_body:
            return None
        text = parse_pricing_html(html_body).text[: self.text_chars]
        if not text.strip():
            return None

        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract public SaaS pricing plans from the provided pricing page text. "
                        "Return only primary public package prices. Ignore discounts, trials, FAQ examples, add-ons, "
                        "API credit tables, and unrelated comparison text unless they are the main package price. "
                        "Use at most six plans. Each plan must keep at most one primary price."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_url": source_url,
                            "final_url": final_url,
                            "pricing_text": text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "pricing_extraction",
                    "strict": True,
                    "schema": openai_pricing_schema(),
                },
            },
            "max_completion_tokens": 3000,
        }

        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
                response = await client.post(
                    f"{OPENAI_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
            response.raise_for_status()
            data = response.json()
            message = ((data.get("choices") or [{}])[0].get("message") or {})
            content = message.get("content")
            if not content:
                log_info("pricing.openai.empty_response", model=self.model, final_url=final_url)
                return None
            parsed = json.loads(content)
        except Exception as error_value:
            log_info("pricing.openai.failed", model=self.model, final_url=final_url, error=str(error_value)[:300])
            return None

        raw_plans = parsed.get("plans") if isinstance(parsed, dict) else []
        plans = [
            normalized
            for index, raw_plan in enumerate(raw_plans if isinstance(raw_plans, list) else [])
            if (normalized := normalize_openai_plan(raw_plan, index)) is not None
        ][:6]
        validation_errors = validate_pricing_plans(plans, source_url, final_url, http_status, error)
        validation_errors.extend(validate_extracted_plan_consistency(plans))
        try:
            model_confidence = int(parsed.get("confidence") or 70) if isinstance(parsed, dict) else 70
        except (TypeError, ValueError):
            model_confidence = 70
        if model_confidence < OPENAI_PRICING_MIN_CONFIDENCE:
            validation_errors.append(f"OpenAI confidence below {OPENAI_PRICING_MIN_CONFIDENCE}: {model_confidence}")
        approved = not validation_errors and bool(plans)
        confidence = min(90, max(0, model_confidence)) if approved else min(65, max(20, model_confidence))
        payload = {
            "plans": plans,
            "plan_count": len(plans),
            "quality": {
                "ok": approved,
                "reason": validation_errors[0] if validation_errors else None,
                "text_score": pricing_text_quality(text),
                "final_url": final_url,
                "notes": clean_snippet(str(parsed.get("notes") or ""), 300) if isinstance(parsed, dict) else "",
            },
            "extraction_method": "openai_structured",
        }
        return payload, "approved" if approved else "manual_review", confidence, validation_errors


class CloudflareBrowserRunRenderer:
    def __init__(self, config: Config):
        self.endpoint = (
            f"{D1_API_BASE}/accounts/{config.cloudflare_account_id}"
            "/browser-rendering/content"
        )
        self.headers = {
            "Authorization": f"Bearer {config.browser_rendering_api_token}",
            "Content-Type": "application/json",
        }
        self.timeout_seconds = config.browser_rendering_timeout_seconds

    async def render(self, result: PricingFetchResult) -> PricingFetchResult | None:
        target_url = result.final_url or result.url
        request_payload = {
            "url": target_url,
            "userAgent": random_pricing_user_agent(),
            "setExtraHTTPHeaders": {
                "Accept-Language": "en-US,en;q=0.9",
            },
            "rejectResourceTypes": ["image", "media", "font"],
            "gotoOptions": {
                "waitUntil": "networkidle0",
            },
        }
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
                response = await client.post(self.endpoint, headers=self.headers, json=request_payload)
        except Exception as error:
            log_info("pricing.browser_render.failed", url=target_url, error=str(error)[:300])
            return None

        if response.status_code < 200 or response.status_code >= 300:
            log_info(
                "pricing.browser_render.http_error",
                url=target_url,
                status=response.status_code,
                body=response_body_sample(response, 300),
            )
            return None

        rendered_html = ""
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            if data.get("success") is False:
                log_info("pricing.browser_render.api_error", url=target_url, response=str(data)[:300])
                return None
            rendered = data.get("result")
            if isinstance(rendered, dict):
                rendered = rendered.get("content") or rendered.get("html")
            if isinstance(rendered, str):
                rendered_html = rendered
        if not rendered_html and "html" in response.headers.get("content-type", "").lower():
            rendered_html = response.text
        if not rendered_html.strip():
            log_info("pricing.browser_render.empty", url=target_url)
            return None

        log_info(
            "pricing.browser_render.done",
            url=target_url,
            text_score=pricing_text_quality(parse_pricing_html(rendered_html).text),
        )
        return PricingFetchResult(
            url=result.url,
            final_url=target_url,
            status=200,
            content_type="text/html; rendered=cloudflare-browser-run",
            html=rendered_html,
            error="",
            page_status="found",
            discovery_method=f"{result.discovery_method}+browser_run",
        )


class CloudflareBrowserRunAssetClient:
    def __init__(self, config: Config):
        self.endpoint_base = f"{D1_API_BASE}/accounts/{config.cloudflare_account_id}/browser-rendering"
        self.headers = {
            "Authorization": f"Bearer {config.browser_rendering_api_token}",
            "Content-Type": "application/json",
        }
        self.timeout_seconds = config.browser_rendering_timeout_seconds

    async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(f"{self.endpoint_base}/{endpoint}", headers=self.headers, json=body)
        text = response.text
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        if response.status_code < 200 or response.status_code >= 300 or (isinstance(parsed, dict) and parsed.get("success") is False):
            messages: list[str] = []
            if isinstance(parsed, dict):
                errors = parsed.get("errors")
                if isinstance(errors, list):
                    for error in errors:
                        if not isinstance(error, dict):
                            continue
                        code = error.get("code")
                        message = str(error.get("message") or "").strip()
                        if message and code is not None:
                            messages.append(f"{code}: {message}")
                        elif message:
                            messages.append(message)
                        elif code is not None:
                            messages.append(f"code={code}")
            detail = "; ".join(messages)
            if not detail and parsed is not None:
                detail = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))[:300]
            if not detail:
                detail = text[:300] or f"HTTP {response.status_code}"
            retryable = (
                (endpoint == "json" and response.status_code in (400, 409, 422))
                or response.status_code in (408, 425, 429)
                or response.status_code >= 500
                or (200 <= response.status_code < 300 and isinstance(parsed, dict) and parsed.get("success") is False)
            )
            raise AssetPipelineError(
                f"browser_run_{endpoint}_api_error: {detail}",
                retryable=retryable,
            )
        return parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else parsed

    def asset_candidate_urls(self, task: AssetTask) -> list[str]:
        primary_url = asset_page_url(task)
        parsed = urlsplit(primary_url)
        candidates = [primary_url]
        if parsed.scheme == "https":
            candidates.append(f"http://{parsed.netloc}{parsed.path or '/'}")
        return candidates

    def browser_payload(self, target_url: str, *, reject_heavy_resources: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": target_url,
            "userAgent": random_pricing_user_agent(),
            "setExtraHTTPHeaders": {
                "Accept-Language": "en-US,en;q=0.9",
            },
            "gotoOptions": {
                "waitUntil": "domcontentloaded",
                "timeout": self.timeout_seconds * 1000,
            },
        }
        if reject_heavy_resources:
            payload["rejectResourceTypes"] = ["image", "media", "font"]
        return payload

    async def fetch_structured_asset_data(
        self,
        task: AssetTask,
        *,
        prompt: str,
        json_schema: dict[str, Any],
        stage: str,
    ) -> tuple[str, dict[str, Any]]:
        errors: list[str] = []
        retryable_errors: list[bool] = []
        for target_url in self.asset_candidate_urls(task):
            try:
                extracted = await self.call_quick_action(
                    "json",
                    {
                        **self.browser_payload(target_url),
                        "prompt": prompt,
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": json_schema,
                        },
                    },
                )
                return target_url, extracted if isinstance(extracted, dict) else {}
            except Exception as error:
                errors.append(f"{target_url}: {str(error)[:220]}")
                retryable_errors.append(bool(getattr(error, "retryable", True)))

        raise AssetPipelineError(
            f"Browser Run {stage} extraction failed. " + " | ".join(errors),
            retryable=any(retryable_errors),
        )

    async def fetch_homepage_content(self, task: AssetTask) -> tuple[str, str]:
        errors: list[str] = []
        retryable_errors: list[bool] = []
        for target_url in self.asset_candidate_urls(task):
            try:
                content = await self.call_quick_action(
                    "content",
                    self.browser_payload(target_url, reject_heavy_resources=True),
                )
                if isinstance(content, dict):
                    html_body = str(content.get("content") or content.get("html") or "")
                else:
                    html_body = str(content or "")
                if html_body.strip():
                    return target_url, html_body
                raise RuntimeError("content returned no HTML")
            except Exception as error:
                errors.append(f"{target_url}: {str(error)[:220]}")
                retryable_errors.append(bool(getattr(error, "retryable", True)))

        raise AssetPipelineError(
            "Browser Run content extraction failed. " + " | ".join(errors),
            retryable=any(retryable_errors),
        )

    async def fetch_homepage_core_metadata(self, task: AssetTask) -> AssetFetchResult:
        metadata: dict[str, Any] = {}
        final_url = asset_page_url(task)
        html_body = ""
        metadata_error = ""
        metadata_retryable = True
        try:
            final_url, metadata = await self.fetch_structured_asset_data(
                task,
                stage="core metadata",
                prompt=(
                    "Extract the product title, a concise factual product description, and the favicon href "
                    "from this product homepage. Return empty strings only when the value is unavailable."
                ),
                json_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "favicon_href": {"type": "string"},
                    },
                    "required": ["title", "description", "favicon_href"],
                },
            )
        except Exception as error:
            metadata_error = str(error)[:500] or type(error).__name__
            metadata_retryable = bool(getattr(error, "retryable", True))
            log_info("assets.browser_core_metadata.failed", url=final_url, error=metadata_error[:300])

        title = clean_asset_text(metadata.get("title"), 120)
        description = clean_asset_text(metadata.get("description"), 500)
        if not title or not description:
            try:
                content_url, html_body = await self.fetch_homepage_content(task)
                final_url = content_url
                title = title or read_html_title(html_body)
                description = description or read_html_meta(
                    html_body,
                    {"description", "og:description", "twitter:description"},
                )
            except Exception as content_error:
                content_message = str(content_error)[:500] or type(content_error).__name__
                metadata_error = "; ".join(
                    value for value in (metadata_error, content_message) if value
                )[:500]
                metadata_retryable = metadata_retryable or bool(getattr(content_error, "retryable", True))
                log_info("assets.browser_content.failed", url=final_url, error=content_message[:300])

        validation_errors = []
        if not description:
            validation_errors.append("description_empty")
        if validation_errors:
            metadata_error = "; ".join(
                value for value in (metadata_error, *validation_errors) if value
            )[:500]
        return AssetFetchResult(
            final_url=final_url,
            html=html_body,
            title=title,
            description=description,
            favicon_href=clean_asset_text(metadata.get("favicon_href"), 1000),
            metadata_error=metadata_error,
            metadata_retryable=metadata_retryable,
        )

    async def fetch_homepage_key_features(self, task: AssetTask) -> AssetFetchResult:
        final_url, metadata = await self.fetch_structured_asset_data(
            task,
            stage="key features",
            prompt=(
                "Extract 1 to 6 concrete product capabilities from this product homepage. "
                "Use short feature names and factual one-sentence descriptions."
            ),
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key_features": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["name", "description"],
                        },
                    },
                },
                "required": ["key_features"],
            },
        )
        key_features = clean_key_features(metadata.get("key_features"))
        return AssetFetchResult(
            final_url=final_url,
            key_features=key_features,
            metadata_error="" if key_features else "features_empty",
            metadata_retryable=True,
        )

    async def fetch_homepage_categories(
        self,
        task: AssetTask,
        category_options: list[str],
    ) -> AssetFetchResult:
        category_hint = ", ".join(category_options[:180])
        final_url, metadata = await self.fetch_structured_asset_data(
            task,
            stage="category",
            prompt=(
                "Choose the best category slugs for this AI product. Category values must be exact slugs "
                f"from this list: {category_hint}. Use category_l1 for the broad category and category_l2 "
                "for the most specific category. Return empty strings when unsure."
            ),
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category_l1": {"type": "string"},
                    "category_l2": {"type": "string"},
                },
                "required": ["category_l1", "category_l2"],
            },
        )
        valid_categories = set(category_options)
        extracted_category_l1 = clean_category_slug(metadata.get("category_l1"))
        extracted_category_l2 = clean_category_slug(metadata.get("category_l2"))
        category_l1 = extracted_category_l1 if extracted_category_l1 in valid_categories else ""
        category_l2 = extracted_category_l2 if extracted_category_l2 in valid_categories else ""
        rejected_categories = [
            slug
            for slug in (extracted_category_l1, extracted_category_l2)
            if slug and slug not in valid_categories
        ]
        metadata_error = ""
        if not category_l1 and not category_l2:
            metadata_error = (
                "category_unmatched=" + ",".join(rejected_categories)
                if rejected_categories
                else "category_empty"
            )
        return AssetFetchResult(
            final_url=final_url,
            category_l1=category_l1,
            category_l2=category_l2,
            metadata_error=metadata_error,
            metadata_retryable=True,
        )

    async def capture_homepage_screenshot(self, task: AssetTask) -> AssetFetchResult:

        errors: list[str] = []
        retryable_errors: list[bool] = []
        for target_url in self.asset_candidate_urls(task):
            try:
                snapshot = await self.call_quick_action("snapshot", self.browser_payload(target_url))
                screenshot_raw = snapshot.get("screenshot") if isinstance(snapshot, dict) else None
                if not screenshot_raw:
                    raise RuntimeError("snapshot returned no screenshot")
                if isinstance(screenshot_raw, str) and "," in screenshot_raw[:40]:
                    screenshot_raw = screenshot_raw.split(",", 1)[1]
                screenshot = base64.b64decode(str(screenshot_raw), validate=False)
                return AssetFetchResult(
                    final_url=target_url,
                    screenshot=screenshot,
                )
            except Exception as error:
                errors.append(f"{target_url}: {str(error)[:220]}")
                retryable_errors.append(bool(getattr(error, "retryable", True)))

        raise AssetPipelineError(
            "Browser Run asset capture failed. " + " | ".join(errors),
            retryable=any(retryable_errors),
        )


class R2AssetUploader:
    def __init__(self, config: Config):
        if not config.r2_access_key_id or not config.r2_secret_access_key:
            raise RuntimeError("Missing CLOUDFLARE_R2_ACCESS_KEY_ID or CLOUDFLARE_R2_SECRET_ACCESS_KEY.")
        if not config.r2_bucket:
            raise RuntimeError("Missing CLOUDFLARE_R2_BUCKET.")
        self.account_id = config.cloudflare_account_id
        self.access_key_id = config.r2_access_key_id
        self.secret_access_key = config.r2_secret_access_key
        self.bucket = config.r2_bucket

    def signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_access_key).encode("utf-8")
        for value in (date_stamp, "auto", "s3", "aws4_request"):
            key = hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
        return key

    async def put_object(self, key: str, body: bytes, content_type: str) -> None:
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        canonical_uri = f"/{quote(self.bucket, safe='')}/{quote(key, safe='/-_.~')}"
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "content-type": content_type,
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(self.signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.put(f"https://{host}{canonical_uri}", headers=headers, content=body)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"R2 upload failed for bucket {self.bucket}: HTTP {response.status_code} {response.text[:300]}")

    async def check_access(self) -> None:
        await self.put_object("_runner-healthcheck.txt", b"ok\n", "text/plain")


def amount_minor(value: str | None) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except InvalidOperation:
        return None


def derive_tool_pricing_summary(plans: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [price for plan in plans for price in plan.get("prices", [])]
    has_free = any(
        (price.get("amount") == "0") or plan.get("name", "").lower() == "free"
        for plan in plans
        for price in plan.get("prices", [])
    )
    custom_only = bool(prices) and all(price.get("custom_quote") for price in prices)
    paid_prices = [
        price
        for price in prices
        if not price.get("custom_quote") and decimal_value(price.get("amount")) > 0
    ]
    usage_only = bool(paid_prices) and all(price.get("kind") == "usage" for price in paid_prices)

    if paid_prices and has_free:
        pricing_model = "freemium"
    elif paid_prices and usage_only:
        pricing_model = "usage_based"
    elif paid_prices:
        pricing_model = "paid"
    elif custom_only:
        pricing_model = "contact"
    elif has_free:
        pricing_model = "free"
    else:
        pricing_model = "unknown"

    def candidate_rank(price: dict[str, Any]) -> tuple[int, Decimal]:
        interval = price.get("billing_interval")
        amount = decimal_value(price.get("amount"))
        if interval == "monthly":
            return (0, amount)
        if interval == "yearly":
            return (1, amount)
        return (2, amount)

    chosen = sorted(paid_prices, key=candidate_rank)[0] if paid_prices else None
    if chosen and chosen.get("billing_interval") in {"monthly", "yearly"}:
        pricing_interval = chosen.get("billing_interval")
    elif usage_only:
        pricing_interval = "usage"
    elif custom_only:
        pricing_interval = "custom"
    else:
        pricing_interval = "none"

    starting_minor = amount_minor(chosen.get("amount")) if chosen else None
    currency = normalize_currency(chosen.get("currency") if chosen else None) if chosen else None
    return {
        "pricing_model": pricing_model,
        "has_free_plan": 1 if has_free else 0,
        "pricing_interval": pricing_interval,
        "pricing_currency_code": None if currency == "USD" else currency,
        "starting_price_minor": None if currency == "USD" else starting_minor,
        "starting_price_usd_minor": starting_minor if currency == "USD" else None,
    }


def previous_traffic_month() -> str:
    now = datetime.now(timezone.utc)
    first_this_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    previous = first_this_month - timedelta(days=1)
    return f"{previous.year}-{previous.month:02d}-01"


def to_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def to_integer(value: Any) -> int | None:
    parsed = to_number(value)
    if parsed is None:
        return None
    return max(0, int(parsed))


def to_month_start(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    match = re.match(r"^(\d{4})-(\d{2})", text)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-01"


def country_name(country_code: Any) -> str | None:
    if not country_code:
        return None
    return str(country_code).upper()


def parse_country_rank(payload: dict[str, Any]) -> dict[str, Any]:
    country_rank = payload.get("CountryRank") or {}
    country_code = country_rank.get("CountryCode")
    rank = to_integer(country_rank.get("Rank"))
    return {
        "country_rank_country": country_code,
        "country_rank": rank,
        "country_rank_text": f"{country_code} #{rank}" if country_code and rank is not None else None,
    }


def parse_traffic_sources(payload: dict[str, Any]) -> dict[str, Any]:
    sources = payload.get("TrafficSources") or {}
    if not isinstance(sources, dict):
        sources = {}

    social_organic = to_number(sources.get("SocialOrganic"))
    social_paid = to_number(sources.get("SocialPaid"))
    search_organic = to_number(sources.get("SearchOrganic"))
    search_paid = to_number(sources.get("SearchPaid"))

    def sum_present(*values: float | None) -> float | None:
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    def prefer_explicit(explicit: float | None, fallback: float | None) -> float | None:
        return explicit if explicit is not None else fallback

    return {
        "social_traffic_share": prefer_explicit(
            to_number(sources.get("Social")),
            sum_present(social_organic, social_paid),
        ),
        "social_organic_traffic_share": social_organic,
        "social_paid_traffic_share": social_paid,
        "paid_referrals_traffic_share": to_number(sources.get("Paid Referrals")),
        "mail_traffic_share": to_number(sources.get("Mail")),
        "search_traffic_share": prefer_explicit(
            to_number(sources.get("Search")),
            sum_present(search_organic, search_paid),
        ),
        "search_organic_traffic_share": search_organic,
        "search_paid_traffic_share": search_paid,
        "direct_traffic_share": to_number(sources.get("Direct")),
        "referrals_traffic_share": to_number(sources.get("Referrals")),
        "display_ads_traffic_share": to_number(sources.get("DisplayAds")),
        "gen_ai_traffic_share": to_number(sources.get("GenAi")),
        "affiliate_traffic_share": to_number(sources.get("Affiliate")),
    }


def parse_top_countries(payload: dict[str, Any]) -> dict[str, Any]:
    countries = payload.get("TopCountryShares") or []
    result: dict[str, Any] = {}
    for index in range(1, 6):
        country = countries[index - 1] if index - 1 < len(countries) else {}
        result[f"top_country_{index}"] = country_name(country.get("CountryCode"))
        result[f"top_country_{index}_traffic_share"] = to_number(country.get("Value"))
    return result


def parse_top_keywords(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_keywords = payload.get("TopKeywords")
    if not isinstance(raw_keywords, list):
        return []

    keywords: list[dict[str, Any]] = []
    for raw_keyword in raw_keywords:
        if not isinstance(raw_keyword, dict):
            continue
        name = str(raw_keyword.get("Name") or "").strip()
        if not name:
            continue
        keywords.append(
            {
                "name": name,
                "volume": to_integer(raw_keyword.get("Volume")),
                "estimated_value": to_number(raw_keyword.get("EstimatedValue")),
                "cpc": to_number(raw_keyword.get("Cpc")),
            }
        )
    return keywords


def parse_ai_traffic(payload: dict[str, Any]) -> dict[str, Any] | None:
    ai_details = payload.get("AiTrafficDetails")
    if not isinstance(ai_details, dict):
        return None

    traffic = ai_details.get("Traffic")
    traffic = traffic if isinstance(traffic, dict) else {}
    distribution = traffic.get("Distribution")
    distribution = distribution if isinstance(distribution, dict) else {}

    sources: list[dict[str, Any]] = []
    raw_sources = distribution.get("Chatbots")
    if isinstance(raw_sources, list):
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                continue
            name = str(raw_source.get("Name") or "").strip()
            share = to_number(raw_source.get("Value"))
            if name and share is not None:
                sources.append({"name": name, "share": share})

    history: list[dict[str, Any]] = []
    raw_history = distribution.get("Chart")
    if isinstance(raw_history, list):
        for raw_series in raw_history:
            if not isinstance(raw_series, dict):
                continue
            name = str(raw_series.get("Name") or "").strip()
            points: list[dict[str, Any]] = []
            raw_points = raw_series.get("History")
            if isinstance(raw_points, list):
                for raw_point in raw_points:
                    if not isinstance(raw_point, dict):
                        continue
                    date = to_month_start(raw_point.get("Date"))
                    share = to_number(raw_point.get("Value"))
                    if date and share is not None:
                        points.append({"date": date, "share": share})
            if name and points:
                history.append({"name": name, "points": points})

    rankings: list[dict[str, Any]] = []
    raw_rankings = traffic.get("Split")
    if isinstance(raw_rankings, list):
        for raw_ranking in raw_rankings:
            if not isinstance(raw_ranking, dict):
                continue
            name = str(raw_ranking.get("Name") or "").strip()
            rank = to_integer(raw_ranking.get("Rank"))
            if name:
                rankings.append({"name": name, "rank": rank})

    top_prompts = ai_details.get("TopPrompts")
    top_prompts = top_prompts if isinstance(top_prompts, dict) else {}
    prompts = top_prompts.get("Prompts")
    prompt_count = len(prompts) if isinstance(prompts, list) else 0
    prompt_error = str(top_prompts.get("ErrorMessage") or "").strip() or None

    return {
        "total_visits": to_number(ai_details.get("TotalVisits")),
        "referral_share": to_number(ai_details.get("ReferralTraffic")),
        "boundary": str(distribution.get("Boundary") or "").strip() or None,
        "sources": sources,
        "history": history,
        "rankings": rankings,
        "top_prompts": {
            "status": to_integer(top_prompts.get("Status")),
            "error": prompt_error,
            "count": prompt_count,
        },
    }


def build_traffic_metrics(row: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    website = str(row.get("website") or "").strip()
    if website:
        metrics["website"] = website
    if row.get("query_date"):
        metrics["query_date"] = row["query_date"]
    if row.get("engagement_visits") is not None:
        metrics["engagement_visits"] = row["engagement_visits"]

    traffic_source_fields = {
        "social": "social_traffic_share",
        "social_organic": "social_organic_traffic_share",
        "social_paid": "social_paid_traffic_share",
        "paid_referrals": "paid_referrals_traffic_share",
        "mail": "mail_traffic_share",
        "search": "search_traffic_share",
        "search_organic": "search_organic_traffic_share",
        "search_paid": "search_paid_traffic_share",
        "direct": "direct_traffic_share",
        "referrals": "referrals_traffic_share",
        "display_ads": "display_ads_traffic_share",
        "gen_ai": "gen_ai_traffic_share",
        "affiliate": "affiliate_traffic_share",
    }
    traffic_sources = {
        key: row[field]
        for key, field in traffic_source_fields.items()
        if row.get(field) is not None
    }
    if traffic_sources:
        metrics["traffic_sources"] = traffic_sources

    geographies = []
    for index in range(1, 6):
        country_code = row.get(f"top_country_{index}")
        share = row.get(f"top_country_{index}_traffic_share")
        if country_code and share is not None:
            geographies.append({"country_code": country_code, "share": share})
    if geographies:
        metrics["top_geographies"] = geographies

    raw_keywords = row.get("top_keywords")
    if isinstance(raw_keywords, list) and raw_keywords:
        metrics["top_search_keywords"] = [
            {
                "name": keyword.get("name"),
                "volume": keyword.get("volume"),
                "estimated_traffic": keyword.get("estimated_value"),
                "cpc": keyword.get("cpc"),
            }
            for keyword in raw_keywords
            if isinstance(keyword, dict) and keyword.get("name")
        ]

    ai_traffic = row.get("ai_traffic")
    if isinstance(ai_traffic, dict):
        metrics["ai_traffic"] = ai_traffic

    return metrics


def parse_monthly_rows(payload: dict[str, Any], domain: str, requested_month: str) -> list[dict[str, Any]]:
    engagements = payload.get("Engagments") or {}
    estimated_visits = payload.get("EstimatedMonthlyVisits") or {}
    snapshot_month = to_month_start(payload.get("SnapshotDate"))
    query_date = None
    if to_integer(engagements.get("Year")) and to_integer(engagements.get("Month")):
        query_date = f"{to_integer(engagements.get('Year'))}-{to_integer(engagements.get('Month')):02d}-01"

    ai_traffic = parse_ai_traffic(payload)
    base_fields = {
        "website": payload.get("SiteName") or domain,
        "query_date": query_date,
        "engagement_visits": to_integer(engagements.get("Visits")),
        "global_rank": to_integer((payload.get("GlobalRank") or {}).get("Rank")),
        **parse_country_rank(payload),
        "bounce_rate": to_number(engagements.get("BounceRate")),
        "pages_per_visit": to_number(engagements.get("PagePerVisit")),
        "avg_visit_duration_seconds": to_integer(engagements.get("TimeOnSite")),
        **parse_traffic_sources(payload),
        **parse_top_countries(payload),
    }
    top_keywords = parse_top_keywords(payload)
    if top_keywords:
        base_fields["top_keywords"] = top_keywords
    if ai_traffic is not None:
        base_fields["ai_traffic"] = ai_traffic

    monthly_rows: list[dict[str, Any]] = []
    if isinstance(estimated_visits, dict):
        for month, visits in estimated_visits.items():
            traffic_month = to_month_start(month)
            if traffic_month:
                monthly_rows.append({"traffic_month": traffic_month, "visits": to_integer(visits)})

    if not monthly_rows:
        traffic_month = snapshot_month or requested_month
        monthly_rows.append({"traffic_month": traffic_month, "visits": to_integer(engagements.get("Visits"))})

    monthly_rows.sort(key=lambda row: row["traffic_month"])
    latest_month = monthly_rows[-1]["traffic_month"]
    for row in monthly_rows:
        if row["traffic_month"] == latest_month:
            row.update(base_fields)
        else:
            row.update({"website": domain, "query_date": None})
    return monthly_rows


def requested_month_has_traffic_data(rows: list[dict[str, Any]], requested_month: str) -> bool:
    return any(
        row.get("traffic_month") == requested_month
        and (row.get("visits") is not None or row.get("global_rank") is not None)
        for row in rows
    )


def latest_observed_traffic_month(rows: list[dict[str, Any]]) -> str | None:
    months = [str(row.get("traffic_month") or "") for row in rows if row.get("traffic_month")]
    return max(months) if months else None


DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL = """
    INSERT INTO domain_traffic_monthly (
      normalized_domain, source, traffic_month, visits, global_rank,
      country_rank_country, country_rank, bounce_rate, pages_per_visit,
      avg_visit_duration_seconds, gen_ai_traffic_share, ai_visits,
      ai_referral_share, metrics_json, metrics_schema_version,
      source_snapshot_id, captured_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (normalized_domain, source, traffic_month) DO UPDATE
    SET visits = coalesce(excluded.visits, domain_traffic_monthly.visits),
        global_rank = coalesce(excluded.global_rank, domain_traffic_monthly.global_rank),
        country_rank_country = coalesce(
          excluded.country_rank_country,
          domain_traffic_monthly.country_rank_country
        ),
        country_rank = coalesce(excluded.country_rank, domain_traffic_monthly.country_rank),
        bounce_rate = coalesce(excluded.bounce_rate, domain_traffic_monthly.bounce_rate),
        pages_per_visit = coalesce(
          excluded.pages_per_visit,
          domain_traffic_monthly.pages_per_visit
        ),
        avg_visit_duration_seconds = coalesce(
          excluded.avg_visit_duration_seconds,
          domain_traffic_monthly.avg_visit_duration_seconds
        ),
        gen_ai_traffic_share = coalesce(
          excluded.gen_ai_traffic_share,
          domain_traffic_monthly.gen_ai_traffic_share
        ),
        ai_visits = coalesce(excluded.ai_visits, domain_traffic_monthly.ai_visits),
        ai_referral_share = coalesce(
          excluded.ai_referral_share,
          domain_traffic_monthly.ai_referral_share
        ),
        metrics_json = json_patch(domain_traffic_monthly.metrics_json, excluded.metrics_json),
        metrics_schema_version = max(
          domain_traffic_monthly.metrics_schema_version,
          excluded.metrics_schema_version
        ),
        source_snapshot_id = coalesce(
          excluded.source_snapshot_id,
          domain_traffic_monthly.source_snapshot_id
        ),
        captured_at = excluded.captured_at,
        updated_at = excluded.captured_at
"""


def build_domain_traffic_monthly_upsert(
    domain: str,
    row: dict[str, Any],
    *,
    captured_at: str | None = None,
    source_snapshot_id: int | None = None,
) -> tuple[str, list[Any]]:
    ai_traffic = row.get("ai_traffic")
    ai_traffic = ai_traffic if isinstance(ai_traffic, dict) else {}
    fetched_at = captured_at or utc_now_iso()
    return (
        DOMAIN_TRAFFIC_MONTHLY_UPSERT_SQL,
        [
            domain,
            TRAFFIC_SOURCE,
            row.get("traffic_month"),
            row.get("visits"),
            row.get("global_rank") or None,
            row.get("country_rank_country"),
            row.get("country_rank") or None,
            row.get("bounce_rate"),
            row.get("pages_per_visit"),
            row.get("avg_visit_duration_seconds"),
            row.get("gen_ai_traffic_share"),
            to_integer(ai_traffic.get("total_visits")),
            to_number(ai_traffic.get("referral_share")),
            json.dumps(build_traffic_metrics(row), ensure_ascii=False),
            TRAFFIC_METRICS_SCHEMA_VERSION,
            source_snapshot_id,
            fetched_at,
        ],
    )


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
    def __init__(self, ahref_api_key: str) -> None:
        self.ahref_api_key = ahref_api_key.strip()

    async def fetch(self, domain: str) -> DomainStateResult:
        ahrefs_result, whois_result = await asyncio.gather(
            self.fetch_ahrefs_domain_rating(domain),
            self.fetch_domain_created_at(domain),
        )
        domain_created_at = ahrefs_result.domain_created_at or whois_result.domain_created_at
        if ahrefs_result.status == "done" or domain_created_at:
            status = "done"
            error = None
        elif ahrefs_result.status == "no_data" or whois_result.status == "no_data":
            status = "no_data"
            error = ahrefs_result.error or whois_result.error
        else:
            status = "failed"
            error = ahrefs_result.error or whois_result.error
        return DomainStateResult(
            status=status,
            domain_rating=ahrefs_result.domain_rating,
            domain_created_at=domain_created_at,
            error=error,
        )

    async def fetch_ahrefs_domain_rating(self, domain: str) -> DomainStateResult:
        clean_domain = normalize_domain(domain)
        if not clean_domain:
            return DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error="invalid_domain")
        if not self.ahref_api_key:
            raise RuntimeError("AHREF_API_KEY is required for Ahrefs Domain Rating requests")

        endpoint = httpx.URL(AHREFS_DOMAIN_RATING_URL).copy_add_param("target", clean_domain)
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


class PricingClient:
    def __init__(self, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds

    async def fetch_url(self, url: str) -> PricingFetchResult:
        try:
            normalized = normalize_pricing_url(url)
            headers = pricing_request_headers(normalized)
        except ValueError as error:
            return PricingFetchResult(url=url, final_url=url, status=0, content_type="", html="", error=str(error))

        try:
            started_at = time.perf_counter()
            async with httpx.AsyncClient(
                timeout=float(self.timeout_seconds),
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(normalized)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        except Exception as error:
            log_info("pricing.fetch.request_error", url=normalized, error=str(error)[:300])
            return PricingFetchResult(url=normalized, final_url=normalized, status=0, content_type="", html="", error=str(error)[:300])

        content_type = response.headers.get("content-type", "")
        html_body = ""
        if any(kind in content_type.lower() for kind in ("html", "xml", "text")):
            body = response.content[:MAX_PRICING_HTML_BYTES]
            html_body = body.decode(response.encoding or "utf-8", errors="replace")
        log_info(
            "pricing.fetch.response",
            url=normalized,
            final_url=str(response.url),
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            content_type=content_type[:120],
        )
        return PricingFetchResult(
            url=normalized,
            final_url=str(response.url),
            status=response.status_code,
            content_type=content_type,
            html=html_body,
            error="" if response.is_success else f"HTTP {response.status_code}",
        )

    async def fetch_sitemap_body(self, origin_url: str) -> str:
        try:
            origin = pricing_url_origin(origin_url)
        except ValueError:
            return ""
        root = await self.fetch_url(urljoin(origin, "/sitemap.xml"))
        if root.status != 200 or not root.html:
            return ""

        bodies = [root.html]
        nested_sitemaps = []
        for loc in extract_sitemap_locs(root.html):
            try:
                parsed = urlsplit(loc)
            except ValueError:
                continue
            if parsed.netloc == urlsplit(origin).netloc and parsed.path.lower().endswith(".xml"):
                nested_sitemaps.append(loc)
        for sitemap_url in nested_sitemaps[:4]:
            nested = await self.fetch_url(sitemap_url)
            if nested.status == 200 and nested.html:
                bodies.append(nested.html)
        return "\n".join(bodies)

    async def choose_pricing_page(self, task: PricingTask) -> PricingFetchResult:
        first = await self.fetch_url(task.source_url)
        first_text = parse_pricing_html(first.html).text if first.html else ""
        if first.status == 200 and is_strict_pricing_url(first.final_url) and pricing_text_quality(first_text) >= 12:
            return PricingFetchResult(
                first.url,
                first.final_url,
                first.status,
                first.content_type,
                first.html,
                first.error,
                "found",
                "source_url",
            )

        try:
            home_url = pricing_url_origin(task.official_url or task.source_url)
        except ValueError:
            home_url = task.source_url
        home = await self.fetch_url(home_url)
        sitemap_body = await self.fetch_sitemap_body(home.final_url or home_url)
        candidates = discover_pricing_urls(home.final_url or home_url, home.html, sitemap_body) if home.html else []
        best_result = first
        best_score = (
            pricing_url_score(first.final_url) + pricing_text_quality(first_text)
            if first.status == 200
            else -1000
        )
        for candidate in candidates:
            if candidate.rstrip("/") == first.final_url.rstrip("/"):
                continue
            result = await self.fetch_url(candidate)
            text = parse_pricing_html(result.html).text if result.html else ""
            if result.status != 200 or not is_strict_pricing_url(result.final_url):
                continue
            score = pricing_url_score(result.final_url) + pricing_text_quality(text)
            if score > best_score:
                best_result = result
                best_score = score
        best_text = parse_pricing_html(best_result.html).text if best_result.html else ""
        if (
            best_result.status == 200
            and is_strict_pricing_url(best_result.final_url)
            and pricing_text_quality(best_text) > 0
        ):
            return PricingFetchResult(
                best_result.url,
                best_result.final_url,
                best_result.status,
                best_result.content_type,
                best_result.html,
                best_result.error,
                "found",
                "candidate_scored",
            )

        contact_candidates = discover_contact_sales_urls(home.final_url or home_url, home.html, sitemap_body) if home.html else []
        for candidate in contact_candidates:
            result = await self.fetch_url(candidate)
            text = parse_pricing_html(result.html).text if result.html else ""
            if result.status == 200 and len(text) >= 80:
                return PricingFetchResult(
                    result.url,
                    result.final_url,
                    result.status,
                    result.content_type,
                    result.html,
                    result.error,
                    "contact_sales",
                    "contact_or_demo",
                )

        return PricingFetchResult(
            best_result.url,
            best_result.final_url,
            best_result.status,
            best_result.content_type,
            best_result.html,
            best_result.error or "No credible pricing page found",
            "not_found",
            "none",
        )


class D1Client:
    def __init__(self, config: Config):
        self.url = (
            f"{D1_API_BASE}/accounts/{config.cloudflare_account_id}"
            f"/d1/database/{config.cloudflare_d1_database_id}/query"
        )
        self.headers = {
            "Authorization": f"Bearer {config.cloudflare_api_token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> "D1Client":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def _request(self, body: dict[str, Any], retry_transient: bool = False) -> list[dict[str, Any]]:
        response: httpx.Response | None = None
        last_error: Exception | None = None
        max_attempts = 4 if retry_transient else 1
        for attempt in range(max_attempts):
            try:
                response = await self.client.post(self.url, headers=self.headers, json=body)
                if response.status_code not in {429, 502, 503, 504}:
                    response.raise_for_status()
                    break
                last_error = httpx.HTTPStatusError(
                    f"D1 transient HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                last_error = error
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code not in {429, 502, 503, 504}:
                    raise
            if attempt < max_attempts - 1:
                await asyncio.sleep(min(8.0, (2**attempt) + random.uniform(0.0, 0.5)))

        if response is None or response.status_code in {429, 502, 503, 504}:
            raise RuntimeError(f"D1 request failed after {max_attempts} attempt(s): {last_error}") from last_error

        payload = response.json()
        if not payload.get("success", False):
            raise RuntimeError(f"D1 query failed: {payload}")
        result = payload.get("result")
        results = result if isinstance(result, list) else [result or {}]
        for query_result in results:
            if isinstance(query_result, dict) and not query_result.get("success", True):
                raise RuntimeError(f"D1 query failed: {payload}")
        return [query_result for query_result in results if isinstance(query_result, dict)]

    async def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        normalized_sql = sql.lstrip().upper()
        retry_transient = normalized_sql.startswith(("SELECT", "PRAGMA", "EXPLAIN"))
        results = await self._request({"sql": sql, "params": params or []}, retry_transient=retry_transient)
        return results[0] if results else {}

    async def batch(self, statements: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
        if not statements:
            return []
        return await self._request(
            {
                "batch": [
                    {"sql": sql, "params": params}
                    for sql, params in statements
                ]
            }
        )

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        result = await self.execute(sql, params)
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            return result["results"]
        return []

    async def run(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        result = await self.execute(sql, params)
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
        )
        last_row_id = to_integer(meta.get("last_row_id"))
        return last_row_id if last_row_id and last_row_id > 0 else None

    async def insert_result(self, task: TrafficTask, result: FetchResult) -> None:
        rows = result.monthly_rows or [{"traffic_month": task.traffic_month}]
        log_info(
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
        log_info(
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
        statements = [
            build_domain_traffic_monthly_upsert(
                domain,
                row,
                captured_at=captured_at,
                source_snapshot_id=to_integer(row.get("_source_snapshot_id")),
            )
            for row in rows
            if row.get("traffic_month")
        ]
        if statements:
            await self.batch(statements)

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
                )


async def backfill_domain_traffic_monthly(
    config: Config,
    limit: int | None = None,
    *,
    page_size: int = 100,
    write_batch_size: int = 40,
) -> dict[str, int]:
    """Rebuild the domain projection from append-only Similarweb snapshots.

    Snapshots are replayed from oldest to newest. The projection upsert is
    idempotent, so this command is safe to resume or run again after a parser
    change.
    """
    counts = {
        "snapshots_scanned": 0,
        "snapshots_projected": 0,
        "snapshots_invalid": 0,
        "monthly_rows_upserted": 0,
    }
    last_snapshot_id = 0

    async with D1Client(config) as d1:
        while limit is None or counts["snapshots_scanned"] < limit:
            remaining = page_size if limit is None else min(page_size, limit - counts["snapshots_scanned"])
            if remaining <= 0:
                break
            snapshots = await d1.query(
                """
                SELECT id, normalized_domain, traffic_month, fetched_at, raw_payload
                FROM domain_traffic_snapshots
                WHERE source = ?
                  AND status = 'done'
                  AND id > ?
                  AND json_valid(raw_payload)
                  AND raw_payload <> '{}'
                ORDER BY id ASC
                LIMIT ?
                """,
                [TRAFFIC_SOURCE, last_snapshot_id, remaining],
            )
            if not snapshots:
                break

            statements: list[tuple[str, list[Any]]] = []
            for snapshot in snapshots:
                snapshot_id = to_integer(snapshot.get("id")) or 0
                last_snapshot_id = max(last_snapshot_id, snapshot_id)
                counts["snapshots_scanned"] += 1
                raw_payload = snapshot.get("raw_payload")
                try:
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if not isinstance(payload, dict):
                    counts["snapshots_invalid"] += 1
                    continue

                domain = normalize_domain(str(snapshot.get("normalized_domain") or ""))
                requested_month = to_month_start(snapshot.get("traffic_month"))
                if not domain or not requested_month:
                    counts["snapshots_invalid"] += 1
                    continue
                try:
                    rows = parse_monthly_rows(payload, domain, requested_month)
                except Exception as error:
                    counts["snapshots_invalid"] += 1
                    log_error(
                        "traffic_projection_backfill.parse_failed",
                        snapshot_id=snapshot_id,
                        domain=domain,
                        error=str(error)[:300],
                    )
                    continue

                captured_at = str(snapshot.get("fetched_at") or "").strip() or utc_now_iso()
                row_statements = [
                    build_domain_traffic_monthly_upsert(
                        domain,
                        row,
                        captured_at=captured_at,
                        source_snapshot_id=snapshot_id,
                    )
                    for row in rows
                    if row.get("traffic_month")
                ]
                if not row_statements:
                    counts["snapshots_invalid"] += 1
                    continue
                statements.extend(row_statements)
                counts["snapshots_projected"] += 1

            for offset in range(0, len(statements), write_batch_size):
                batch = statements[offset : offset + write_batch_size]
                await d1.batch(batch)
                counts["monthly_rows_upserted"] += len(batch)

            log_info(
                "traffic_projection_backfill.page",
                last_snapshot_id=last_snapshot_id,
                **counts,
            )

    return counts


class RunnerTelemetry:
    WORKLOADS = ["assets", "traffic", "domain_state", "pricing", "enrichment"]

    def __init__(self, d1: D1Client, config: Config):
        self.d1 = d1
        self.instance_id = config.runner_instance_id
        self.version = config.runner_version

    async def start(self, workload: str) -> int:
        now = utc_now_iso()
        await self.d1.run(
            """
            INSERT INTO runner_instances (
              instance_id, service, version, status, workloads_json,
              started_at, last_heartbeat_at, last_error, metadata_json, updated_at
            )
            VALUES (?, 'tool-data-runner', ?, 'healthy', ?, ?, ?, NULL, '{}', ?)
            ON CONFLICT(instance_id) DO UPDATE SET
              version = excluded.version,
              workloads_json = excluded.workloads_json,
              last_heartbeat_at = excluded.last_heartbeat_at,
              stopped_at = NULL,
              updated_at = excluded.updated_at
            """,
            [self.instance_id, self.version, json.dumps(self.WORKLOADS), now, now, now],
        )
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
        status = "failed" if error else "succeeded"
        counts = counts or {}
        counts_json = json.dumps(counts, sort_keys=True)
        degraded_counts = {
            key: int(counts.get(key) or 0)
            for key in ("failed", "materialization_failed", "stale")
            if int(counts.get(key) or 0) > 0
        }
        health_error = error
        if not health_error and degraded_counts:
            health_error = "Batch completed with " + ", ".join(
                f"{key}={value}" for key, value in degraded_counts.items()
            )
        statements: list[tuple[str, list[Any]]] = [
            (
                """
                UPDATE runner_runs
                SET status = ?, finished_at = ?, counts_json = ?, error = ?
                WHERE id = ? AND instance_id = ? AND status = 'running'
                """,
                [status, now, counts_json, error, run_id, self.instance_id],
            ),
            (
                """
                UPDATE runner_instances
                SET status = CASE
                      WHEN ? IS NOT NULL THEN 'degraded'
                      WHEN EXISTS (
                        SELECT 1
                        FROM runner_runs latest
                        WHERE latest.instance_id = ? AND latest.status = 'failed'
                          AND latest.id = (
                            SELECT max(candidate.id)
                            FROM runner_runs candidate
                            WHERE candidate.instance_id = latest.instance_id
                              AND candidate.workload = latest.workload
                          )
                      ) THEN 'degraded'
                      ELSE 'healthy'
                    END,
                    last_heartbeat_at = ?,
                    last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                    last_error = CASE
                      WHEN ? IS NOT NULL THEN ?
                      WHEN EXISTS (
                        SELECT 1
                        FROM runner_runs latest
                        WHERE latest.instance_id = ? AND latest.status = 'failed'
                          AND latest.id = (
                            SELECT max(candidate.id)
                            FROM runner_runs candidate
                            WHERE candidate.instance_id = latest.instance_id
                              AND candidate.workload = latest.workload
                          )
                      ) THEN last_error
                      ELSE NULL
                    END,
                    updated_at = ?
                WHERE instance_id = ?
                """,
                [
                    health_error,
                    self.instance_id,
                    now,
                    health_error,
                    now,
                    health_error,
                    health_error,
                    self.instance_id,
                    now,
                    self.instance_id,
                ],
            ),
        ]
        await self.d1.batch(statements)

    async def heartbeat(self) -> None:
        now = utc_now_iso()
        await self.d1.run(
            """
            UPDATE runner_instances
            SET last_heartbeat_at = ?, updated_at = ?
            WHERE instance_id = ?
            """,
            [now, now, self.instance_id],
        )


class D1EnrichmentStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def evaluate_tool(self, tool_id: int) -> str:
        rows = await self.d1.query(
            """
            SELECT
              t.id,
              state.readiness AS previous_readiness,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'screenshot' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_screenshot,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_assets a
                WHERE a.tool_id = t.id AND a.asset_kind = 'favicon' AND a.is_current = 1
              ) THEN 1 ELSE 0 END AS has_favicon,
              CASE WHEN EXISTS (
                SELECT 1 FROM tool_localizations l
                WHERE l.tool_id = t.id AND l.translation_status = 'published'
                  AND l.published_at IS NOT NULL AND trim(l.name) <> ''
                  AND trim(coalesce(l.short_description, '')) <> ''
              ) THEN 1 ELSE 0 END AS has_localization,
              CASE WHEN EXISTS (SELECT 1 FROM tool_key_features f WHERE f.tool_id = t.id)
                     OR EXISTS (
                       SELECT 1 FROM tool_localizations l
                       WHERE l.tool_id = t.id AND l.translation_status = 'published'
                         AND l.published_at IS NOT NULL
                         AND json_array_length(coalesce(l.feature_highlights, '[]')) > 0
                     ) THEN 1 ELSE 0 END AS has_features,
              CASE WHEN t.primary_category_id IS NOT NULL
                     OR EXISTS (SELECT 1 FROM tool_categories tc WHERE tc.tool_id = t.id)
                   THEN 1 ELSE 0 END AS has_category,
              CASE WHEN EXISTS (SELECT 1 FROM tool_sources s WHERE s.tool_id = t.id)
                   THEN 1 ELSE 0 END AS has_source,
              CASE WHEN EXISTS (
                SELECT 1
                FROM domain_traffic_monthly tm
                WHERE tm.normalized_domain = t.normalized_domain
                  AND tm.source = ?
              ) THEN 1 ELSE 0 END AS has_traffic,
              CASE WHEN EXISTS (
                SELECT 1 FROM domain_states ds
                WHERE ds.normalized_domain = t.normalized_domain AND ds.source = ?
                  AND ds.last_crawled_at IS NOT NULL
              ) THEN 1 ELSE 0 END AS has_domain_state,
              CASE WHEN t.pricing_model <> 'unknown' OR EXISTS (
                SELECT 1 FROM pricing_sources ps
                WHERE ps.tool_id = t.id AND ps.is_active = 1 AND ps.last_success_at IS NOT NULL
              ) THEN 1 ELSE 0 END AS has_pricing
            FROM tools t
            LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
            WHERE t.id = ?
            LIMIT 1
            """,
            [TRAFFIC_SOURCE, DOMAIN_STATE_SOURCE, tool_id],
        )
        if not rows:
            return "missing"
        row = rows[0]
        blocking = [
            name
            for name, column in (
                ("screenshot", "has_screenshot"),
                ("published_localization", "has_localization"),
                ("key_feature", "has_features"),
                ("category", "has_category"),
                ("source_evidence", "has_source"),
            )
            if not int(row.get(column) or 0)
        ]
        warnings = [
            name
            for name, column in (
                ("favicon", "has_favicon"),
                ("traffic", "has_traffic"),
                ("domain_state", "has_domain_state"),
                ("pricing", "has_pricing"),
            )
            if not int(row.get(column) or 0)
        ]
        readiness = "ready" if not blocking else "blocked"
        now = utc_now_iso()
        transitioned_at = now if row.get("previous_readiness") != readiness else None
        await self.d1.run(
            """
            INSERT INTO tool_enrichment_states (
              tool_id, readiness, blocking_json, warnings_json, evaluated_at, transitioned_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_id) DO UPDATE SET
              readiness = excluded.readiness,
              blocking_json = excluded.blocking_json,
              warnings_json = excluded.warnings_json,
              evaluated_at = excluded.evaluated_at,
              transitioned_at = coalesce(excluded.transitioned_at, tool_enrichment_states.transitioned_at),
              updated_at = excluded.updated_at
            """,
            [
                tool_id,
                readiness,
                json.dumps(blocking, separators=(",", ":")),
                json.dumps(warnings, separators=(",", ":")),
                now,
                transitioned_at,
                now,
            ],
        )
        if readiness == "ready":
            await self.d1.run(
                """
                UPDATE tools
                SET status = 'pending_review', updated_at = ?
                WHERE id = ? AND status = 'pending_enrich'
                """,
                [now, tool_id],
            )
        return readiness

    async def reconcile_active_tools(self, limit: int) -> dict[str, int]:
        rows = await self.d1.query(
            """
            SELECT t.id AS tool_id
            FROM tools t
            LEFT JOIN tool_enrichment_states state ON state.tool_id = t.id
            WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
              AND t.duplicate_of_tool_id IS NULL
            ORDER BY CASE WHEN t.status = 'pending_enrich' THEN 0 ELSE 1 END,
                     CASE WHEN state.evaluated_at IS NULL THEN 0 ELSE 1 END,
                     state.evaluated_at,
                     t.id
            LIMIT ?
            """,
            [max(1, limit)],
        )
        counts = {"evaluated": 0, "ready": 0, "blocked": 0, "missing": 0}
        for row in rows:
            readiness = await self.evaluate_tool(int(row.get("tool_id") or 0))
            counts["evaluated"] += 1
            counts[readiness] = counts.get(readiness, 0) + 1
        return counts

    async def reconcile_pending_tools(self, limit: int) -> dict[str, int]:
        """Backward-compatible entrypoint for callers predating active-catalog reconciliation."""
        return await self.reconcile_active_tools(limit)


class D1AssetStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def queue_missing_asset_tasks(self, limit: int) -> int:
        now = utc_now_iso()
        stale_done_before = iso_delta(hours=-24)
        rows = await self.d1.query(
            """
            SELECT
              t.id AS tool_id,
              t.canonical_slug,
              t.normalized_domain,
              t.official_url
            FROM tools t
            LEFT JOIN asset_tasks task
              ON task.tool_id = t.id
             AND task.source = ?
            WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
              AND t.duplicate_of_tool_id IS NULL
              AND trim(t.normalized_domain) <> ''
              AND (
                NOT EXISTS (
                  SELECT 1
                  FROM tool_assets ta
                  WHERE ta.tool_id = t.id
                    AND ta.asset_kind = 'screenshot'
                    AND ta.storage_bucket = ?
                    AND ta.is_current = 1
                )
                OR NOT EXISTS (
                  SELECT 1
                  FROM tool_assets ta
                  WHERE ta.tool_id = t.id
                    AND ta.asset_kind = 'favicon'
                    AND ta.is_current = 1
                )
                OR NOT EXISTS (
                  SELECT 1
                  FROM tool_localizations tl
                  WHERE tl.tool_id = t.id
                    AND tl.translation_status = 'published'
                    AND tl.published_at IS NOT NULL
                    AND trim(coalesce(tl.short_description, '')) <> ''
                )
                OR (
                  NOT EXISTS (
                    SELECT 1
                    FROM tool_key_features tkf
                    WHERE tkf.tool_id = t.id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tool_localizations tl
                    WHERE tl.tool_id = t.id
                      AND tl.translation_status = 'published'
                      AND tl.published_at IS NOT NULL
                      AND json_array_length(coalesce(tl.feature_highlights, '[]')) > 0
                  )
                )
                OR (
                  t.primary_category_id IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tool_categories tc
                    WHERE tc.tool_id = t.id
                  )
                )
              )
              AND (
                task.tool_id IS NULL
                OR (
                  task.dead_letter_at IS NULL
                  AND
                  task.status IN ('failed', 'sync_failed')
                  AND task.attempts < task.max_attempts
                  AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                )
                OR (task.status = 'queued' AND task.dead_letter_at IS NULL)
                OR (
                  task.status = 'processing'
                  AND task.dead_letter_at IS NULL
                  AND task.lease_expires_at IS NOT NULL
                  AND task.lease_expires_at <= ?
                )
                OR (
                  task.status = 'done'
                  AND task.updated_at < ?
                )
              )
            ORDER BY t.id ASC
            LIMIT ?
            """,
            [ASSET_SOURCE, ASSET_DB_STORAGE_BUCKET, now, now, stale_done_before, limit],
        )
        queued = 0
        for row in rows:
            tool_id = int(row.get("tool_id") or 0)
            domain = str(row.get("normalized_domain") or "")
            if tool_id <= 0 or not domain:
                continue
            meta = await self.d1.run(
                """
                INSERT INTO asset_tasks (
                  tool_id, normalized_domain, source, status, last_queued_at, next_retry_at, last_error
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL)
                ON CONFLICT (tool_id, source) DO UPDATE
                SET normalized_domain = excluded.normalized_domain,
                    status = excluded.status,
                    attempts = 0,
                    generation = asset_tasks.generation + 1,
                    last_queued_at = excluded.last_queued_at,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    dead_letter_at = NULL,
                    updated_at = excluded.last_queued_at
                WHERE asset_tasks.status = 'done'
                """,
                [tool_id, domain, ASSET_SOURCE, now],
            )
            queued += int(meta.get("changes") or 0)
        return queued

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[AssetTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(hours=1)
        rows = await self.d1.query(
            """
            SELECT
              task.tool_id,
              task.normalized_domain,
              task.attempts,
              task.max_attempts,
              task.generation,
              t.canonical_slug,
              t.official_url
            FROM asset_tasks task
            JOIN tools t ON t.id = task.tool_id
            WHERE task.source = ?
              AND task.dead_letter_at IS NULL
              AND task.attempts < task.max_attempts
              AND (
                (
                  task.status IN ('queued', 'failed', 'sync_failed')
                  AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                )
                OR (
                  task.status = 'processing'
                  AND task.lease_expires_at IS NOT NULL
                  AND task.lease_expires_at <= ?
                )
              )
            ORDER BY coalesce(task.next_retry_at, ''), task.updated_at
            LIMIT ?
            """,
            [ASSET_SOURCE, now, now, limit],
        )

        claimed: list[AssetTask] = []
        for row in rows:
            tool_id = int(row.get("tool_id") or 0)
            domain = str(row.get("normalized_domain") or "")
            if tool_id <= 0 or not domain:
                continue
            claimed_rows = await self.d1.query(
                """
                UPDATE asset_tasks
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_started_at = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE tool_id = ?
                  AND source = ?
                  AND dead_letter_at IS NULL
                  AND attempts < max_attempts
                  AND (
                    (
                      status IN ('queued', 'failed', 'sync_failed')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    )
                    OR (
                      status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    )
                  )
                RETURNING attempts, max_attempts, generation, lease_token
                """,
                [now, lease_owner, lease_expires_at, now, tool_id, ASSET_SOURCE, now, now],
            )
            if claimed_rows:
                claimed_row = claimed_rows[0]
                claimed.append(
                    AssetTask(
                        tool_id=tool_id,
                        canonical_slug=str(row.get("canonical_slug") or ""),
                        normalized_domain=domain,
                        official_url=str(row.get("official_url") or ""),
                        attempts=int(claimed_row.get("attempts") or 0),
                        max_attempts=int(claimed_row.get("max_attempts") or 5),
                        generation=int(claimed_row.get("generation") or 1),
                        lease_token=str(claimed_row.get("lease_token") or ""),
                    )
                )
        return claimed

    async def upsert_tool_asset(
        self,
        task: AssetTask,
        asset_kind: str,
        storage_object_path: str,
        public_url: str | None,
        mime_type: str,
        width: int | None,
        height: int | None,
    ) -> None:
        rows = await self.d1.query(
            """
            SELECT id
            FROM tool_assets
            WHERE tool_id = ?
              AND asset_kind = ?
              AND coalesce(locale_code, '') = ''
              AND is_current = 1
            LIMIT 1
            """,
            [task.tool_id, asset_kind],
        )
        if rows:
            await self.d1.run(
                """
                UPDATE tool_assets
                SET storage_bucket = ?,
                    storage_object_path = ?,
                    public_url = ?,
                    mime_type = ?,
                    width = ?,
                    height = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                [ASSET_DB_STORAGE_BUCKET, storage_object_path, public_url, mime_type, width, height, rows[0]["id"]],
            )
            return

        await self.d1.run(
            """
            INSERT INTO tool_assets (
              tool_id,
              locale_code,
              asset_kind,
              storage_bucket,
              storage_object_path,
              public_url,
              mime_type,
              width,
              height,
              is_current
            )
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [task.tool_id, asset_kind, ASSET_DB_STORAGE_BUCKET, storage_object_path, public_url, mime_type, width, height],
        )

    async def category_options(self) -> list[str]:
        rows = await self.d1.query(
            """
            SELECT canonical_slug
            FROM categories
            WHERE status = 'active'
            ORDER BY parent_category_id IS NOT NULL, display_order, canonical_slug
            """,
        )
        return [str(row.get("canonical_slug") or "") for row in rows if row.get("canonical_slug")]

    async def save_tool_localization(self, task: AssetTask, result: AssetFetchResult) -> None:
        title = clean_asset_text(result.title, 120)
        description = clean_asset_text(result.description, 500)
        if not title and not description:
            return
        rows = await self.d1.query(
            """
            SELECT locale_code
            FROM tool_localizations
            WHERE tool_id = ?
            ORDER BY CASE WHEN locale_code = 'en' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            [task.tool_id],
        )
        locale = str(rows[0].get("locale_code") or "") if rows else ""
        if not locale:
            locale_rows = await self.d1.query(
                """
                SELECT code
                FROM app_locales
                WHERE code = 'en' OR is_primary = 1
                ORDER BY CASE WHEN code = 'en' THEN 0 ELSE 1 END
                LIMIT 1
                """,
            )
            locale = str(locale_rows[0].get("code") or "") if locale_rows else ""
            if not locale:
                return
            slug_base = public_tool_slug_base(task, title)
            for number in range(1, 1001):
                localized_slug = numbered_public_slug(slug_base, number)
                meta = await self.d1.run(
                    """
                    INSERT OR IGNORE INTO tool_localizations (
                      tool_id, locale_code, localized_slug, name, tagline, short_description, feature_highlights,
                      translation_status, published_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, '[]', 'published', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    [
                        task.tool_id,
                        locale,
                        localized_slug,
                        title or localized_slug,
                        description or None,
                        description or None,
                    ],
                )
                if int(meta.get("changes") or 0) > 0:
                    return
                existing_rows = await self.d1.query(
                    "SELECT localized_slug FROM tool_localizations WHERE tool_id = ? AND locale_code = ? LIMIT 1",
                    [task.tool_id, locale],
                )
                if existing_rows:
                    return
            raise RuntimeError(f"Unable to allocate public slug for tool {task.tool_id}")

        await self.d1.run(
            """
            UPDATE tool_localizations
            SET name = CASE
                  WHEN ? <> '' AND (name IS NULL OR trim(name) = '' OR name = ?)
                  THEN ?
                  ELSE name
                END,
                tagline = CASE
                  WHEN ? <> '' AND (tagline IS NULL OR trim(tagline) = '')
                  THEN ?
                  ELSE tagline
                END,
                short_description = CASE
                  WHEN ? <> '' AND (short_description IS NULL OR trim(short_description) = '')
                  THEN ?
                  ELSE short_description
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE tool_id = ?
              AND locale_code = ?
            """,
            [
                title,
                task.canonical_slug,
                title,
                description,
                description,
                description,
                description,
                task.tool_id,
                locale,
            ],
        )

    async def save_tool_categories(self, task: AssetTask, result: AssetFetchResult) -> None:
        slugs = [slug for slug in (result.category_l2, result.category_l1) if slug]
        slugs = list(dict.fromkeys(slugs))
        if not slugs:
            return
        slug_placeholders = ",".join("?" for _ in slugs)
        rows = await self.d1.query(
            f"""
            SELECT
              c.id,
              c.canonical_slug,
              c.parent_category_id,
              parent.id AS parent_id,
              parent.canonical_slug AS parent_slug
            FROM categories c
            LEFT JOIN categories parent
              ON parent.id = c.parent_category_id
             AND parent.status = 'active'
            WHERE c.status = 'active'
              AND c.canonical_slug IN ({slug_placeholders})
            """,
            slugs,
        )
        by_slug = {str(row.get("canonical_slug")): row for row in rows}
        specific = next((by_slug.get(slug) for slug in slugs if by_slug.get(slug)), None)
        if not specific:
            return
        primary_id = int(specific.get("parent_id") or specific.get("id") or 0)
        specific_id = int(specific.get("id") or 0)
        for category_id in dict.fromkeys([primary_id, specific_id]):
            if category_id > 0:
                await self.d1.run(
                    "INSERT OR IGNORE INTO tool_categories (tool_id, category_id) VALUES (?, ?)",
                    [task.tool_id, category_id],
                )
        if primary_id > 0:
            await self.d1.run(
                """
                UPDATE tools
                SET primary_category_id = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                  AND primary_category_id IS NULL
                """,
                [primary_id, task.tool_id],
            )

    async def save_tool_features(self, task: AssetTask, result: AssetFetchResult) -> None:
        features = clean_key_features(result.key_features)
        if not features:
            return
        await self.d1.run(
            "DELETE FROM tool_key_features WHERE tool_id = ? AND source = ?",
            [task.tool_id, ASSET_SOURCE],
        )
        for position, feature in enumerate(features):
            await self.d1.run(
                """
                INSERT INTO tool_key_features (
                  tool_id, feature_name, feature_description, position, source
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [task.tool_id, feature["name"], feature["description"] or None, position, ASSET_SOURCE],
            )
        feature_highlights = json.dumps(
            [feature["name"] for feature in features],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self.d1.run(
            """
            UPDATE tool_localizations
            SET feature_highlights = CASE
                  WHEN json_array_length(coalesce(feature_highlights, '[]')) = 0
                  THEN ?
                  ELSE feature_highlights
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE tool_id = ?
            """,
            [feature_highlights, task.tool_id],
        )

    async def missing_tool_enrichment_requirements(self, tool_id: int) -> list[str]:
        rows = await self.d1.query(
            """
            SELECT
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND tl.translation_status = 'published'
                  AND tl.published_at IS NOT NULL
                  AND trim(coalesce(tl.short_description, '')) <> ''
              ) THEN 1 ELSE 0 END AS has_description,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_key_features tkf
                WHERE tkf.tool_id = ?
              ) OR EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND tl.translation_status = 'published'
                  AND tl.published_at IS NOT NULL
                  AND json_array_length(coalesce(tl.feature_highlights, '[]')) > 0
              ) THEN 1 ELSE 0 END AS has_features,
              CASE WHEN EXISTS (
                SELECT 1 FROM tools t
                WHERE t.id = ? AND t.primary_category_id IS NOT NULL
              ) OR EXISTS (
                SELECT 1 FROM tool_categories tc WHERE tc.tool_id = ?
              ) THEN 1 ELSE 0 END AS has_category
            """,
            [tool_id, tool_id, tool_id, tool_id, tool_id],
        )
        row = rows[0] if rows else {}
        return [
            name
            for name, column in (
                ("description", "has_description"),
                ("key_features", "has_features"),
                ("category", "has_category"),
            )
            if not int(row.get(column) or 0)
        ]

    async def missing_asset_requirements(self, tool_id: int) -> list[str]:
        rows = await self.d1.query(
            """
            SELECT
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_assets ta
                WHERE ta.tool_id = ?
                  AND ta.asset_kind = 'screenshot'
                  AND ta.storage_bucket = ?
                  AND ta.is_current = 1
              ) THEN 1 ELSE 0 END AS has_screenshot,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_assets ta
                WHERE ta.tool_id = ?
                  AND ta.asset_kind = 'favicon'
                  AND ta.is_current = 1
              ) THEN 1 ELSE 0 END AS has_favicon,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND tl.translation_status = 'published'
                  AND tl.published_at IS NOT NULL
                  AND trim(coalesce(tl.short_description, '')) <> ''
              ) THEN 1 ELSE 0 END AS has_description,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tool_key_features tkf
                WHERE tkf.tool_id = ?
              ) OR EXISTS (
                SELECT 1
                FROM tool_localizations tl
                WHERE tl.tool_id = ?
                  AND tl.translation_status = 'published'
                  AND tl.published_at IS NOT NULL
                  AND json_array_length(coalesce(tl.feature_highlights, '[]')) > 0
              ) THEN 1 ELSE 0 END AS has_key_features,
              CASE WHEN EXISTS (
                SELECT 1
                FROM tools t
                WHERE t.id = ?
                  AND t.primary_category_id IS NOT NULL
              ) OR EXISTS (
                SELECT 1
                FROM tool_categories tc
                WHERE tc.tool_id = ?
              ) THEN 1 ELSE 0 END AS has_category
            """,
            [
                tool_id,
                ASSET_DB_STORAGE_BUCKET,
                tool_id,
                tool_id,
                tool_id,
                tool_id,
                tool_id,
                tool_id,
            ],
        )
        row = rows[0] if rows else {}
        columns = {
            "screenshot": "has_screenshot",
            "favicon": "has_favicon",
            "description": "has_description",
            "key_features": "has_key_features",
            "category": "has_category",
        }
        return [
            requirement
            for requirement in ASSET_REQUIREMENT_ORDER
            if not int(row.get(columns[requirement]) or 0)
        ]

    async def has_tool_enrichment(self, tool_id: int) -> bool:
        return not await self.missing_tool_enrichment_requirements(tool_id)

    async def save_tool_enrichment(self, task: AssetTask, result: AssetFetchResult) -> None:
        await self.save_tool_localization(task, result)
        await self.save_tool_categories(task, result)
        await self.save_tool_features(task, result)

    async def renew_lease(self, task: AssetTask) -> bool:
        meta = await self.d1.run(
            """
            UPDATE asset_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE tool_id = ? AND source = ? AND status = 'processing'
              AND generation = ? AND lease_token = ?
            """,
            [iso_delta(hours=1), utc_now_iso(), task.tool_id, ASSET_SOURCE, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(
        self,
        task: AssetTask,
        status: str,
        error: str | None = None,
        *,
        retryable: bool = True,
    ) -> bool:
        now = utc_now_iso()
        exhausted = task.attempts >= task.max_attempts or not retryable
        meta = await self.d1.run(
            """
            UPDATE asset_tasks
            SET status = ?,
                last_fetched_at = ?,
                next_retry_at = ?,
                last_error = ?,
                dead_letter_at = ?,
                last_completed_at = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE tool_id = ?
              AND source = ?
              AND status = 'processing'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                status,
                now,
                None if status == "done" or exhausted else iso_delta(days=1),
                (error or "")[:2000] or None,
                now if status != "done" and exhausted else None,
                now,
                now,
                task.tool_id,
                ASSET_SOURCE,
                task.generation,
                task.lease_token,
            ],
        )
        return int(meta.get("changes") or 0) > 0


def traffic_release_probe_window_open(config: Config, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    return current.day >= config.traffic_release_probe_start_day


class D1TrafficReleaseStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def check_or_probe(
        self,
        traffic_month: str,
        probe_domain: str,
        probe_interval_seconds: int,
        client: SimilarWebClient,
    ) -> TrafficReleaseGateResult:
        now = utc_now_iso()
        rows = await self.d1.query(
            """
            SELECT status, observed_latest_month, next_check_at
            FROM traffic_month_release_checks
            WHERE source = ? AND traffic_month = ?
            LIMIT 1
            """,
            [TRAFFIC_SOURCE, traffic_month],
        )
        existing = rows[0] if rows else None
        if existing and existing.get("status") == "available":
            return TrafficReleaseGateResult(
                available=True,
                status="available",
                probe_attempted=False,
                observed_latest_month=existing.get("observed_latest_month"),
            )

        next_check_at = str(existing.get("next_check_at") or "") if existing else ""
        if next_check_at and next_check_at > now:
            return TrafficReleaseGateResult(
                available=False,
                status=str(existing.get("status") or "unavailable"),
                probe_attempted=False,
                observed_latest_month=existing.get("observed_latest_month"),
            )

        result = await client.fetch(probe_domain, traffic_month)
        available = result.status == "done" and requested_month_has_traffic_data(result.monthly_rows, traffic_month)
        status = "available" if available else ("unavailable" if result.status == "no_data" else "error")
        next_check = None if available else iso_delta(seconds=probe_interval_seconds)
        error = None if available else (result.error or f"probe_status:{result.status}")
        response_meta = json.dumps(
            {
                "probe_status": result.status,
                "monthly_rows": len(result.monthly_rows),
                "requested_month_present": requested_month_has_traffic_data(result.monthly_rows, traffic_month),
            },
            sort_keys=True,
        )
        await self.d1.run(
            """
            INSERT INTO traffic_month_release_checks (
              source, traffic_month, status, probe_domain, observed_latest_month,
              attempts, last_checked_at, next_check_at, available_at, last_error,
              response_meta_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, traffic_month) DO UPDATE SET
              status = excluded.status,
              probe_domain = excluded.probe_domain,
              observed_latest_month = excluded.observed_latest_month,
              attempts = traffic_month_release_checks.attempts + 1,
              last_checked_at = excluded.last_checked_at,
              next_check_at = excluded.next_check_at,
              available_at = CASE
                WHEN excluded.status = 'available' THEN coalesce(traffic_month_release_checks.available_at, excluded.available_at)
                ELSE traffic_month_release_checks.available_at
              END,
              last_error = excluded.last_error,
              response_meta_json = excluded.response_meta_json,
              updated_at = excluded.updated_at
            """,
            [
                TRAFFIC_SOURCE,
                traffic_month,
                status,
                probe_domain,
                result.observed_latest_month,
                now,
                next_check,
                now if available else None,
                error[:2000] if error else None,
                response_meta,
                now,
            ],
        )
        log_info(
            "traffic_release_gate.probe",
            traffic_month=traffic_month,
            probe_domain=probe_domain,
            status=status,
            observed_latest_month=result.observed_latest_month,
            next_check_at=next_check,
        )
        return TrafficReleaseGateResult(
            available=available,
            status=status,
            probe_attempted=True,
            observed_latest_month=result.observed_latest_month,
        )


class D1TaskStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def queue_missing_traffic_tasks(self, limit: int, traffic_month: str) -> int:
        now = utc_now_iso()
        candidates = await self.d1.query(
            """
            SELECT t.normalized_domain
            FROM tools t
            LEFT JOIN domain_traffic_monthly tm
              ON tm.normalized_domain = t.normalized_domain
             AND tm.source = ?
             AND tm.traffic_month = ?
            LEFT JOIN traffic_tasks task
              ON task.normalized_domain = t.normalized_domain
             AND task.source = ?
             AND task.traffic_month = ?
            WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
              AND t.duplicate_of_tool_id IS NULL
              AND trim(t.normalized_domain) <> ''
              AND tm.traffic_month IS NULL
              AND (
                task.normalized_domain IS NULL
                OR task.status = 'done'
                OR (
                  task.status IN ('failed', 'sync_failed')
                  AND task.dead_letter_at IS NULL
                  AND task.attempts < task.max_attempts
                  AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                )
                OR (task.status = 'queued' AND task.dead_letter_at IS NULL)
                OR (
                  task.status = 'processing'
                  AND task.dead_letter_at IS NULL
                  AND task.lease_expires_at IS NOT NULL
                  AND task.lease_expires_at <= ?
                )
              )
            GROUP BY t.normalized_domain
            ORDER BY min(coalesce(task.updated_at, '')) ASC, min(t.id) ASC
            LIMIT ?
            """,
            [TRAFFIC_SOURCE, traffic_month, TRAFFIC_SOURCE, traffic_month, now, now, limit],
        )

        queued = 0
        for candidate in candidates:
            domain = str(candidate.get("normalized_domain") or "")
            if not domain:
                continue
            meta = await self.d1.run(
                """
                INSERT INTO traffic_tasks (
                  normalized_domain, source, traffic_month, status, last_queued_at, next_retry_at, last_error
                )
                VALUES (?, ?, ?, 'queued', ?, NULL, NULL)
                ON CONFLICT (normalized_domain, source, traffic_month) DO UPDATE SET
                  status = 'queued',
                  attempts = 0,
                  generation = traffic_tasks.generation + 1,
                  last_queued_at = excluded.last_queued_at,
                  last_started_at = NULL,
                  last_fetched_at = NULL,
                  next_retry_at = NULL,
                  last_error = 'Requeued because the monthly materialization is missing',
                  lease_owner = NULL,
                  lease_token = NULL,
                  lease_expires_at = NULL,
                  dead_letter_at = NULL,
                  updated_at = excluded.last_queued_at
                WHERE traffic_tasks.status = 'done'
                """,
                [domain, TRAFFIC_SOURCE, traffic_month, now],
            )
            queued += int(meta.get("changes") or 0)

        return queued

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[TrafficTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(hours=1)
        rows = await self.d1.query(
            """
            SELECT normalized_domain, source, traffic_month, attempts, max_attempts, generation
            FROM traffic_tasks
            WHERE source = ?
              AND dead_letter_at IS NULL
              AND attempts < max_attempts
              AND (
                (
                  status IN ('queued', 'failed', 'sync_failed')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                )
                OR (
                  status = 'processing'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                )
              )
            ORDER BY coalesce(next_retry_at, ''), updated_at
            LIMIT ?
            """,
            [TRAFFIC_SOURCE, now, now, limit],
        )

        claimed: list[TrafficTask] = []
        for row in rows:
            domain = str(row.get("normalized_domain") or "")
            traffic_month = str(row.get("traffic_month") or "")
            if not domain or not traffic_month:
                continue

            claimed_rows = await self.d1.query(
                """
                UPDATE traffic_tasks
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_started_at = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE normalized_domain = ?
                  AND source = ?
                  AND traffic_month = ?
                  AND dead_letter_at IS NULL
                  AND attempts < max_attempts
                  AND (
                    (
                      status IN ('queued', 'failed', 'sync_failed')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    )
                    OR (
                      status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    )
                  )
                RETURNING attempts, max_attempts, generation, lease_token
                """,
                [
                    now,
                    lease_owner,
                    lease_expires_at,
                    now,
                    domain,
                    TRAFFIC_SOURCE,
                    traffic_month,
                    now,
                    now,
                ],
            )
            if claimed_rows:
                claimed_row = claimed_rows[0]
                claimed.append(
                    TrafficTask(
                        normalized_domain=domain,
                        traffic_month=traffic_month,
                        attempts=int(claimed_row.get("attempts") or 0),
                        max_attempts=int(claimed_row.get("max_attempts") or 5),
                        generation=int(claimed_row.get("generation") or 1),
                        lease_token=str(claimed_row.get("lease_token") or ""),
                    )
                )

        return claimed

    async def renew_lease(self, task: TrafficTask) -> bool:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE traffic_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE normalized_domain = ? AND source = ? AND traffic_month = ?
              AND status = 'processing' AND generation = ? AND lease_token = ?
            """,
            [iso_delta(hours=1), now, task.normalized_domain, TRAFFIC_SOURCE, task.traffic_month, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(self, task: TrafficTask, result: FetchResult) -> bool:
        retry_days = 1 if result.status == "failed" else None
        now = utc_now_iso()
        exhausted = task.attempts >= task.max_attempts
        log_info(
            "d1.complete_task.start",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
        )
        meta = await self.d1.run(
            """
            UPDATE traffic_tasks
            SET status = ?,
                last_fetched_at = ?,
                next_retry_at = ?,
                last_error = ?,
                dead_letter_at = ?,
                last_completed_at = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE normalized_domain = ?
              AND source = ?
              AND traffic_month = ?
              AND status = 'processing'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                result.status,
                now,
                iso_delta(days=retry_days) if retry_days is not None and not exhausted else None,
                (result.error or "")[:2000] or None,
                now if result.status == "failed" and exhausted else None,
                now,
                now,
                task.normalized_domain,
                TRAFFIC_SOURCE,
                task.traffic_month,
                task.generation,
                task.lease_token,
            ],
        )
        if int(meta.get("changes") or 0) == 0:
            log_info("d1.complete_task.stale", domain=task.normalized_domain, traffic_month=task.traffic_month)
            return False
        await self.update_tool_status(task.normalized_domain, result)
        log_info(
            "d1.complete_task.done",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            status=result.status,
        )
        return True

    async def update_tool_status(self, domain: str, result: FetchResult) -> None:
        retry_days = 30
        if result.status in ("no_data", "forbidden"):
            retry_days = 7
        if result.status == "failed":
            retry_days = 1

        now = utc_now_iso()
        await self.d1.run(
            """
            INSERT INTO tool_traffic_fetch_status (
              tool_id, normalized_domain, source, last_checked_at, last_status, last_error, next_retry_at
            )
            SELECT
              id,
              normalized_domain,
              ?,
              ?,
              ?,
              ?,
              ?
            FROM tools
            WHERE normalized_domain = ?
              AND status IN ('published', 'pending_enrich', 'pending_review')
              AND duplicate_of_tool_id IS NULL
            ON CONFLICT (tool_id, source) DO UPDATE
            SET normalized_domain = excluded.normalized_domain,
                last_checked_at = excluded.last_checked_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                next_retry_at = excluded.next_retry_at,
                updated_at = ?
            """,
            [
                TRAFFIC_SOURCE,
                now,
                result.status,
                (result.error or "")[:2000] or None,
                iso_delta(days=retry_days),
                domain,
                now,
            ],
        )


class D1DomainStateStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def queue_due_tasks(self, limit: int, max_age_days: int) -> int:
        stale_before = iso_delta(days=-max_age_days)
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            INSERT INTO domain_state_tasks (
              normalized_domain, source, status, attempts, last_queued_at,
              next_retry_at, last_error, created_at, updated_at
            )
            SELECT due.normalized_domain, ?, 'queued', 0, ?, NULL, NULL, ?, ?
            FROM (
              SELECT t.normalized_domain
              FROM tools t
              LEFT JOIN domain_states ds
                ON ds.normalized_domain = t.normalized_domain
               AND ds.source = ?
              WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
                AND t.duplicate_of_tool_id IS NULL
                AND trim(t.normalized_domain) <> ''
                AND (
                  ds.last_crawled_at IS NULL
                  OR ds.last_crawled_at < ?
                )
              GROUP BY t.normalized_domain
              ORDER BY CASE WHEN min(ds.last_crawled_at) IS NULL THEN 0 ELSE 1 END,
                       min(ds.last_crawled_at) ASC,
                       t.normalized_domain ASC
              LIMIT ?
            ) AS due
            WHERE 1 = 1
            ON CONFLICT(normalized_domain, source) DO UPDATE SET
              status = 'queued',
              attempts = 0,
              generation = domain_state_tasks.generation + 1,
              last_queued_at = excluded.last_queued_at,
              next_retry_at = NULL,
              last_error = NULL,
              lease_owner = NULL,
              lease_token = NULL,
              lease_expires_at = NULL,
              dead_letter_at = NULL,
              updated_at = excluded.updated_at
            WHERE domain_state_tasks.status IN ('done', 'no_data')
            """,
            [
                DOMAIN_STATE_SOURCE,
                now,
                now,
                now,
                DOMAIN_STATE_SOURCE,
                stale_before,
                limit,
            ],
        )
        return int(meta.get("changes") or 0)

    async def claim_due_tasks(self, limit: int, lease_owner: str) -> list[DomainStateTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(minutes=15)
        rows = await self.d1.query(
            """
            UPDATE domain_state_tasks
            SET status = 'processing',
                attempts = attempts + 1,
                last_started_at = ?,
                next_retry_at = NULL,
                last_error = NULL,
                lease_owner = ?,
                lease_token = lower(hex(randomblob(16))),
                lease_expires_at = ?,
                updated_at = ?
            WHERE rowid IN (
              SELECT rowid
              FROM domain_state_tasks
              WHERE source = ?
                AND dead_letter_at IS NULL
                AND attempts < max_attempts
                AND (
                  (
                    status IN ('queued', 'failed', 'sync_failed')
                    AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  )
                  OR (
                    status = 'processing'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                  )
                )
              ORDER BY coalesce(next_retry_at, ''), updated_at, normalized_domain
              LIMIT ?
            )
            RETURNING normalized_domain, attempts, max_attempts, generation, lease_token
            """,
            [now, lease_owner, lease_expires_at, now, DOMAIN_STATE_SOURCE, now, now, limit],
        )
        return [
            DomainStateTask(
                normalized_domain=str(row.get("normalized_domain") or ""),
                attempts=int(row.get("attempts") or 0),
                max_attempts=int(row.get("max_attempts") or 5),
                generation=int(row.get("generation") or 1),
                lease_token=str(row.get("lease_token") or ""),
            )
            for row in rows
            if row.get("normalized_domain") and row.get("lease_token")
        ]

    async def renew_lease(self, task: DomainStateTask) -> bool:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE domain_state_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE normalized_domain = ? AND source = ? AND status = 'processing'
              AND generation = ? AND lease_token = ?
            """,
            [iso_delta(minutes=15), now, task.normalized_domain, DOMAIN_STATE_SOURCE, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def complete_task(self, task: DomainStateTask, result: DomainStateResult) -> bool:
        now = utc_now_iso()
        if result.status in ("done", "no_data"):
            statements: list[tuple[str, list[Any]]] = []
            if result.domain_rating is not None:
                statements.append(
                    (
                        """
                        INSERT INTO domain_rating_history (
                          normalized_domain, source, observed_date, domain_rating, observed_at
                        )
                        SELECT ?, ?, substr(?, 1, 10), ?, ?
                        WHERE EXISTS (
                          SELECT 1 FROM domain_state_tasks
                          WHERE normalized_domain = ? AND source = ?
                            AND status = 'processing' AND generation = ? AND lease_token = ?
                        )
                        ON CONFLICT (normalized_domain, source, observed_date) DO UPDATE SET
                          domain_rating = excluded.domain_rating,
                          observed_at = excluded.observed_at,
                          updated_at = excluded.observed_at
                        """,
                        [
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            now,
                            result.domain_rating,
                            now,
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            task.generation,
                            task.lease_token,
                        ],
                    )
                )
            statements.extend(
                [
                    (
                        """
                        INSERT INTO domain_states (
                          normalized_domain, source, domain_rating, last_crawled_at, domain_created_at
                        )
                        SELECT ?, ?, ?, ?, ?
                        WHERE EXISTS (
                          SELECT 1 FROM domain_state_tasks
                          WHERE normalized_domain = ? AND source = ?
                            AND status = 'processing' AND generation = ? AND lease_token = ?
                        )
                        ON CONFLICT (normalized_domain, source) DO UPDATE SET
                          domain_rating = coalesce(excluded.domain_rating, domain_states.domain_rating),
                          last_crawled_at = excluded.last_crawled_at,
                          domain_created_at = coalesce(excluded.domain_created_at, domain_states.domain_created_at),
                          updated_at = ?
                        """,
                        [
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            result.domain_rating,
                            now,
                            result.domain_created_at,
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            task.generation,
                            task.lease_token,
                            now,
                        ],
                    ),
                    (
                        """
                        UPDATE domain_state_tasks
                        SET status = ?, last_fetched_at = ?, last_completed_at = ?,
                            next_retry_at = NULL, last_error = NULL,
                            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                            updated_at = ?
                        WHERE normalized_domain = ? AND source = ?
                          AND status = 'processing' AND generation = ? AND lease_token = ?
                        """,
                        [
                            result.status,
                            now,
                            now,
                            now,
                            task.normalized_domain,
                            DOMAIN_STATE_SOURCE,
                            task.generation,
                            task.lease_token,
                        ],
                    ),
                ]
            )
            batch_results = await self.d1.batch(statements)
            task_meta = batch_results[-1].get("meta") if batch_results else {}
            return int((task_meta or {}).get("changes") or 0) > 0

        exhausted = task.attempts >= task.max_attempts
        retry_minutes = min(60, 2 ** min(task.attempts, 6))
        meta = await self.d1.run(
            """
            UPDATE domain_state_tasks
            SET status = 'failed',
                next_retry_at = ?,
                last_error = ?,
                dead_letter_at = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE normalized_domain = ? AND source = ?
              AND status = 'processing' AND generation = ? AND lease_token = ?
            """,
            [
                None if exhausted else iso_delta(minutes=retry_minutes),
                (result.error or "Domain state fetch failed")[:2000],
                now if exhausted else None,
                now,
                task.normalized_domain,
                DOMAIN_STATE_SOURCE,
                task.generation,
                task.lease_token,
            ],
        )
        return int(meta.get("changes") or 0) > 0


class D1PricingStore:
    def __init__(self, d1: D1Client):
        self.d1 = d1

    async def missing_source_candidates(self, limit: int) -> list[PricingSourceCandidate]:
        now = utc_now_iso()
        rows = await self.d1.query(
            """
            SELECT
              t.id AS tool_id,
              t.canonical_slug,
              t.official_url
            FROM tools t
            WHERE t.status IN ('published', 'pending_enrich', 'pending_review')
              AND t.duplicate_of_tool_id IS NULL
              AND t.official_url IS NOT NULL
              AND trim(t.official_url) <> ''
              AND NOT EXISTS (
                SELECT 1 FROM pricing_sources active_source
                WHERE active_source.tool_id = t.id AND active_source.is_active = 1
              )
              AND (
                NOT EXISTS (SELECT 1 FROM pricing_sources any_source WHERE any_source.tool_id = t.id)
                OR EXISTS (
                  SELECT 1 FROM pricing_sources retry_source
                  WHERE retry_source.tool_id = t.id
                    AND retry_source.is_active = 0
                    AND retry_source.discovery_status IN ('retryable', 'not_found')
                    AND retry_source.discovery_attempts < retry_source.discovery_max_attempts
                    AND retry_source.next_discovery_at IS NOT NULL
                    AND retry_source.next_discovery_at <= ?
                )
              )
            ORDER BY t.id
            LIMIT ?
            """,
            [now, limit],
        )
        return [
            PricingSourceCandidate(
                tool_id=int(row["tool_id"]),
                canonical_slug=str(row.get("canonical_slug") or ""),
                official_url=str(row.get("official_url") or ""),
            )
            for row in rows
        ]

    async def insert_pricing_source(
        self,
        tool_id: int,
        url: str,
        discovery_method: str,
        source_confidence: int,
    ) -> None:
        now = utc_now_iso()
        await self.d1.run(
            """
            INSERT INTO pricing_sources (
              tool_id,
              url,
              source_type,
              scope,
              locale,
              region,
              expected_currency,
              fetch_mode,
              discovery_method,
              source_confidence,
              is_active,
              unchanged_runs,
              next_run_at,
              last_error,
              discovery_status,
              discovery_attempts,
              last_discovery_at,
              next_discovery_at,
              created_at,
              updated_at
            )
            VALUES (?, ?, 'marketing_pricing', 'individual', 'en-US', 'US', 'USD', 'static', ?, ?, 1, 0, NULL, NULL, 'found', 0, ?, NULL, ?, ?)
            ON CONFLICT (tool_id, url, locale, region, scope) DO UPDATE SET
              is_active = 1,
              discovery_method = excluded.discovery_method,
              source_confidence = excluded.source_confidence,
              next_run_at = NULL,
              last_error = NULL,
              discovery_status = 'found',
              discovery_attempts = 0,
              last_discovery_at = excluded.last_discovery_at,
              next_discovery_at = NULL,
              updated_at = ?
            """,
            [tool_id, url, discovery_method, source_confidence, now, now, now, now],
        )

    async def mark_pricing_source_discovery_skipped(
        self,
        tool_id: int,
        url: str,
        error: str,
        retryable: bool,
    ) -> None:
        clean_url = (url or "").strip()
        if not clean_url:
            return
        now = utc_now_iso()
        discovery_status = "retryable" if retryable else "not_found"
        next_discovery_at = iso_delta(days=1 if retryable else 30)
        await self.d1.run(
            """
            INSERT INTO pricing_sources (
              tool_id,
              url,
              source_type,
              scope,
              locale,
              region,
              expected_currency,
              fetch_mode,
              discovery_method,
              source_confidence,
              is_active,
              unchanged_runs,
              next_run_at,
              last_error,
              discovery_status,
              discovery_attempts,
              last_discovery_at,
              next_discovery_at,
              created_at,
              updated_at
            )
            VALUES (?, ?, 'marketing_pricing', 'individual', 'en-US', 'US', 'USD', 'static', 'homepage_link', 0, 0, 0, NULL, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT (tool_id, url, locale, region, scope) DO UPDATE SET
              is_active = 0,
              source_confidence = 0,
              next_run_at = NULL,
              last_error = excluded.last_error,
              discovery_attempts = pricing_sources.discovery_attempts + 1,
              discovery_status = CASE
                WHEN pricing_sources.discovery_attempts + 1 >= pricing_sources.discovery_max_attempts
                THEN 'exhausted'
                ELSE excluded.discovery_status
              END,
              last_discovery_at = excluded.last_discovery_at,
              next_discovery_at = CASE
                WHEN pricing_sources.discovery_attempts + 1 >= pricing_sources.discovery_max_attempts
                THEN NULL
                ELSE excluded.next_discovery_at
              END,
              updated_at = ?
            """,
            [
                tool_id,
                clean_url,
                error[:2000],
                discovery_status,
                now,
                next_discovery_at,
                now,
                now,
                now,
            ],
        )

    async def queue_due_tasks(self, limit: int) -> int:
        now = utc_now_iso()
        rows = await self.d1.query(
            """
            WITH latest_task AS (
              SELECT pricing_source_id, max(id) AS task_id
              FROM pricing_tasks
              GROUP BY pricing_source_id
            )
            SELECT
              ps.id AS pricing_source_id,
              ps.tool_id
            FROM pricing_sources ps
            JOIN tools t ON t.id = ps.tool_id
            LEFT JOIN latest_task lt ON lt.pricing_source_id = ps.id
            LEFT JOIN pricing_tasks task ON task.id = lt.task_id
            WHERE ps.is_active = 1
              AND t.status IN ('published', 'pending_enrich', 'pending_review')
              AND t.duplicate_of_tool_id IS NULL
              AND (ps.next_run_at IS NULL OR ps.next_run_at <= ?)
              AND (
                task.id IS NULL
                OR task.status = 'succeeded'
              )
            ORDER BY coalesce(ps.next_run_at, ''), ps.id
            LIMIT ?
            """,
            [now, limit],
        )

        queued = 0
        for row in rows:
            source_id = int(row.get("pricing_source_id") or 0)
            tool_id = int(row.get("tool_id") or 0)
            if source_id <= 0 or tool_id <= 0:
                continue
            await self.d1.run(
                """
                INSERT INTO pricing_tasks (
                  pricing_source_id,
                  tool_id,
                  status,
                  priority,
                  run_after,
                  attempts,
                  max_attempts,
                  last_error
                )
                VALUES (?, ?, 'queued', 0, ?, 0, 3, NULL)
                """,
                [source_id, tool_id, now],
            )
            queued += 1
        return queued

    async def claim_due_tasks(
        self,
        limit: int,
        task_ids: list[int] | None = None,
        claim: bool = True,
        lease_owner: str = "tool-data-runner",
    ) -> list[PricingTask]:
        now = utc_now_iso()
        lease_expires_at = iso_delta(hours=1)
        task_ids = task_ids or []
        params: list[Any]
        if task_ids:
            placeholders = ", ".join("?" for _ in task_ids)
            where = f"task.id IN ({placeholders}) AND task.status IN ('queued', 'manual_review', 'failed')"
            params = [*task_ids, limit]
        else:
            where = """
              task.dead_letter_at IS NULL
              AND task.attempts < task.max_attempts
              AND (
                (task.status IN ('queued', 'failed') AND task.run_after <= ?)
                OR (
                  task.status = 'running'
                  AND task.lease_expires_at IS NOT NULL
                  AND task.lease_expires_at <= ?
                )
              )
            """
            params = [now, now, limit]

        rows = await self.d1.query(
            f"""
            SELECT
              task.id AS task_id,
              task.pricing_source_id,
              task.tool_id,
              task.attempts,
              task.max_attempts,
              task.generation,
              t.canonical_slug,
              t.official_url,
              ps.url AS source_url
            FROM pricing_tasks task
            JOIN pricing_sources ps ON ps.id = task.pricing_source_id
            JOIN tools t ON t.id = task.tool_id
            WHERE {where}
            ORDER BY task.priority DESC, task.id ASC
            LIMIT ?
            """,
            params,
        )

        tasks: list[PricingTask] = []
        for row in rows:
            task = PricingTask(
                task_id=int(row["task_id"]),
                pricing_source_id=int(row["pricing_source_id"]),
                tool_id=int(row["tool_id"]),
                canonical_slug=str(row.get("canonical_slug") or ""),
                source_url=str(row.get("source_url") or ""),
                official_url=str(row.get("official_url") or ""),
                attempts=int(row.get("attempts") or 0) + (1 if claim else 0),
                max_attempts=int(row.get("max_attempts") or 3),
                generation=int(row.get("generation") or 1),
                lease_token="",
            )
            if not claim:
                tasks.append(task)
                continue

            claimed_rows = await self.d1.query(
                """
                UPDATE pricing_tasks
                SET status = 'running',
                    attempts = attempts + 1,
                    started_at = ?,
                    finished_at = NULL,
                    last_error = NULL,
                    lease_owner = ?,
                    lease_token = lower(hex(randomblob(16))),
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND dead_letter_at IS NULL
                  AND attempts < max_attempts
                  AND (
                    (? = 1 AND status IN ('queued', 'manual_review', 'failed'))
                    OR (status IN ('queued', 'failed') AND run_after <= ?)
                    OR (
                      status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    )
                  )
                RETURNING attempts, max_attempts, generation, lease_token
                """,
                [now, lease_owner, lease_expires_at, now, task.task_id, 1 if task_ids else 0, now, now],
            )
            if claimed_rows:
                claimed_row = claimed_rows[0]
                tasks.append(
                    PricingTask(
                        task_id=task.task_id,
                        pricing_source_id=task.pricing_source_id,
                        tool_id=task.tool_id,
                        canonical_slug=task.canonical_slug,
                        source_url=task.source_url,
                        official_url=task.official_url,
                        attempts=int(claimed_row.get("attempts") or 0),
                        max_attempts=int(claimed_row.get("max_attempts") or task.max_attempts),
                        generation=int(claimed_row.get("generation") or task.generation),
                        lease_token=str(claimed_row.get("lease_token") or ""),
                    )
                )
        return tasks

    async def renew_lease(self, task: PricingTask) -> bool:
        now = utc_now_iso()
        meta = await self.d1.run(
            """
            UPDATE pricing_tasks
            SET lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND generation = ? AND lease_token = ?
            """,
            [iso_delta(hours=1), now, task.task_id, task.generation, task.lease_token],
        )
        return int(meta.get("changes") or 0) > 0

    async def insert_snapshot(self, task: PricingTask, result: PricingFetchResult) -> int:
        text = parse_pricing_html(result.html).text if result.html else ""
        raw_hash = sha256_text(result.html or f"{result.status}:{result.final_url}")
        text_hash = sha256_text(text)
        meta = await self.d1.run(
            """
            INSERT INTO pricing_snapshots (
              pricing_source_id,
              pricing_task_id,
              final_url,
              http_status,
              content_type,
              raw_hash,
              semantic_hash,
              fetch_mode,
              error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'static', ?)
            """,
            [
                task.pricing_source_id,
                task.task_id,
                result.final_url or result.url,
                result.status or None,
                result.content_type or None,
                raw_hash,
                text_hash,
                result.error or None,
            ],
        )
        return int(meta.get("last_row_id") or 0)

    async def insert_extraction(
        self,
        snapshot_id: int,
        payload: dict[str, Any],
        review_status: str,
        confidence: int,
        validation_errors: list[str],
        extractor_version: str = PRICING_EXTRACTOR_VERSION,
        model_name: str | None = None,
    ) -> int:
        meta = await self.d1.run(
            """
            INSERT INTO pricing_extractions (
              snapshot_id,
              schema_version,
              extractor_version,
              model_name,
              raw_extraction_json,
              confidence_score,
              validation_errors,
              review_status
            )
            VALUES (?, 'v1', ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_id,
                extractor_version,
                model_name,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                confidence,
                json.dumps(validation_errors, ensure_ascii=False, separators=(",", ":")),
                review_status,
            ],
        )
        return int(meta.get("last_row_id") or 0)

    async def save_catalog(self, task: PricingTask, result: PricingFetchResult, plans: list[dict[str, Any]]) -> int:
        now = utc_now_iso()
        context_hash = sha256_text(f"{task.pricing_source_id}:{result.final_url or result.url}")
        version_hash = sha256_text(json.dumps(plans, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        await self.d1.run(
            """
            INSERT INTO pricing_catalog_versions (
              tool_id,
              pricing_source_id,
              context_hash,
              version_hash,
              first_observed_at,
              last_observed_at,
              status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT (pricing_source_id, context_hash, version_hash) DO UPDATE SET
              last_observed_at = excluded.last_observed_at,
              superseded_at = NULL,
              status = 'active'
            """,
            [task.tool_id, task.pricing_source_id, context_hash, version_hash, now, now],
        )
        rows = await self.d1.query(
            """
            SELECT id
            FROM pricing_catalog_versions
            WHERE pricing_source_id = ?
              AND context_hash = ?
              AND version_hash = ?
            LIMIT 1
            """,
            [task.pricing_source_id, context_hash, version_hash],
        )
        if not rows:
            raise RuntimeError("Unable to resolve pricing catalog version.")
        version_id = int(rows[0]["id"])

        await self.d1.run(
            """
            UPDATE pricing_catalog_versions
            SET status = 'superseded',
                superseded_at = ?
            WHERE pricing_source_id = ?
              AND status = 'active'
              AND id <> ?
            """,
            [now, task.pricing_source_id, version_id],
        )
        await self.d1.run(
            """
            DELETE FROM plan_features
            WHERE pricing_plan_id IN (
              SELECT id FROM pricing_plans WHERE pricing_version_id = ?
            )
            """,
            [version_id],
        )
        await self.d1.run(
            """
            DELETE FROM plan_prices
            WHERE pricing_plan_id IN (
              SELECT id FROM pricing_plans WHERE pricing_version_id = ?
            )
            """,
            [version_id],
        )
        await self.d1.run("DELETE FROM pricing_plans WHERE pricing_version_id = ?", [version_id])

        for index, plan in enumerate(plans):
            plan_meta = await self.d1.run(
                """
                INSERT INTO pricing_plans (
                  pricing_version_id,
                  source_plan_key,
                  name,
                  description,
                  audience,
                  is_enterprise,
                  display_order
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    version_id,
                    plan.get("source_plan_key"),
                    plan.get("name"),
                    plan.get("description"),
                    plan.get("audience"),
                    1 if plan.get("is_enterprise") else 0,
                    index,
                ],
            )
            plan_id = int(plan_meta.get("last_row_id") or 0)
            for price in list(plan.get("prices") or [])[:1]:
                await self.d1.run(
                    """
                    INSERT INTO plan_prices (
                      pricing_plan_id,
                      kind,
                      amount,
                      currency,
                      billing_interval,
                      commitment_interval,
                      unit,
                      starting_at,
                      custom_quote,
                      display_text,
                      derived
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    [
                        plan_id,
                        price.get("kind") or "recurring",
                        price.get("amount"),
                        price.get("currency"),
                        price.get("billing_interval"),
                        price.get("commitment_interval"),
                        price.get("unit"),
                        1 if price.get("starting_at") else 0,
                        1 if price.get("custom_quote") else 0,
                        price.get("display_text"),
                    ],
                )
        return version_id

    async def update_summary(self, task: PricingTask, plans: list[dict[str, Any]]) -> None:
        summary = derive_tool_pricing_summary(plans)
        await self.d1.run(
            """
            UPDATE tools
            SET pricing_model = ?,
                has_free_plan = ?,
                pricing_interval = ?,
                pricing_currency_code = ?,
                starting_price_minor = ?,
                starting_price_usd_minor = ?,
                updated_at = ?
            WHERE id = ?
            """,
            [
                summary["pricing_model"],
                summary["has_free_plan"],
                summary["pricing_interval"],
                summary["pricing_currency_code"],
                summary["starting_price_minor"],
                summary["starting_price_usd_minor"],
                utc_now_iso(),
                task.tool_id,
            ],
        )

    async def finish_task(
        self,
        task: PricingTask,
        status: str,
        error: str | None,
        result: PricingFetchResult | None,
    ) -> bool:
        now = utc_now_iso()
        exhausted = task.attempts >= task.max_attempts
        meta = await self.d1.run(
            """
            UPDATE pricing_tasks
            SET status = ?,
                last_error = ?,
                run_after = ?,
                finished_at = ?,
                dead_letter_at = ?,
                last_completed_at = ?,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE id = ?
              AND status = 'running'
              AND generation = ?
              AND lease_token = ?
            """,
            [
                status,
                (error or "")[:2000] or None,
                iso_delta(hours=6) if status == "failed" and not exhausted else now,
                now,
                now if status not in ("succeeded", "manual_review") and exhausted else None,
                now,
                now,
                task.task_id,
                task.generation,
                task.lease_token,
            ],
        )
        if int(meta.get("changes") or 0) == 0:
            log_info("pricing_task.stale_completion_ignored", task_id=task.task_id)
            return False
        if status == "succeeded":
            await self.d1.run(
                """
                UPDATE pricing_sources
                SET last_success_at = ?,
                    last_content_hash = ?,
                    unchanged_runs = 0,
                    next_run_at = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                [
                    now,
                    sha256_text(result.html) if result else None,
                    iso_delta(days=30),
                    now,
                    task.pricing_source_id,
                ],
            )
            return True

        await self.d1.run(
            """
            UPDATE pricing_sources
            SET last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            [(error or "")[:2000] or None, now, task.pricing_source_id],
        )
        return True

    async def claim_reviewed_extractions(self, limit: int = 20) -> list[ReviewedPricingExtraction]:
        stale_before = iso_delta(hours=-1)
        rows = await self.d1.query(
            """
            WITH latest_review AS (
              SELECT extraction_id, max(id) AS review_id
              FROM pricing_extraction_reviews
              GROUP BY extraction_id
            )
            SELECT
              extraction.id AS extraction_id,
              snapshot.pricing_task_id,
              snapshot.pricing_source_id,
              task.tool_id,
              tool.canonical_slug,
              source.url AS source_url,
              snapshot.final_url,
              coalesce(snapshot.http_status, 0) AS http_status,
              coalesce(snapshot.content_type, '') AS content_type,
              extraction.raw_extraction_json
            FROM latest_review latest
            JOIN pricing_extraction_reviews review ON review.id = latest.review_id
            JOIN pricing_extractions extraction ON extraction.id = latest.extraction_id
            JOIN pricing_snapshots snapshot ON snapshot.id = extraction.snapshot_id
            JOIN pricing_tasks task ON task.id = snapshot.pricing_task_id
            JOIN pricing_sources source ON source.id = snapshot.pricing_source_id
            JOIN tools tool ON tool.id = task.tool_id
            LEFT JOIN pricing_extraction_materializations materialization
              ON materialization.extraction_id = extraction.id
            WHERE review.decision = 'approved'
              AND extraction.review_status = 'approved'
              AND (
                materialization.extraction_id IS NULL
                OR materialization.status = 'failed'
                OR (materialization.status = 'running' AND materialization.started_at < ?)
              )
              AND coalesce(materialization.attempts, 0) < 5
            ORDER BY review.id
            LIMIT ?
            """,
            [stale_before, limit],
        )
        claimed: list[ReviewedPricingExtraction] = []
        now = utc_now_iso()
        for row in rows:
            claimed_rows = await self.d1.query(
                """
                INSERT INTO pricing_extraction_materializations (
                  extraction_id, status, attempts, started_at, finished_at, last_error, updated_at
                )
                VALUES (?, 'running', 1, ?, NULL, NULL, ?)
                ON CONFLICT(extraction_id) DO UPDATE SET
                  status = 'running',
                  attempts = pricing_extraction_materializations.attempts + 1,
                  started_at = excluded.started_at,
                  finished_at = NULL,
                  last_error = NULL,
                  updated_at = excluded.updated_at
                WHERE pricing_extraction_materializations.attempts < 5
                  AND (
                    pricing_extraction_materializations.status = 'failed'
                    OR (
                      pricing_extraction_materializations.status = 'running'
                      AND pricing_extraction_materializations.started_at < ?
                    )
                  )
                RETURNING extraction_id
                """,
                [int(row["extraction_id"]), now, now, stale_before],
            )
            if not claimed_rows:
                continue
            try:
                payload = json.loads(str(row.get("raw_extraction_json") or "{}"))
            except json.JSONDecodeError:
                await self.fail_materialization(int(row["extraction_id"]), "Invalid extraction JSON")
                continue
            if not isinstance(payload, dict):
                await self.fail_materialization(int(row["extraction_id"]), "Extraction JSON must be an object")
                continue
            claimed.append(
                ReviewedPricingExtraction(
                    extraction_id=int(row["extraction_id"]),
                    pricing_task_id=int(row["pricing_task_id"]),
                    pricing_source_id=int(row["pricing_source_id"]),
                    tool_id=int(row["tool_id"]),
                    canonical_slug=str(row.get("canonical_slug") or ""),
                    source_url=str(row.get("source_url") or ""),
                    final_url=str(row.get("final_url") or row.get("source_url") or ""),
                    http_status=int(row.get("http_status") or 0),
                    content_type=str(row.get("content_type") or ""),
                    payload=payload,
                )
            )
        return claimed

    async def fail_materialization(self, extraction_id: int, error: str) -> None:
        now = utc_now_iso()
        await self.d1.run(
            """
            UPDATE pricing_extraction_materializations
            SET status = 'failed', finished_at = ?, last_error = ?, updated_at = ?
            WHERE extraction_id = ? AND status = 'running'
            """,
            [now, error[:2000], now, extraction_id],
        )

    async def materialize_reviewed_extraction(self, extraction: ReviewedPricingExtraction) -> int:
        plans = list(extraction.payload.get("plans") or [])
        if not plans:
            raise RuntimeError("Approved extraction contains no pricing plans")
        task = PricingTask(
            task_id=extraction.pricing_task_id,
            pricing_source_id=extraction.pricing_source_id,
            tool_id=extraction.tool_id,
            canonical_slug=extraction.canonical_slug,
            source_url=extraction.source_url,
            official_url=extraction.source_url,
            attempts=0,
            max_attempts=1,
            generation=1,
            lease_token="materializer",
        )
        result = PricingFetchResult(
            url=extraction.source_url,
            final_url=extraction.final_url,
            status=extraction.http_status,
            content_type=extraction.content_type,
            html="",
        )
        version_id = await self.save_catalog(task, result, plans)
        await self.update_summary(task, plans)
        now = utc_now_iso()
        await self.d1.batch(
            [
                (
                    """
                    UPDATE pricing_extraction_materializations
                    SET status = 'succeeded', catalog_version_id = ?, finished_at = ?, last_error = NULL, updated_at = ?
                    WHERE extraction_id = ? AND status = 'running'
                    """,
                    [version_id, now, now, extraction.extraction_id],
                ),
                (
                    """
                    UPDATE pricing_tasks
                    SET status = 'succeeded', finished_at = ?, last_error = NULL, updated_at = ?
                    WHERE id = ? AND status = 'manual_review'
                    """,
                    [now, now, extraction.pricing_task_id],
                ),
                (
                    """
                    UPDATE pricing_sources
                    SET last_success_at = ?, next_run_at = ?, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    [now, iso_delta(days=30), now, extraction.pricing_source_id],
                ),
            ]
        )
        return version_id


async def process_task(
    task: TrafficTask,
    similarweb: SimilarWebClient,
    d1: D1Client,
    store: D1TaskStore,
    max_retries: int,
) -> str:
    result = FetchResult(status="failed", monthly_rows=[], error="not_started")
    for attempt in range(max_retries + 1):
        log_info(
            "task.fetch_attempt.start",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            attempt=attempt + 1,
            max_attempts=max_retries + 1,
        )
        result = await similarweb.fetch(task.normalized_domain, task.traffic_month)
        log_info(
            "task.fetch_attempt.done",
            domain=task.normalized_domain,
            traffic_month=task.traffic_month,
            attempt=attempt + 1,
            status=result.status,
        )
        if result.status != "failed":
            break
        if attempt < max_retries:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    if not await store.renew_lease(task):
        return "stale"
    await d1.insert_result(task, result)
    completed = await store.complete_task(task, result)
    return result.status if completed else "stale"


async def process_domain_state(
    task: DomainStateTask,
    client: DomainStateClient,
    store: D1DomainStateStore,
    max_retries: int,
) -> str:
    result = DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error="not_started")
    for attempt in range(max_retries + 1):
        log_info(
            "domain_state.fetch_attempt.start",
            domain=task.normalized_domain,
            attempt=attempt + 1,
            max_attempts=max_retries + 1,
        )
        try:
            result = await client.fetch(task.normalized_domain)
        except Exception as error:
            result = DomainStateResult(status="failed", domain_rating=None, domain_created_at=None, error=str(error)[:300])
        log_info(
            "domain_state.fetch_attempt.done",
            domain=task.normalized_domain,
            attempt=attempt + 1,
            status=result.status,
        )
        if result.status != "failed":
            break
        if attempt < max_retries:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    if not await store.renew_lease(task):
        return "stale"
    completed = await store.complete_task(task, result)
    return result.status if completed else "stale"


async def run_with_telemetry(
    config: Config,
    d1: D1Client,
    workload: str,
    operation: Any,
) -> dict[str, int]:
    telemetry = RunnerTelemetry(d1, config)
    run_id = await telemetry.start(workload)
    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await telemetry.heartbeat()
            except Exception as error:
                log_error("runner.telemetry.heartbeat_failed", workload=workload, error=str(error)[:300])

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        counts = await operation()
    except asyncio.CancelledError:
        try:
            await telemetry.finish(run_id, error="cancelled")
        except Exception as telemetry_error:
            log_error("runner.telemetry.cancel_finish_failed", workload=workload, error=str(telemetry_error)[:300])
        raise
    except Exception as error:
        try:
            await telemetry.finish(run_id, error=str(error)[:2000])
        except Exception as telemetry_error:
            log_error(
                "runner.telemetry.finish_failed",
                workload=workload,
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


async def _run_domain_state_once(config: Config, d1: D1Client, limit: int | None = None) -> dict[str, int]:
    effective_limit = limit or config.domain_state_limit
    log_info("domain_state_runner.batch.start", limit=effective_limit, concurrency=config.concurrency)
    store = D1DomainStateStore(d1)
    client = DomainStateClient(config.ahref_api_key)
    queued = await store.queue_due_tasks(effective_limit, config.domain_state_max_age_days)
    log_info("domain_state_runner.queue_due_tasks.done", queued=queued)
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    log_info("domain_state_runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(config.concurrency)
    counts = {
        "queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "no_data": 0,
        "failed": 0,
        "stale": 0,
    }

    async def guarded(task: DomainStateTask) -> None:
        async with semaphore:
            try:
                status = await process_domain_state(task, client, store, config.max_retries)
            except Exception as error:
                status = "failed"
                log_error(
                    "domain_state.failed_with_exception",
                    domain=task.normalized_domain,
                    error=str(error)[:300],
                )
                try:
                    completed = await store.complete_task(
                        task,
                        DomainStateResult(
                            status="failed",
                            domain_rating=None,
                            domain_created_at=None,
                            error=str(error)[:300],
                        ),
                    )
                    if not completed:
                        status = "stale"
                except Exception as completion_error:
                    log_error(
                        "domain_state.complete_failed",
                        domain=task.normalized_domain,
                        error=str(completion_error)[:300],
                    )
            counts[status] = counts.get(status, 0) + 1
            log_info("domain_state.done", domain=task.normalized_domain, status=status)

    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))

    return counts


async def run_domain_state_once(config: Config, limit: int | None = None) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await run_with_telemetry(
            config,
            d1,
            "domain_state",
            lambda: _run_domain_state_once(config, d1, limit),
        )


async def fetch_favicon_asset(page_url: str, domain: str, html_body: str, favicon_href: str = "") -> FaviconAsset | None:
    favicon_url = urljoin(page_url, favicon_href) if favicon_href else extract_favicon_href(html_body, page_url)
    if not favicon_url:
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                page_response = await client.get(
                    page_url,
                    headers={
                        "User-Agent": random_pricing_user_agent(),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
            if 200 <= page_response.status_code < 300:
                favicon_url = extract_favicon_href(page_response.text[:200000], str(page_response.url))
        except Exception as error:
            log_info("assets.favicon.discover_failed", domain=domain, url=page_url, error=str(error)[:300])
    favicon_url = favicon_url or urljoin(page_url, "/favicon.ico")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                favicon_url,
                headers={
                    "User-Agent": random_pricing_user_agent(),
                    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": page_url,
                },
            )
    except Exception as error:
        log_info("assets.favicon.fetch_failed", domain=domain, url=favicon_url, error=str(error)[:300])
        return None
    if response.status_code < 200 or response.status_code >= 300:
        log_info("assets.favicon.http_error", domain=domain, url=favicon_url, status=response.status_code)
        return None
    try:
        content_length = int(response.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > 1024 * 1024:
        return None
    body = response.content
    if not body or len(body) > 1024 * 1024:
        return None
    mime_type = asset_mime_type(favicon_url, response.headers.get("content-type", ""))
    extension = asset_extension(favicon_url, mime_type)
    return FaviconAsset(
        body=body,
        key=f"{domain}/favicon-{int(time.time() * 1000)}{extension}",
        mime_type=mime_type,
    )


async def process_asset_task(
    task: AssetTask,
    browser_client: CloudflareBrowserRunAssetClient,
    uploader: R2AssetUploader,
    store: D1AssetStore,
    public_base_url: str,
    max_retries: int,
    category_options: list[str],
) -> str:
    last_error = "not_started"
    last_retryable = True
    for attempt in range(max_retries + 1):
        missing_before = await store.missing_asset_requirements(task.tool_id)
        if not missing_before:
            if not await store.complete_task(task, "done"):
                return "stale"
            log_info(
                "asset_task.fetch_attempt.skipped",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                reason="requirements_already_complete",
            )
            return "done"

        stage_errors: list[str] = []
        stage_retryable: list[bool] = []
        core_result: AssetFetchResult | None = None

        def record_stage_error(stage: str, error: Any, *, retryable: bool | None = None) -> None:
            message = str(error).strip()[:500] or type(error).__name__
            stage_errors.append(f"{stage}={message}")
            stage_retryable.append(
                bool(getattr(error, "retryable", True))
                if retryable is None
                else retryable
            )

        try:
            log_info(
                "asset_task.fetch_attempt.start",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                attempt=attempt + 1,
                max_attempts=max_retries + 1,
                requirements=missing_before,
            )

            if "description" in missing_before:
                try:
                    core_result = await browser_client.fetch_homepage_core_metadata(task)
                    if not await store.renew_lease(task):
                        return "stale"
                    await store.save_tool_localization(task, core_result)
                    if core_result.metadata_error:
                        record_stage_error(
                            "description",
                            core_result.metadata_error,
                            retryable=core_result.metadata_retryable,
                        )
                except Exception as error:
                    record_stage_error("description", error)

            if "key_features" in missing_before:
                try:
                    feature_result = await browser_client.fetch_homepage_key_features(task)
                    if not await store.renew_lease(task):
                        return "stale"
                    await store.save_tool_features(task, feature_result)
                    if feature_result.metadata_error:
                        record_stage_error(
                            "key_features",
                            feature_result.metadata_error,
                            retryable=feature_result.metadata_retryable,
                        )
                except Exception as error:
                    record_stage_error("key_features", error)

            if "category" in missing_before:
                try:
                    category_result = await browser_client.fetch_homepage_categories(task, category_options)
                    if not await store.renew_lease(task):
                        return "stale"
                    await store.save_tool_categories(task, category_result)
                    if category_result.metadata_error:
                        record_stage_error(
                            "category",
                            category_result.metadata_error,
                            retryable=category_result.metadata_retryable,
                        )
                except Exception as error:
                    record_stage_error("category", error)

            if "screenshot" in missing_before:
                try:
                    screenshot_result = await browser_client.capture_homepage_screenshot(task)
                    if not await store.renew_lease(task):
                        return "stale"
                    screenshot_key = f"{task.normalized_domain}/{int(time.time() * 1000)}.png"
                    await uploader.put_object(screenshot_key, screenshot_result.screenshot, "image/png")
                    await store.upsert_tool_asset(
                        task,
                        "screenshot",
                        screenshot_key,
                        asset_public_url(public_base_url, screenshot_key),
                        "image/png",
                        1280,
                        720,
                    )
                except Exception as error:
                    record_stage_error("screenshot", error)

            favicon: FaviconAsset | None = None
            if "favicon" in missing_before:
                try:
                    favicon = await fetch_favicon_asset(
                        core_result.final_url if core_result else asset_page_url(task),
                        task.normalized_domain,
                        core_result.html if core_result else "",
                        core_result.favicon_href if core_result else "",
                    )
                    if favicon is None:
                        raise AssetPipelineError("favicon_empty", retryable=True)
                    if not await store.renew_lease(task):
                        return "stale"
                    await uploader.put_object(favicon.key, favicon.body, favicon.mime_type)
                    await store.upsert_tool_asset(
                        task,
                        "favicon",
                        favicon.key,
                        asset_public_url(public_base_url, favicon.key),
                        favicon.mime_type,
                        None,
                        None,
                    )
                except Exception as error:
                    record_stage_error("favicon", error)

            missing_requirements = await store.missing_asset_requirements(task.tool_id)
            blocking_requirements = [
                requirement
                for requirement in missing_requirements
                if requirement != "favicon"
            ]
            if blocking_requirements:
                details = [f"missing={','.join(blocking_requirements)}"]
                if stage_errors:
                    details.append("stages=" + "; ".join(stage_errors))
                last_error = "asset_enrichment_incomplete: " + "; ".join(details)
                last_retryable = any(stage_retryable) if stage_retryable else True
                raise AssetPipelineError(last_error, retryable=last_retryable)

            if not await store.complete_task(task, "done"):
                return "stale"
            log_info(
                "asset_task.fetch_attempt.done",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                status="done",
                favicon=bool(favicon),
                requirements_processed=missing_before,
                remaining_warnings=missing_requirements,
            )
            return "done"
        except Exception as error:
            last_error = str(error)[:900]
            last_retryable = bool(getattr(error, "retryable", True))
            log_error(
                "asset_task.fetch_attempt.failed",
                tool_id=task.tool_id,
                slug=task.canonical_slug,
                domain=task.normalized_domain,
                attempt=attempt + 1,
                error=last_error,
            )
            if attempt < max_retries and last_retryable:
                await asyncio.sleep(random.uniform(1.0, 3.0))
            else:
                break

    completed = await store.complete_task(task, "failed", last_error, retryable=last_retryable)
    return "failed" if completed else "stale"


async def run_openai_pricing_extraction(
    task: PricingTask,
    result: PricingFetchResult,
    payload: dict[str, Any],
    review_status: str,
    confidence: int,
    validation_errors: list[str],
    openai_extractors: list[OpenAIPricingExtractor],
    needs_model_check: bool,
    model_check_reasons: list[str],
) -> tuple[dict[str, Any], str, int, list[str], str | None]:
    if not ((review_status != "approved" or needs_model_check) and openai_extractors and result.page_status == "found"):
        return payload, review_status, confidence, validation_errors, None

    model_name = None
    model_verified = False
    for index, openai_extractor in enumerate(openai_extractors):
        openai_extraction = await openai_extractor.extract(
            result.html,
            task.source_url,
            result.final_url,
            result.status,
            result.error,
        )
        if openai_extraction is None:
            if index + 1 < len(openai_extractors):
                log_info(
                    "pricing.openai.escalate",
                    from_model=openai_extractor.model,
                    to_model=openai_extractors[index + 1].model,
                    reason="empty_response",
                )
            continue

        payload, review_status, confidence, validation_errors = openai_extraction
        model_name = openai_extractor.model
        model_verified = True
        if (review_status != "approved" or confidence < OPENAI_PRICING_MIN_CONFIDENCE) and index + 1 < len(openai_extractors):
            log_info(
                "pricing.openai.escalate",
                from_model=openai_extractor.model,
                to_model=openai_extractors[index + 1].model,
                review_status=review_status,
                confidence=confidence,
                validation_errors=validation_errors[:3],
            )
            continue
        break

    if not model_verified and needs_model_check:
        review_status = "manual_review"
        validation_errors = [*validation_errors, f"Rule extraction requires model verification: {', '.join(model_check_reasons)}"]
        confidence = min(confidence, 55)
    return payload, review_status, confidence, validation_errors, model_name


async def discover_missing_pricing_sources(
    store: D1PricingStore,
    client: PricingClient,
    limit: int,
) -> int:
    if limit <= 0:
        return 0

    created = 0
    for candidate in await store.missing_source_candidates(limit):
        task = PricingTask(
            task_id=0,
            pricing_source_id=0,
            tool_id=candidate.tool_id,
            canonical_slug=candidate.canonical_slug,
            source_url=candidate.official_url,
            official_url=candidate.official_url,
            attempts=0,
            max_attempts=1,
            generation=1,
            lease_token="",
        )
        result = await client.choose_pricing_page(task)
        text = parse_pricing_html(result.html).text if result.html else ""
        text_score = pricing_text_quality(text)
        if (
            result.page_status == "found"
            and result.status == 200
            and is_strict_pricing_url(result.final_url)
            and text_score > 0
        ):
            confidence = 70 if text_score >= 12 else 60
            await store.insert_pricing_source(candidate.tool_id, result.final_url, "homepage_link", confidence)
            created += 1
            log_info(
                "pricing_source.discovery.created",
                tool_id=candidate.tool_id,
                slug=candidate.canonical_slug,
                url=result.final_url,
                text_score=text_score,
            )
        else:
            skip_error = result.error or f"{result.page_status}: HTTP {result.status}; text_score={text_score}"
            await store.mark_pricing_source_discovery_skipped(
                candidate.tool_id,
                result.final_url or candidate.official_url,
                skip_error,
                retryable=(
                    result.status == 0
                    or result.status in (408, 425, 429)
                    or result.status >= 500
                ),
            )
            log_info(
                "pricing_source.discovery.skipped",
                tool_id=candidate.tool_id,
                slug=candidate.canonical_slug,
                final_url=result.final_url,
                status=result.status,
                page_status=result.page_status,
                text_score=text_score,
                error=(result.error or "")[:200],
            )
    return created


def should_render_pricing_with_browser(
    result: PricingFetchResult,
    review_status: str,
    text_score: int,
    validation_errors: list[str],
) -> bool:
    if review_status == "approved":
        return False
    if result.status != 200 or not result.html:
        return False
    if result.page_status not in {"found", "not_found"}:
        return False
    if not is_strict_pricing_url(result.final_url):
        return False
    if text_score < BROWSER_RENDERING_TEXT_SCORE_THRESHOLD:
        return True
    return any("No public pricing plans found" in error for error in validation_errors)


async def process_pricing_task(
    task: PricingTask,
    client: PricingClient,
    openai_extractors: list[OpenAIPricingExtractor],
    browser_renderer: CloudflareBrowserRunRenderer | None,
    store: D1PricingStore,
    max_retries: int,
    approve_pricing: bool,
    dry_run: bool,
) -> str:
    result = PricingFetchResult(task.source_url, task.source_url, 0, "", "", "not_started")
    for attempt in range(max_retries + 1):
        log_info(
            "pricing_task.fetch_attempt.start",
            task_id=task.task_id,
            slug=task.canonical_slug,
            attempt=attempt + 1,
            max_attempts=max_retries + 1,
        )
        result = await client.choose_pricing_page(task)
        log_info(
            "pricing_task.fetch_attempt.done",
            task_id=task.task_id,
            slug=task.canonical_slug,
            attempt=attempt + 1,
            status=result.status,
            final_url=result.final_url,
        )
        if result.status and result.status < 500:
            break
        if attempt < max_retries:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    payload, review_status, confidence, validation_errors = extract_pricing_payload(
        result.html,
        task.source_url,
        result.final_url,
        result.status,
        result.error,
        result.page_status,
        result.discovery_method,
    )
    extractor_version = PRICING_EXTRACTOR_VERSION
    model_name = None
    text_score = pricing_text_quality(parse_pricing_html(result.html).text if result.html else "")
    needs_model_check, model_check_reasons = should_verify_rule_pricing_with_openai(
        payload,
        text_score,
        result.page_status,
    )
    payload, review_status, confidence, validation_errors, model_name = await run_openai_pricing_extraction(
        task,
        result,
        payload,
        review_status,
        confidence,
        validation_errors,
        openai_extractors,
        needs_model_check,
        model_check_reasons,
    )
    if model_name:
        extractor_version = OPENAI_PRICING_EXTRACTOR_VERSION

    if browser_renderer is not None and should_render_pricing_with_browser(result, review_status, text_score, validation_errors):
        rendered_result = await browser_renderer.render(result)
        if rendered_result is not None:
            result = rendered_result
            payload, review_status, confidence, validation_errors = extract_pricing_payload(
                result.html,
                task.source_url,
                result.final_url,
                result.status,
                result.error,
                result.page_status,
                result.discovery_method,
            )
            extractor_version = PRICING_EXTRACTOR_VERSION
            model_name = None
            text_score = pricing_text_quality(parse_pricing_html(result.html).text if result.html else "")
            needs_model_check, model_check_reasons = should_verify_rule_pricing_with_openai(
                payload,
                text_score,
                result.page_status,
            )
            payload, review_status, confidence, validation_errors, model_name = await run_openai_pricing_extraction(
                task,
                result,
                payload,
                review_status,
                confidence,
                validation_errors,
                openai_extractors,
                needs_model_check,
                model_check_reasons,
            )
            if model_name:
                extractor_version = OPENAI_PRICING_EXTRACTOR_VERSION

    final_pipeline_stage = derive_final_pipeline_stage(
        payload,
        review_status,
        extractor_version,
        model_name,
        result.discovery_method,
    )
    payload["final_pipeline_stage"] = final_pipeline_stage

    if review_status == "approved" and not approve_pricing:
        review_status = "manual_review"
        validation_errors = ["Python extraction pending manual approval"]
        confidence = min(confidence, 70)

    if dry_run:
        log_info(
            "pricing_task.dry_run",
            task_id=task.task_id,
            slug=task.canonical_slug,
            review_status=review_status,
            final_pipeline_stage=final_pipeline_stage,
            plans=len(payload.get("plans") or []),
            final_url=result.final_url,
            validation_errors=validation_errors,
        )
        return "dry_run"

    if not await store.renew_lease(task):
        return "stale"
    snapshot_id = await store.insert_snapshot(task, result)
    await store.insert_extraction(
        snapshot_id,
        payload,
        review_status,
        confidence,
        validation_errors,
        extractor_version=extractor_version,
        model_name=model_name,
    )

    if review_status == "approved":
        if not await store.renew_lease(task):
            return "stale"
        plans = list(payload.get("plans") or [])
        await store.save_catalog(task, result, plans)
        await store.update_summary(task, plans)
        completed = await store.finish_task(task, "succeeded", None, result)
        return "succeeded" if completed else "stale"

    error = "; ".join(validation_errors)[:900] or result.error or "manual review"
    completed = await store.finish_task(task, "manual_review", error, result)
    return "manual_review" if completed else "stale"


async def _run_assets_once(config: Config, d1: D1Client, limit: int | None = None) -> dict[str, int]:
    effective_limit = limit or config.asset_limit
    log_info("assets_runner.batch.start", limit=effective_limit, concurrency=config.concurrency)
    browser_client = CloudflareBrowserRunAssetClient(config)
    uploader = R2AssetUploader(config)
    await uploader.check_access()
    store = D1AssetStore(d1)
    category_options = await store.category_options()
    queued = await store.queue_missing_asset_tasks(effective_limit)
    log_info("assets_runner.queue_missing_asset_tasks.done", queued=queued)
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    log_info("assets_runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(config.concurrency)
    counts = {
        "asset_queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "failed": 0,
        "stale": 0,
        "enrichment_ready": 0,
        "enrichment_blocked": 0,
        "enrichment_evaluated": 0,
    }

    async def guarded(task: AssetTask) -> None:
        async with semaphore:
            try:
                status = await process_asset_task(
                    task,
                    browser_client,
                    uploader,
                    store,
                    config.r2_public_base_url,
                    config.max_retries,
                    category_options,
                )
            except Exception as error:
                status = "failed"
                log_error(
                    "asset_task.failed_with_exception",
                    tool_id=task.tool_id,
                    slug=task.canonical_slug,
                    domain=task.normalized_domain,
                    error=str(error)[:300],
                )
                if not await store.complete_task(task, "failed", str(error)[:900]):
                    status = "stale"
            counts[status] = counts.get(status, 0) + 1
            log_info("asset_task.done", tool_id=task.tool_id, slug=task.canonical_slug, status=status)

    enrichment = D1EnrichmentStore(d1)
    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))
        for tool_id in dict.fromkeys(task.tool_id for task in tasks):
            readiness = await enrichment.evaluate_tool(tool_id)
            if readiness in ("ready", "blocked"):
                counts[f"enrichment_{readiness}"] += 1

    reconciliation = await enrichment.reconcile_active_tools(effective_limit)
    counts["enrichment_evaluated"] += reconciliation["evaluated"]
    counts["enrichment_ready"] += reconciliation["ready"]
    counts["enrichment_blocked"] += reconciliation["blocked"]

    return counts


async def run_assets_once(config: Config, limit: int | None = None) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await run_with_telemetry(
            config,
            d1,
            "assets",
            lambda: _run_assets_once(config, d1, limit),
        )


async def _run_once(config: Config, d1: D1Client, limit: int | None = None) -> dict[str, int]:
    effective_limit = limit or config.limit
    log_info("runner.batch.start", limit=effective_limit, concurrency=config.concurrency)
    store = D1TaskStore(d1)
    traffic_month = previous_traffic_month()
    if not traffic_release_probe_window_open(config):
        log_info(
            "traffic_release_gate.not_scheduled",
            traffic_month=traffic_month,
            probe_start_day=config.traffic_release_probe_start_day,
        )
        return {
            "traffic_queued": 0,
            "claimed": 0,
            "done": 0,
            "no_data": 0,
            "forbidden": 0,
            "failed": 0,
            "stale": 0,
            "release_available": 0,
            "release_probe_attempted": 0,
            "release_not_scheduled": 1,
            "release_waiting": 0,
        }

    similarweb = SimilarWebClient(config)
    release = await D1TrafficReleaseStore(d1).check_or_probe(
        traffic_month,
        config.traffic_release_probe_domain,
        config.traffic_release_probe_interval_seconds,
        similarweb,
    )
    if not release.available:
        log_info(
            "traffic_release_gate.blocked",
            traffic_month=traffic_month,
            status=release.status,
            observed_latest_month=release.observed_latest_month,
        )
        return {
            "traffic_queued": 0,
            "claimed": 0,
            "done": 0,
            "no_data": 0,
            "forbidden": 0,
            "failed": 0,
            "stale": 0,
            "release_available": 0,
            "release_probe_attempted": 1 if release.probe_attempted else 0,
            "release_not_scheduled": 0,
            "release_waiting": 0 if release.probe_attempted else 1,
        }

    queued = await store.queue_missing_traffic_tasks(config.traffic_release_queue_limit, traffic_month)
    log_info("runner.queue_missing_traffic_tasks.done", queued=queued, traffic_month=traffic_month)
    tasks = await store.claim_due_tasks(effective_limit, config.runner_instance_id)
    log_info("runner.claim_due_tasks.done", claimed=len(tasks))

    semaphore = asyncio.Semaphore(config.concurrency)
    counts = {
        "traffic_queued": queued,
        "claimed": len(tasks),
        "done": 0,
        "no_data": 0,
        "forbidden": 0,
        "failed": 0,
        "stale": 0,
        "release_available": 1,
        "release_probe_attempted": 1 if release.probe_attempted else 0,
        "release_not_scheduled": 0,
        "release_waiting": 0,
    }

    async def guarded(task: TrafficTask) -> None:
        async with semaphore:
            try:
                log_info("task.start", domain=task.normalized_domain, traffic_month=task.traffic_month)
                status = await process_task(task, similarweb, d1, store, config.max_retries)
            except Exception as error:
                status = "failed"
                log_error(
                    "task.failed_with_exception",
                    domain=task.normalized_domain,
                    traffic_month=task.traffic_month,
                    error=str(error)[:300],
                )
                if not await store.complete_task(task, FetchResult(status="failed", monthly_rows=[], error=str(error)[:300])):
                    status = "stale"
            counts[status] = counts.get(status, 0) + 1
            log_info("task.done", domain=task.normalized_domain, traffic_month=task.traffic_month, status=status)

    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))

    return counts


async def run_once(config: Config, limit: int | None = None) -> dict[str, int]:
    async with D1Client(config) as d1:
        return await run_with_telemetry(
            config,
            d1,
            "traffic",
            lambda: _run_once(config, d1, limit),
        )


async def _run_pricing_once(
    config: Config,
    d1: D1Client,
    limit: int | None = None,
    task_ids: list[int] | None = None,
    approve_pricing: bool = False,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    effective_limit = limit or config.pricing_limit
    log_info(
        "pricing_runner.batch.start",
        limit=effective_limit,
        concurrency=config.concurrency,
        dry_run=dry_run,
        approve_pricing=approve_pricing,
    )
    store = D1PricingStore(d1)
    materialized = 0
    materialization_failed = 0
    if not dry_run:
        reviewed_extractions = await store.claim_reviewed_extractions(effective_limit)
        for extraction in reviewed_extractions:
            try:
                await store.materialize_reviewed_extraction(extraction)
                materialized += 1
            except Exception as error:
                materialization_failed += 1
                await store.fail_materialization(extraction.extraction_id, str(error))
                log_error(
                    "pricing_materialization.failed",
                    extraction_id=extraction.extraction_id,
                    error=str(error)[:300],
                )
    client = PricingClient(timeout_seconds or config.pricing_timeout_seconds)
    openai_models: list[str] = []
    if config.openai_api_key:
        for model in (config.openai_pricing_model, config.openai_pricing_fallback_model):
            clean_model = (model or "").strip()
            if clean_model and clean_model not in openai_models:
                openai_models.append(clean_model)
    openai_extractors = [
        OpenAIPricingExtractor(
            config.openai_api_key,
            model,
            config.openai_pricing_timeout_seconds,
            config.openai_pricing_text_chars,
        )
        for model in openai_models
    ]
    log_info(
        "pricing_runner.openai_config",
        enabled=bool(openai_extractors),
        models=openai_models,
    )
    browser_renderer = CloudflareBrowserRunRenderer(config) if config.browser_rendering_enabled else None
    log_info(
        "pricing_runner.browser_rendering_config",
        enabled=browser_renderer is not None,
        timeout_seconds=config.browser_rendering_timeout_seconds if browser_renderer is not None else None,
    )
    queued = 0
    discovered_sources = 0
    if not task_ids and not dry_run:
        queued = await store.queue_due_tasks(effective_limit)
        log_info("pricing_runner.queue_due_tasks.done", queued=queued)
        if queued < effective_limit:
            discovered_sources = await discover_missing_pricing_sources(store, client, effective_limit - queued)
            log_info("pricing_runner.discover_missing_sources.done", discovered=discovered_sources)
            if discovered_sources:
                queued += await store.queue_due_tasks(effective_limit - queued)
                log_info("pricing_runner.queue_discovered_tasks.done", queued=queued)
    tasks = await store.claim_due_tasks(
        effective_limit,
        task_ids=task_ids,
        claim=not dry_run,
        lease_owner=config.runner_instance_id,
    )
    log_info("pricing_runner.claim_due_tasks.done", claimed=len(tasks), dry_run=dry_run)

    semaphore = asyncio.Semaphore(config.concurrency)
    counts = {
        "queued": queued,
        "discovered_sources": discovered_sources,
        "materialized": materialized,
        "materialization_failed": materialization_failed,
        "claimed": len(tasks),
        "succeeded": 0,
        "manual_review": 0,
        "failed": 0,
        "dry_run": 0,
        "stale": 0,
    }

    async def guarded(task: PricingTask) -> None:
        async with semaphore:
            try:
                log_info("pricing_task.start", task_id=task.task_id, slug=task.canonical_slug)
                status = await process_pricing_task(
                    task,
                    client,
                    openai_extractors,
                    browser_renderer,
                    store,
                    config.max_retries,
                    approve_pricing=approve_pricing,
                    dry_run=dry_run,
                )
            except Exception as error:
                status = "failed" if dry_run else "manual_review"
                log_error(
                    "pricing_task.failed_with_exception",
                    task_id=task.task_id,
                    slug=task.canonical_slug,
                    error=str(error)[:300],
                )
                if not dry_run:
                    if not await store.finish_task(task, "manual_review", str(error)[:900], None):
                        status = "stale"
            counts[status] = counts.get(status, 0) + 1
            log_info("pricing_task.done", task_id=task.task_id, slug=task.canonical_slug, status=status)

    if tasks:
        await asyncio.gather(*(guarded(task) for task in tasks))

    return counts


async def run_pricing_once(
    config: Config,
    limit: int | None = None,
    task_ids: list[int] | None = None,
    approve_pricing: bool = False,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    async with D1Client(config) as d1:
        if dry_run:
            return await _run_pricing_once(
                config,
                d1,
                limit,
                task_ids=task_ids,
                approve_pricing=False,
                dry_run=True,
                timeout_seconds=timeout_seconds,
            )
        return await run_with_telemetry(
            config,
            d1,
            "pricing",
            lambda: _run_pricing_once(
                config,
                d1,
                limit,
                task_ids=task_ids,
                approve_pricing=approve_pricing,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
            ),
        )


async def run_assets_loop(config: Config, limit: int | None, interval_seconds: int) -> None:
    log_info("assets_runner.loop.start", interval_seconds=interval_seconds)
    while True:
        try:
            counts = await run_assets_once(config, limit)
            log_info("assets_runner.batch.summary", **counts)
        except Exception as error:
            log_error("assets_runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(interval_seconds)


async def run_loop(config: Config, limit: int | None, interval_seconds: int) -> None:
    log_info("runner.loop.start", interval_seconds=interval_seconds)
    while True:
        try:
            counts = await run_once(config, limit)
            log_info("runner.batch.summary", **counts)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error("runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(interval_seconds)


async def run_pricing_loop(
    config: Config,
    limit: int | None,
    interval_seconds: int,
    task_ids: list[int] | None,
    approve_pricing: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    log_info("pricing_runner.loop.start", interval_seconds=interval_seconds)
    while True:
        try:
            counts = await run_pricing_once(
                config,
                limit,
                task_ids=task_ids,
                approve_pricing=approve_pricing,
                dry_run=dry_run,
                timeout_seconds=timeout_seconds,
            )
            log_info("pricing_runner.batch.summary", **counts)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_error("pricing_runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(interval_seconds)


async def run_domain_state_loop(config: Config, limit: int | None, interval_seconds: int) -> None:
    log_info("domain_state_runner.loop.start", interval_seconds=interval_seconds)
    while True:
        try:
            counts = await run_domain_state_once(config, limit)
            log_info("domain_state_runner.batch.summary", **counts)
        except Exception as error:
            log_error("domain_state_runner.batch.failed", error=str(error)[:500])
        await asyncio.sleep(interval_seconds)


async def run_all_loop(config: Config, limit: int | None, interval_seconds: int, timeout_seconds: int | None) -> None:
    shared_interval = interval_seconds or 300
    assets_interval = max(60, shared_interval // 2)
    traffic_interval = shared_interval
    domain_state_interval = max(900, shared_interval * 3)
    pricing_interval = max(900, shared_interval * 3)
    log_info(
        "all_runner.loop.start",
        assets_interval_seconds=assets_interval,
        traffic_interval_seconds=traffic_interval,
        domain_state_interval_seconds=domain_state_interval,
        pricing_interval_seconds=pricing_interval,
    )
    await asyncio.gather(
        run_assets_loop(config, config.asset_limit, assets_interval),
        run_loop(config, limit or config.limit, traffic_interval),
        run_domain_state_loop(config, config.domain_state_limit, domain_state_interval),
        run_pricing_loop(config, config.pricing_limit, pricing_interval, None, False, False, timeout_seconds),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled traffic, assets, and pricing runner")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process one batch and exit")
    mode.add_argument("--loop", action="store_true", help="poll tasks forever")
    parser.add_argument("--pricing", action="store_true", help="process pricing_tasks instead of traffic_tasks")
    parser.add_argument("--assets", action="store_true", help="process asset_tasks instead of traffic_tasks")
    parser.add_argument("--domain-state", action="store_true", help="process domain rating and whois creation date tasks")
    parser.add_argument(
        "--backfill-traffic-monthly",
        action="store_true",
        help="rebuild domain_traffic_monthly from raw Similarweb snapshots",
    )
    parser.add_argument("--all", action="store_true", help="run traffic, domain-state, pricing, and assets loops in one process")
    parser.add_argument("--approve-pricing", action="store_true", help="write approved pricing extractions into active catalogs")
    parser.add_argument("--dry-run", action="store_true", help="for pricing mode, fetch and extract without D1 writes")
    parser.add_argument("--task-id", type=int, action="append", default=[], help="pricing task id to run; can be repeated")
    parser.add_argument("--timeout", type=int, default=None, help="pricing HTTP timeout in seconds")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=None)
    args = parser.parse_args()
    selected = [args.pricing, args.assets, args.domain_state, args.backfill_traffic_monthly, args.all]
    if sum(1 for value in selected if value) > 1:
        parser.error("--pricing, --assets, --domain-state, --backfill-traffic-monthly, and --all are mutually exclusive")
    if args.all and not args.loop:
        parser.error("--all requires --loop")
    if args.approve_pricing:
        parser.error("--approve-pricing is retired; approve the stored extraction in ainav Admin")
    if (args.approve_pricing or args.task_id or args.dry_run) and not args.pricing:
        parser.error("--approve-pricing, --task-id, and --dry-run require --pricing")
    if args.backfill_traffic_monthly and args.loop:
        parser.error("--backfill-traffic-monthly cannot be combined with --loop")
    return args


def main() -> None:
    args = parse_args()
    config = load_config(
        require_brightdata=not (
            args.pricing
            or args.assets
            or args.domain_state
            or args.backfill_traffic_monthly
        )
    )
    interval_seconds = args.interval_seconds or config.poll_interval_seconds
    if args.backfill_traffic_monthly:
        counts = asyncio.run(backfill_domain_traffic_monthly(config, args.limit))
        log_info("traffic_projection_backfill.summary", **counts)
        return
    if args.all:
        asyncio.run(run_all_loop(config, args.limit, interval_seconds, args.timeout))
        return

    if args.assets:
        if args.loop:
            asyncio.run(run_assets_loop(config, args.limit, interval_seconds))
            return

        counts = asyncio.run(run_assets_once(config, args.limit))
        log_info("assets_runner.batch.summary", **counts)
        return

    if args.domain_state:
        if args.loop:
            asyncio.run(run_domain_state_loop(config, args.limit, interval_seconds))
            return True

        counts = asyncio.run(run_domain_state_once(config, args.limit))
        log_info("domain_state_runner.batch.summary", **counts)
        return

    if args.pricing:
        if args.loop:
            asyncio.run(
                run_pricing_loop(
                    config,
                    args.limit,
                    interval_seconds,
                    args.task_id,
                    args.approve_pricing,
                    args.dry_run,
                    args.timeout,
                )
            )
            return

        counts = asyncio.run(
            run_pricing_once(
                config,
                args.limit,
                task_ids=args.task_id,
                approve_pricing=args.approve_pricing,
                dry_run=args.dry_run,
                timeout_seconds=args.timeout,
            )
        )
        log_info("pricing_runner.batch.summary", **counts)
        return

    if args.loop:
        asyncio.run(run_loop(config, args.limit, interval_seconds))
        return

    counts = asyncio.run(run_once(config, args.limit))
    log_info("runner.batch.summary", **counts)


if __name__ == "__main__":
    main()
