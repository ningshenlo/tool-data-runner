from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import ipaddress
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from anti_bot_signatures import detect_anti_bot_page
from shared.readiness import (
    accepted_primary_taxonomy_predicate,
    tool_auto_publish_admission_predicate,
    tool_live_ready_predicate,
)
from taxonomy_schema import strict_json_schema
from workloads.assets.constants import (
    ASSET_DB_STORAGE_BUCKET,
    ASSET_DEAD_LETTER_REVIVE_HOURS,
    ASSET_REQUIREMENT_ORDER,
    ASSET_SOURCE,
    AUTO_APPROVE_TOOL_NAME_CONFIDENCE,
    CATALOG_AUTO_PUBLISH_MAX_LIMIT,
    CATALOG_AUTO_PUBLISH_POLICY_VERSION,
    DEFAULT_R2_BUCKET,
    REVIEW_TOOL_NAME_CONFIDENCE,
)
from workloads.assets.types import AssetTask, ContentSafetyAssessment, PageQualityAssessment, ToolNameResolution

from runner import (
    browser_request_headers,
    get_registrable_domain,
    normalize_domain,
    random_browser_user_agent,
)

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


def homepage_url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip(".").lower()
    except ValueError:
        return ""


def is_safe_homepage_url(url: str) -> bool:
    """Reject local/private targets before the runner performs a direct fetch."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").strip(".").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port not in {80, 443})
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def homepage_urls_share_site(source_url: str, final_url: str) -> bool:
    source_host = homepage_url_host(source_url)
    final_host = homepage_url_host(final_url)
    if not source_host or not final_host:
        return False
    return (
        source_host == final_host
        or source_host.endswith(f".{final_host}")
        or final_host.endswith(f".{source_host}")
    )

def read_html_attribute(tag: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag, re.I)
    if not match or not match.group(1):
        return ""
    return match.group(1).strip().strip("\"'").strip()


def clean_asset_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return html.unescape(text)[:limit]

def normalize_taxonomy_model_id(value: Any) -> str:
    """Normalize category model ids for Browser Rendering custom_ai.

    Accepts short DeepSeek names (``deepseek-v4-flash``) and expands them to the
    provider form required by Cloudflare custom_ai (``deepseek/deepseek-v4-flash``).
    """
    model = str(value or "").strip()
    if not model:
        return ""
    if model.startswith(("workers-ai/", "deepseek/", "openai/", "anthropic/", "google-ai-studio/", "groq/")):
        return model
    if model.startswith("deepseek-") or model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        return f"deepseek/{model}"
    return model


def taxonomy_model_auth_token(model: str, config_or_client: Any) -> str:
    """Pick the provider credential for a category custom_ai model entry."""
    model_id = normalize_taxonomy_model_id(model)
    if model_id.startswith("deepseek/"):
        deepseek_key = getattr(config_or_client, "taxonomy_deepseek_api_key", None)
        if deepseek_key is None:
            deepseek_key = getattr(config_or_client, "taxonomy_classification_deepseek_api_key", "")
        return str(deepseek_key or "").strip()
    if model_id.startswith("openai/"):
        openai_key = getattr(config_or_client, "taxonomy_openai_api_key", None)
        if openai_key is None:
            openai_key = getattr(config_or_client, "openai_api_key", "")
        return str(openai_key or "").strip()
    token = getattr(config_or_client, "taxonomy_api_token", None)
    if token is None:
        token = getattr(config_or_client, "taxonomy_classification_api_token", "")
    return str(token or "").strip()


def build_browser_response_format(
    json_schema: dict[str, Any],
    *,
    model: str | None = None,
    stage: str = "extraction",
) -> dict[str, Any]:
    """Build provider-compatible response_format for Browser Rendering /json.

    DeepSeek's API documents ``json_object`` only. Forwarding OpenAI-style
    ``json_schema`` without a nested ``schema`` field fails with:
    ``response_format: missing field schema``.
    Workers AI / default Browser Run accept a json_schema envelope with
    ``name`` + ``schema``.
    """
    model_id = normalize_taxonomy_model_id(model or "")
    if model_id.startswith("deepseek/"):
        return {"type": "json_object"}

    schema_name = re.sub(r"[^a-z0-9_]+", "_", (stage or "extraction").lower()).strip("_")[:64] or "extraction"
    # OpenAI strict mode also requires every nested property to be required.
    # Preserve caller input: the same schema may be reused by another provider.
    if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict) and "name" in json_schema:
        envelope = dict(json_schema)
        if model_id.startswith("openai/") and envelope.get("strict", False):
            envelope["schema"] = strict_json_schema(envelope["schema"])
        return {"type": "json_schema", "json_schema": envelope}
    if model_id.startswith("openai/"):
        json_schema = strict_json_schema(json_schema)

    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": json_schema,
        },
    }


def augment_prompt_for_response_format(
    prompt: str,
    json_schema: dict[str, Any],
    response_format: dict[str, Any],
) -> str:
    """When using json_object mode, pin the expected keys into the prompt."""
    if response_format.get("type") != "json_object":
        return prompt
    schema = json_schema
    if isinstance(json_schema.get("schema"), dict):
        schema = json_schema["schema"]
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return (
            f"{prompt}\n\nReturn one JSON object only. Do not wrap it in markdown."
        )
    required = schema.get("required") if isinstance(schema, dict) else None
    keys = list(properties.keys())
    required_keys = [str(item) for item in required] if isinstance(required, list) else keys
    return (
        f"{prompt}\n\n"
        "Return one JSON object only (no markdown fences). "
        f"Required keys: {', '.join(required_keys)}. "
        f"Allowed keys: {', '.join(keys)}. "
        "Use empty strings when a value is unknown."
    )


def extract_json_object_from_text(text: str) -> dict[str, Any]:
    """Best-effort extraction of a schema JSON object from free-form model text."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    # Prefer the last compact object that mentions a category field.
    candidates = re.findall(r"\{[^{}]*\"category_l[12]\"[^{}]*\}", cleaned)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    # gpt-oss sometimes concludes in prose: category_l1 = "coding-development"
    match = re.search(
        r"category_l1\s*[:=]\s*[\"']([a-z0-9][a-z0-9-]{0,119})[\"']",
        cleaned,
        flags=re.IGNORECASE,
    )
    if match:
        result = {"category_l1": match.group(1).lower()}
        match_l2 = re.search(
            r"category_l2\s*[:=]\s*[\"']([a-z0-9][a-z0-9-]{0,119})[\"']",
            cleaned,
            flags=re.IGNORECASE,
        )
        if match_l2:
            result["category_l2"] = match_l2.group(1).lower()
        return result
    return {}


