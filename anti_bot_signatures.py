"""Shared WAF, anti-bot, and human-verification page signatures.

The asset and taxonomy pipelines must classify these pages before extracting
product facts.  A matched challenge page is evidence about the fetch, never
evidence about the product itself.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AntiBotSignature:
    code: str
    provider: str
    state: str
    weight: int
    pattern: re.Pattern[str]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class AntiBotDetection:
    state: str
    provider: str
    code: str
    evidence: str
    confidence: float
    matched_codes: tuple[str, ...]


def _signature(
    code: str,
    provider: str,
    state: str,
    weight: int,
    pattern: str,
    *scopes: str,
) -> AntiBotSignature:
    return AntiBotSignature(
        code=code,
        provider=provider,
        state=state,
        weight=weight,
        pattern=re.compile(pattern, re.I),
        scopes=tuple(scopes) or ("body",),
    )


ANTI_BOT_SIGNATURES: tuple[AntiBotSignature, ...] = (
    # Cloudflare challenge and block pages.
    _signature("cf_challenge_platform", "cloudflare", "anti_bot", 100, r"/cdn-cgi/challenge-platform|\bcf[-_]chl[-_]", "html"),
    _signature("cf_ray_id", "cloudflare", "access_denied", 92, r"\bcloudflare\s+ray\s+id\b", "body", "html"),
    _signature("cf_just_a_moment", "cloudflare", "anti_bot", 94, r"\bjust\s+(?:a\s+moment|wait)(?:\.{2,})?\b", "title", "body"),
    _signature("cf_checking_browser", "cloudflare", "anti_bot", 96, r"\bchecking\s+(?:your\s+)?browser(?:\s+before\s+accessing)?\b", "title", "body"),
    _signature("cf_blocked", "cloudflare", "access_denied", 98, r"\bsorry[,.\s]+you\s+have\s+been\s+blocked\b", "title", "body"),
    _signature("cf_security_service", "cloudflare", "access_denied", 94, r"\bthis\s+website\s+is\s+using\s+a\s+security\s+service\s+to\s+protect\s+itself\b", "body"),
    _signature("cf_enable_cookies", "cloudflare", "captcha", 90, r"\benable\s+javascript\s+and\s+cookies\s+to\s+continue\b", "body"),
    _signature("cf_attention_required", "cloudflare", "access_denied", 88, r"\battention\s+required\b", "title"),
    # Generic firewall / WAF text, including the Janitorai failure mode.
    _signature("waf_access_blocked_firewall", "generic_waf", "access_denied", 100, r"\baccess\s+has\s+been\s+blocked\s+by\s+(?:the\s+)?firewall\b", "title", "body"),
    _signature("waf_request_denied_security", "generic_waf", "access_denied", 94, r"\b(?:your\s+)?request\s+has\s+been\s+denied\s+for\s+security\s+reasons\b", "body"),
    _signature("waf_access_page_denied", "generic_waf", "access_denied", 92, r"\baccess\s+to\s+this\s+page\s+has\s+been\s+denied\b", "body"),
    _signature("waf_request_blocked", "generic_waf", "access_denied", 86, r"\b(?:the\s+)?request\s+(?:was|has\s+been)\s+blocked\b", "body"),
    _signature("waf_access_denied_title", "generic_waf", "access_denied", 96, r"^\s*(?:access\s+denied|forbidden|unauthorized|error\s*403)\b", "title"),
    # Human verification variants shared by several vendors.
    _signature("human_verify", "generic_challenge", "captcha", 92, r"\b(?:verify|verifying)\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human\b", "title", "body"),
    _signature("human_security_check", "generic_challenge", "captcha", 88, r"\bcomplete\s+the\s+security\s+check\b", "body"),
    _signature("human_wait_verify", "generic_challenge", "captcha", 90, r"\bplease\s+wait\s+while\s+we\s+(?:verify|check)(?:\s+your\s+browser)?\b", "body"),
    _signature("human_press_hold", "human_perimeterx", "captcha", 98, r"\bpress\s*(?:&|and)\s*hold\s+to\s+confirm\s+you\s+are\s+(?:a\s+)?human\b", "body"),
    _signature("perimeterx_marker", "human_perimeterx", "captcha", 100, r"\bpx-captcha\b|\b_pxhd\b", "html"),
    # Akamai.
    _signature("akamai_marker", "akamai", "anti_bot", 100, r"\bakamai(?:g?host|\s+bot\s+manager)\b", "html"),
    _signature("akamai_permission", "akamai", "access_denied", 94, r"\byou\s+do(?:n't|\s+not)\s+have\s+permission\s+to\s+access\b", "body"),
    _signature("akamai_reference", "akamai", "access_denied", 78, r"\breference\s*#[0-9a-f.:-]{6,}\b", "body"),
    # Imperva / Incapsula.
    _signature("incapsula_marker", "imperva", "anti_bot", 100, r"_incapsula_resource|imperva\s+captcha", "html"),
    _signature("incapsula_incident", "imperva", "access_denied", 98, r"\bincapsula\s+incident\s+id\b|\bpowered\s+by\s+imperva\b", "body", "html"),
    _signature("imperva_unsuccessful", "imperva", "access_denied", 88, r"\brequest\s+unsuccessful\b", "title", "body"),
    # DataDome.
    _signature("datadome_marker", "datadome", "captcha", 100, r"datadome-captcha|captcha-delivery\.com", "html"),
    _signature("datadome_enable_js", "datadome", "captcha", 94, r"\bplease\s+enable\s+js\s+and\s+disable\s+any\s+ad\s+blocker\b", "body"),
    # AWS WAF / CloudFront.
    _signature("cloudfront_unsatisfied", "aws_cloudfront", "access_denied", 96, r"\bthe\s+request\s+could\s+not\s+be\s+satisfied\b", "body"),
    _signature("cloudfront_generated", "aws_cloudfront", "access_denied", 82, r"\bgenerated\s+by\s+cloudfront\b", "body"),
    # Sucuri.
    _signature("sucuri_firewall", "sucuri", "access_denied", 100, r"\bsucuri\s+website\s+firewall\b|\bwebsite\s+firewall\s*[-:]\s*access\s+denied\b", "title", "body", "html"),
    # Rate limits are retriable anti-bot states, not product content.
    _signature("rate_limited", "generic_waf", "anti_bot", 84, r"\btoo\s+many\s+requests\b|\brate\s+limit(?:ed|\s+exceeded)?\b", "title", "body"),
)


def _visible_text(html_body: str, limit: int = 20000) -> str:
    value = re.sub(r"<(?:script|style)\b[\s\S]*?</(?:script|style)>", " ", html_body or "", flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()[:limit]


def _iter_matches(
    *,
    html_body: str,
    page_title: str,
    visible_text: str,
) -> Iterable[tuple[AntiBotSignature, str]]:
    sources = {"html": html_body or "", "title": page_title or "", "body": visible_text or ""}
    for signature in ANTI_BOT_SIGNATURES:
        for scope in signature.scopes:
            match = signature.pattern.search(sources.get(scope, ""))
            if match:
                evidence = re.sub(r"\s+", " ", html.unescape(match.group(0))).strip()
                yield signature, evidence[:240]
                break


def detect_anti_bot_page(
    html_body: str,
    *,
    page_title: str = "",
    http_status: int | None = None,
) -> AntiBotDetection | None:
    title = page_title.strip()
    visible = _visible_text(html_body)
    matches = list(_iter_matches(html_body=html_body, page_title=title, visible_text=visible))

    if http_status in {401, 403}:
        matches.append(
            (
                _signature(f"http_{http_status}", "http", "access_denied", 100, r"$", "title"),
                f"HTTP {http_status}",
            )
        )
    elif http_status == 429:
        matches.append(
            (_signature("http_429", "http", "anti_bot", 100, r"$", "title"), "HTTP 429")
        )

    if not matches:
        return None

    matches.sort(key=lambda item: (-item[0].weight, item[0].code))
    primary, evidence = matches[0]
    return AntiBotDetection(
        state=primary.state,
        provider=primary.provider,
        code=primary.code,
        evidence=evidence,
        confidence=min(0.99, max(0.5, primary.weight / 100.0)),
        matched_codes=tuple(dict.fromkeys(item[0].code for item in matches)),
    )


def detect_anti_bot_text(value: str) -> AntiBotDetection | None:
    """Detect stored title/description/profile text without requiring HTML."""
    return detect_anti_bot_page(value or "", page_title=value or "")


def contains_anti_bot_text(value: str) -> bool:
    return detect_anti_bot_text(value) is not None