def normalize_structured_json_payload(payload: Any) -> dict[str, Any]:
    """Normalize Browser Run /json payloads across default and custom AI models.

    Cloudflare's default /json path usually returns a flat object matching the
    requested schema. Workers AI custom models (especially gpt-oss) often wrap the
    same object inside OpenAI-style ``choices[0].message.content`` as a JSON string,
    or leave content null while the decision appears only in reasoning text.
    """
    if isinstance(payload, str):
        return extract_json_object_from_text(payload)

    if not isinstance(payload, dict):
        return {}

    # Already looks like a schema object (category/core/features).
    schema_keys = {
        "category_l1",
        "category_l2",
        "title",
        "product_name",
        "page_title",
        "name_confidence",
        "name_source",
        "name_evidence",
        "content_safety_label",
        "content_safety_confidence",
        "content_safety_evidence",
        "description",
        "favicon_href",
        "key_features",
    }
    if schema_keys.intersection(payload.keys()):
        return payload

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            for key in ("content", "reasoning", "reasoning_content"):
                normalized = normalize_structured_json_payload(message.get(key))
                if normalized and schema_keys.intersection(normalized.keys()):
                    return normalized
        normalized = normalize_structured_json_payload(first)
        if normalized and schema_keys.intersection(normalized.keys()):
            return normalized

    # Message-shaped object returned without a choices wrapper.
    if any(key in payload for key in ("content", "reasoning", "reasoning_content")):
        for key in ("content", "reasoning", "reasoning_content"):
            normalized = normalize_structured_json_payload(payload.get(key))
            if normalized and schema_keys.intersection(normalized.keys()):
                return normalized

    for key in ("result", "json", "output", "data", "response", "message"):
        if key not in payload:
            continue
        nested = payload.get(key)
        if nested is payload:
            continue
        normalized = normalize_structured_json_payload(nested)
        if normalized and (
            schema_keys.intersection(normalized.keys()) or normalized is not nested
        ):
            # Avoid returning the same empty-ish wrapper repeatedly.
            if schema_keys.intersection(normalized.keys()) or any(
                value not in (None, "", [], {}) for value in normalized.values()
            ):
                if schema_keys.intersection(normalized.keys()):
                    return normalized

    return payload
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


def canonical_slug_fallback_tool_name(task: AssetTask) -> str:
    base = re.sub(r"-[0-9a-f]{8}$", "", clean_public_slug(task.canonical_slug))
    words = [part for part in base.split("-") if part]
    if not words:
        return domain_fallback_tool_name(task.normalized_domain)
    acronyms = {"ai": "AI", "api": "API", "gpt": "GPT", "llm": "LLM", "seo": "SEO", "crm": "CRM"}
    return " ".join(acronyms.get(word, word.capitalize()) for word in words)[:120]


def deterministic_fallback_description(
    task: AssetTask,
    html_body: str,
    product_name: str = "",
) -> str:
    meta_description = read_html_meta(
        html_body,
        {"description", "og:description", "twitter:description"},
    )
    if meta_description:
        return meta_description
    for tag_name in ("p", "h1"):
        for match in re.finditer(rf"<{tag_name}\b[^>]*>([\s\S]*?)</{tag_name}>", html_body or "", re.I):
            text = clean_asset_text(match.group(1), 500)
            if len(text) >= 24:
                return text
    name = clean_asset_text(product_name, 120) or canonical_slug_fallback_tool_name(task)
    return f"{name} is an AI product available from {task.normalized_domain}."[:500]


def deterministic_fallback_key_features(
    product_name: str,
    description: str,
    html_body: str = "",
) -> list[dict[str, str]]:
    features: list[dict[str, str]] = []
    seen: set[str] = set()
    generic = {"features", "solutions", "product", "products", "why choose us", "how it works"}
    for match in re.finditer(r"<h[23]\b[^>]*>([\s\S]*?)</h[23]>", html_body or "", re.I):
        name = clean_asset_text(match.group(1), 120)
        key = name.lower()
        if len(name) < 4 or key in generic or key in seen:
            continue
        seen.add(key)
        features.append({"name": name, "description": ""})
        if len(features) >= 4:
            break
    if features:
        return features
    summary = clean_asset_text(description, 240)
    name = clean_asset_text(product_name, 80)
    return [{
        "name": f"{name} overview" if name else "Product overview",
        "description": summary,
    }]
def strip_html_comments(html_body: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->", " ", html_body or "")


def read_html_title(html_body: str) -> str:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", strip_html_comments(html_body), re.I)
    return clean_asset_text(match.group(1), 120) if match else ""


def read_html_meta(html_body: str, names: set[str]) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", strip_html_comments(html_body), re.I):
        key = (read_html_attribute(tag, "property") or read_html_attribute(tag, "name")).lower()
        content = read_html_attribute(tag, "content")
        if key in names and content:
            return clean_asset_text(content, 500)
    return ""


_CHALLENGE_TITLE_RE = re.compile(
    r"^(?:just\s+(?:a\s+moment|wait)|checking\s+your\s+browser|"
    r"verify\s+(?:you\s+are\s+human|your\s+identity)|attention\s+required|security\s+check)\b",
    re.I,
)
_ACCESS_DENIED_TITLE_RE = re.compile(
    r"^(?:access\s+denied|forbidden|unauthorized|request\s+(?:was\s+)?blocked|error\s*403)\b",
    re.I,
)
_ERROR_TITLE_RE = re.compile(
    r"^(?:404|not\s+found|page\s+not\s+found|500|internal\s+server\s+error|service\s+unavailable)\b",
    re.I,
)
_GENERIC_PAGE_NAME_RE = re.compile(r"^(?:home|homepage|welcome|index|default\s+page)$", re.I)
_MARKETING_PAGE_NAME_RE = re.compile(r"^(?:best|free|online|official)\b", re.I)
_TITLE_SEPARATOR_RE = re.compile(r"\s*[|｜–—]\s*|\s+[·-]\s+|[:：]\s+")


_HOMEPAGE_EXCLUDED_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "footer",
    "svg",
}
_HOMEPAGE_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


class _HomepageMainTextParser(HTMLParser):
    """Extract semantic homepage body text without navigation or executable markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.excluded_depth = 0
        self.body_depth = 0
        self.main_depth = 0
        self.main_end_tags: list[str] = []
        self.document_parts: list[str] = []
        self.body_parts: list[str] = []
        self.main_parts: list[str] = []

    def _append(self, value: str) -> None:
        if self.excluded_depth > 0 or not value:
            return
        self.document_parts.append(value)
        if self.body_depth > 0:
            self.body_parts.append(value)
        if self.main_depth > 0:
            self.main_parts.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in _HOMEPAGE_EXCLUDED_TAGS:
            self.excluded_depth += 1
            return
        if self.excluded_depth > 0:
            return
        attributes = {str(key).lower(): str(value or "").lower() for key, value in attrs}
        if normalized == "body":
            self.body_depth += 1
        if normalized == "main" or attributes.get("role") == "main":
            self.main_depth += 1
            self.main_end_tags.append(normalized)
        if normalized in _HOMEPAGE_BLOCK_TAGS:
            self._append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if self.excluded_depth == 0 and normalized in _HOMEPAGE_BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _HOMEPAGE_EXCLUDED_TAGS:
            self.excluded_depth = max(0, self.excluded_depth - 1)
            return
        if self.excluded_depth > 0:
            return
        if normalized in _HOMEPAGE_BLOCK_TAGS:
            self._append("\n")
        if self.main_end_tags and normalized == self.main_end_tags[-1]:
            self.main_end_tags.pop()
            self.main_depth -= 1
        if normalized == "body" and self.body_depth > 0:
            self.body_depth -= 1

    def handle_data(self, data: str) -> None:
        self._append(data)


def _normalize_homepage_text(parts: list[str], limit: int) -> str:
    lines: list[str] = []
    previous = ""
    for raw_line in re.split(r"[\r\n]+", "".join(parts)):
        line = re.sub(r"\s+", " ", html.unescape(raw_line)).strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    text = "\n".join(lines).strip()
    return text[: max(1, limit)].rstrip()


def extract_homepage_main_text(html_body: str, limit: int = 10000) -> str:
    """Return cleaned homepage main/body text for classification model input."""
    parser = _HomepageMainTextParser()
    try:
        parser.feed(html_body or "")
        parser.close()
    except Exception:
        text = re.sub(
            r"<(?:script|style|noscript|template|nav|footer|svg)\b[\s\S]*?</(?:script|style|noscript|template|nav|footer|svg)>",
            " ",
            html_body or "",
            flags=re.I,
        )
        text = re.sub(r"<[^>]+>", "\n", text)
        return _normalize_homepage_text([text], limit)

    selected = parser.main_parts or parser.body_parts or parser.document_parts
    return _normalize_homepage_text(selected, limit)


def page_visible_text(html_body: str, limit: int = 12000) -> str:
    text = re.sub(r"<script\b[\s\S]*?</script>", " ", html_body or "", flags=re.I)
    text = re.sub(r"<style\b[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_asset_text(text, limit)


def classify_page_state(
    html_body: str,
    *,
    page_title: str = "",
    http_status: int | None = None,
) -> PageQualityAssessment:
    """Classify the captured target page before any enrichment is persisted.

    Browser Rendering can return HTTP 200 while the rendered target is a WAF or
    anti-bot challenge. Content signatures therefore take precedence over the
    outer API status.
    """
    title = clean_asset_text(page_title or read_html_title(html_body), 200)
    raw_lower = (html_body or "").lower()
    visible = page_visible_text(html_body)
    visible_lower = visible.lower()

    anti_bot = detect_anti_bot_page(
        html_body,
        page_title=title,
        http_status=http_status,
    )
    if anti_bot:
        return PageQualityAssessment(
            anti_bot.state,
            f"anti_bot_signature:{anti_bot.provider}:{anti_bot.code}",
            anti_bot.evidence,
        )

    if http_status in {401, 403}:
        return PageQualityAssessment("access_denied", f"http_{http_status}", title)
    if http_status in {404, 410}:
        return PageQualityAssessment("not_found", f"http_{http_status}", title)
    if http_status == 429:
        return PageQualityAssessment("anti_bot", "http_429", title)
    if http_status is not None and http_status >= 500 and not html_body.strip():
        return PageQualityAssessment("error_page", f"http_{http_status}", title)

    if _ACCESS_DENIED_TITLE_RE.search(title):
        return PageQualityAssessment("access_denied", "access_denied_title", title)
    if _CHALLENGE_TITLE_RE.search(title):
        return PageQualityAssessment("anti_bot", "challenge_title", title)
    if _ERROR_TITLE_RE.search(title):
        return PageQualityAssessment("error_page", "error_title", title)

    challenge_signatures = (
        "cf_chl_opt",
        "cf-chl-",
        "datadome-captcha",
        "_incapsula_resource",
        "imperva captcha",
        "akamai bot manager",
    )
    signature = next((item for item in challenge_signatures if item in raw_lower), "")
    if signature:
        return PageQualityAssessment("anti_bot", "challenge_html_signature", signature)

    access_phrases = (
        "you don't have permission to access",
        "you do not have permission to access",
        "the request was blocked",
        "request has been blocked",
        "access to this page has been denied",
    )
    phrase = next((item for item in access_phrases if item in visible_lower), "")
    if phrase:
        return PageQualityAssessment("access_denied", "access_denied_body", phrase)

    human_check_phrases = (
        "checking your browser before accessing",
        "verify you are human",
        "verifying you are human",
        "complete the security check",
        "enable javascript and cookies to continue",
    )
    phrase = next((item for item in human_check_phrases if item in visible_lower), "")
    if phrase:
        return PageQualityAssessment("captcha", "human_verification_body", phrase)

    parked_phrases = (
        "this domain is for sale",
        "this domain may be for sale",
        "buy this domain",
        "domain is parked",
        "domain parking",
        "make an offer on this domain",
        "inquire about this domain",
    )
    parked_phrase = next(
        (item for item in parked_phrases if item in f"{title.lower()} {visible_lower}"),
        "",
    )
    if parked_phrase:
        return PageQualityAssessment("parked_domain", "domain_parking_signature", parked_phrase)

    if len(visible) < 20:
        return PageQualityAssessment("empty_page", "insufficient_visible_content", visible[:120])
    return PageQualityAssessment("valid_product_page", "usable_html", title)


def invalid_tool_name_reason(value: Any) -> str:
    name = clean_asset_text(value, 160)
    if not name:
        return "empty_name"
    if re.search(r"</?(?:title|meta)\b|-->", name, re.I):
        return "html_markup_name"
    if _CHALLENGE_TITLE_RE.search(name):
        return "anti_bot_name"
    if _ACCESS_DENIED_TITLE_RE.search(name):
        return "access_denied_name"
    if _ERROR_TITLE_RE.search(name):
        return "error_page_name"
    if _GENERIC_PAGE_NAME_RE.fullmatch(name) or re.fullmatch(r"sign\s*in|log\s*in|pricing|contact|about(?:\s+us)?", name, re.I):
        return "generic_page_name"
    if len(name) > 80:
        return "name_too_long"
    if _TITLE_SEPARATOR_RE.search(name):
        return "unsplit_page_title"
    if _MARKETING_PAGE_NAME_RE.search(name) and len(name.split()) >= 4:
        return "marketing_page_title"
    if re.search(r"^\d+[+%]?\s", name) and len(name.split()) >= 4:
        return "marketing_page_title"
    if len(name.split()) > 8:
        return "name_too_many_words"
    return ""


def _json_ld_product_names(html_body: str, hostname: str = "") -> list[str]:
    html_body = re.sub(r"<!--[\s\S]*?-->", " ", html_body or "")
    names: list[str] = []
    accepted_types = {
        "product",
        "softwareapplication",
        "webapplication",
        "mobileapplication",
        "webapp",
        "website",
    }

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        raw_type = value.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        normalized_types = {str(item or "").replace(" ", "").lower() for item in types}
        same_site = True
        if hostname and value.get("url"):
            try:
                candidate_host = urlsplit(urljoin(f"https://{hostname}", str(value["url"]))).hostname or ""
                same_site = candidate_host.lower().removeprefix("www.") == hostname.lower().removeprefix("www.")
            except ValueError:
                same_site = False
        if same_site and normalized_types & accepted_types:
            name = clean_asset_text(value.get("name"), 120)
            if name:
                names.append(name)
        for key in ("@graph", "mainEntity"):
            if key in value:
                visit(value[key])

    for match in re.finditer(
        r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>([\s\S]*?)</script>",
        html_body or "",
        re.I,
    ):
        try:
            visit(json.loads(match.group(1).strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return list(dict.fromkeys(names))


def _domain_brand_label(hostname: str) -> str:
    registrable = get_registrable_domain(hostname)
    return (registrable.split(".", 1)[0] or hostname.split(".", 1)[0]).strip().lower()


def _compact_name(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def _matches_domain_brand(name: str, hostname: str) -> bool:
    name_key = _compact_name(name)
    host = hostname.lower().removeprefix("www.")
    domain_key = _compact_name(host.split(".")[0])
    return len(domain_key) >= 3 and domain_key not in {"app", "api", "docs", "blog"} and name_key in {domain_key, f"{domain_key}ai", _compact_name(host)}


def domain_fallback_tool_name(hostname: str) -> str:
    label = _domain_brand_label(hostname)
    parts = [part for part in re.split(r"[-_]+", label) if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)[:120] or "Tool"


def resolve_tool_name(
    html_body: str,
    hostname: str,
    *,
    model_product_name: Any = "",
    model_page_title: Any = "",
    model_confidence: Any = 0,
    model_source: Any = "",
    model_evidence: Any = "",
) -> ToolNameResolution:
    deterministic_page_title = (
        read_html_title(html_body)
        or read_html_meta(html_body, {"og:title", "twitter:title"})
    )
    model_title = clean_asset_text(model_page_title, 200)
    if re.search(r"</?(?:title|meta)|-->|json-ld.{0,80}hreflang", model_title, re.I):
        model_title = ""
    page_title = deterministic_page_title or model_title

    # 1. Upstream submitted/model name
    product_name = clean_asset_text(model_product_name, 120)
    if product_name and not invalid_tool_name_reason(product_name):
        return ToolNameResolution(
            product_name, page_title, "submitted_name", 95, "upstream_name", "auto_approved"
        )

    # 2. og:site_name / application-name
    site_name = read_html_meta(
        html_body, {"og:site_name", "application-name", "apple-mobile-web-app-title"}
    )
    if site_name and not invalid_tool_name_reason(site_name):
        return ToolNameResolution(
            site_name, page_title, "open_graph_site_name", 90, "meta_site_name", "auto_approved"
        )

    # 3. JSON-LD name
    for json_name in _json_ld_product_names(html_body, hostname):
        if json_name and not invalid_tool_name_reason(json_name):
            return ToolNameResolution(
                json_name, page_title, "json_ld_product", 88, "json_ld_name", "auto_approved"
            )

    # 4. Page title segment (prefer matching domain brand or shortest segment)
    if page_title and not (
        _CHALLENGE_TITLE_RE.search(page_title)
        or _ACCESS_DENIED_TITLE_RE.search(page_title)
        or _ERROR_TITLE_RE.search(page_title)
    ):
        segments = [
            clean_asset_text(part, 120).strip(" -|–—:·")
            for part in _TITLE_SEPARATOR_RE.split(page_title)
        ]
        eligible_segments = [s for s in segments if s and not invalid_tool_name_reason(s)]
        if eligible_segments:
            matching = next((s for s in eligible_segments if _matches_domain_brand(s, hostname)), None)
            chosen = matching or min(eligible_segments, key=len)
            return ToolNameResolution(
                chosen, page_title, "page_title_segment", 85, f"page_title: {chosen}", "auto_approved"
            )

    # 5. Domain fallback
    fallback = domain_fallback_tool_name(hostname)
    return ToolNameResolution(
        fallback, page_title, "domain_fallback", 80, f"hostname={hostname}", "auto_approved"
    )


_CONTENT_SAFETY_STRONG_DOMAIN_RE = re.compile(
    r"porn|porno|pornify|pornjoy|pornwork|hentai|nsfw|deepnude|nudify|undress|clothoff|rule34|sexchat|aiporn",
    re.I,
)
_CONTENT_SAFETY_STRONG_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("porn", re.compile(r"\b(?:ai\s*)?porn(?:ographic)?\b", re.I)),
    ("nsfw", re.compile(r"\bnsfw\b", re.I)),
    ("hentai", re.compile(r"\bhentai\b", re.I)),
    ("deepnude", re.compile(r"\bdeep\s*nude\b|\bdeepnude\b", re.I)),
    ("nudify", re.compile(r"\bnudif(?:y|ies|ied)\b", re.I)),
    ("undress-ai", re.compile(r"\b(?:ai\s*)?undress(?:\s+(?:app|ai|photo|image|generator))?\b", re.I)),
    ("remove-clothes", re.compile(r"\bremove\s+(?:her\s+|their\s+)?clothes\b", re.I)),
    ("erotic-content", re.compile(r"\berotic(?:a|\s+(?:story|chat|content|image|roleplay))?\b", re.I)),
    ("sexting", re.compile(r"\bsexting\b", re.I)),
    ("adult-generator", re.compile(r"\badult\s+(?:content|image|video)\s+generator\b", re.I)),
    ("explicit-sexual", re.compile(r"\b(?:nude|naked)\b.{0,50}\b(?:sex|sexual|generator|image|video)\b", re.I)),
    ("xxx", re.compile(r"\bxxx\b", re.I)),
    ("chinese-porn", re.compile(r"色情|成人视频|黄片|裸照|脱衣|成人图片")),
)
_CONTENT_SAFETY_WEAK_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("uncensored", re.compile(r"\buncensored\b", re.I)),
    ("unrestricted", re.compile(r"\bunrestricted\b", re.I)),
    ("spicy", re.compile(r"\bspicy\b", re.I)),
    ("adult", re.compile(r"\badult\b", re.I)),
    ("ai-girlfriend", re.compile(r"\bai\s+girlfriend\b", re.I)),
    ("ai-boyfriend", re.compile(r"\bai\s+boyfriend\b", re.I)),
    ("fantasy-companion", re.compile(r"\b(?:fantasy|intimate)\s+(?:ai\s+)?(?:companion|chat)\b", re.I)),
    ("no-filter-roleplay", re.compile(r"\b(?:no\s+filter|unfiltered)\b.{0,50}\b(?:roleplay|chat|image|video)\b", re.I)),
)
_CONTENT_SAFETY_SAFE_CONTEXT_RE = re.compile(
    r"\b(?:llm\s+safety|red\s+team|adversarial\s+attack|business\s+(?:domain\s+)?model|data\s+model|code\s+generation|application\s+model)\b",
    re.I,
)


def assess_content_safety(
    html_body: str,
    hostname: str,
    *,
    product_name: Any = "",
    description: Any = "",
    model_label: Any = "",
    model_confidence: Any = 0,
    model_evidence: Any = "",
) -> ContentSafetyAssessment:
    visible = page_visible_text(html_body)[:20000]
    text = " ".join(
        value for value in (
            clean_asset_text(product_name, 200),
            clean_asset_text(description, 1000),
            visible,
        ) if value
    )
    evidence: list[str] = []
    normalized_domain = str(hostname or "").lower().removeprefix("www.")
    if normalized_domain and _CONTENT_SAFETY_STRONG_DOMAIN_RE.search(normalized_domain):
        evidence.append(f"domain:{normalized_domain}")
    for label, pattern in _CONTENT_SAFETY_STRONG_SIGNALS:
        if pattern.search(text):
            evidence.append(f"strong:{label}")
    if evidence:
        return ContentSafetyAssessment(
            "nsfw",
            min(100, 95 + min(len(evidence) - 1, 5)),
            98,
            "explicit_adult_product_signal",
            evidence[:12],
            "deterministic_rules",
        )

    weak = [f"weak:{label}" for label, pattern in _CONTENT_SAFETY_WEAK_SIGNALS if pattern.search(text)]
    safe_context = bool(_CONTENT_SAFETY_SAFE_CONTEXT_RE.search(text))
    label = clean_asset_text(model_label, 30).lower().replace("-", "_")
    try:
        stated_confidence = max(0, min(100, int(float(model_confidence or 0))))
    except (TypeError, ValueError):
        stated_confidence = 0
    model_note = clean_asset_text(model_evidence, 240)

    # A model-only NSFW decision is deliberately routed to a human; only high-precision
    # deterministic evidence may automatically reject a listing.
    if label in {"nsfw", "adult", "explicit"}:
        model_signal = f"model:nsfw:{model_note}" if model_note else "model:nsfw"
        return ContentSafetyAssessment(
            "needs_review",
            max(75, stated_confidence),
            max(70, stated_confidence),
            "model_detected_adult_context",
            (weak + [model_signal])[:12],
            "model_and_rules",
        )
    if label in {"uncertain", "needs_review", "unknown"}:
        return ContentSafetyAssessment(
            "needs_review",
            max(45, 40 + len(weak) * 12),
            max(60, stated_confidence),
            "content_safety_uncertain",
            (weak + ([f"model:uncertain:{model_note}"] if model_note else []))[:12],
            "model_and_rules",
        )
    if weak and not safe_context:
        return ContentSafetyAssessment(
            "needs_review",
            min(85, 40 + len(weak) * 12),
            72,
            "ambiguous_adult_context",
            weak[:12],
            "deterministic_rules",
        )
    if label == "safe" and stated_confidence >= 70:
        return ContentSafetyAssessment(
            "safe",
            15 if weak else 5,
            stated_confidence,
            "safe_context_disambiguated" if weak else "model_confirmed_safe",
            weak[:12],
            "model_and_rules",
        )
    if weak and safe_context:
        return ContentSafetyAssessment(
            "safe", 15, 82, "safe_context_disambiguated", weak[:12], "deterministic_rules"
        )
    return ContentSafetyAssessment(
        "needs_review",
        25,
        max(50, stated_confidence),
        "insufficient_content_safety_evidence",
        [],
        "deterministic_rules",
    )


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

