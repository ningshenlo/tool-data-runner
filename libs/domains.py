"""Hostname / registrable-domain helpers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from config.constants import COMMON_THREE_LABEL_SUFFIXES

PAAS_SHARED_HOSTS = frozenset({
    "vercel.app",
    "lovable.app",
    "polsia.app",
    "netlify.app",
    "pages.dev",
    "web.app",
    "firebaseapp.com",
    "herokuapp.com",
    "streamlit.app",
    "hf.space",
    "github.io",
    "workers.dev",
    "onrender.com",
    "railway.app",
    "fly.dev",
    "replit.app",
    "framer.website",
    "framer.ai",
    "webflow.io",
    "notion.site",
})

MULTI_PRODUCT_ROOT_DOMAINS = frozenset({
    "google.com",
    "microsoft.com",
    "cloud.microsoft",
    "adobe.com",
    "capcut.com",
    "meta.com",
    "amazon.com",
    "aws.amazon.com",
    "cloudflare.com",
    "bytedance.com",
    "apple.com",
    "openai.com",
})

GENERIC_SUBDOMAIN_PREFIXES = frozenset({
    "www",
    "app",
    "apps",
    "chat",
    "web",
    "my",
    "portal",
    "dashboard",
    "console",
    "studio",
    "play",
    "beta",
    "alpha",
    "try",
    "get",
    "go",
    "use",
    "labs",
    "lab",
    "ai",
    "cloud",
    "api",
    "docs",
    "help",
    "support",
    "blog",
    "news",
    "login",
    "auth",
    "account",
    "accounts",
    "home",
    "start",
    "m",
    "en",
    "zh",
    "cn",
})

_UPPER_BRAND_ACRONYMS = {
    "id": "ID",
}


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
    labels = [part for part in (domain or "").split(".") if part.strip()]
    if len(labels) >= 3 and ".".join(labels[-2:]) in COMMON_THREE_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


def normalize_rdap_domain(value: str) -> str:
    domain = normalize_domain(value)
    if not domain:
        return ""
    return get_registrable_domain(domain)


def extract_domain_brand_parts(hostname: str) -> tuple[str, str]:
    """Return ``(brand_stem, tld_suffix)`` using Scheme A domain extraction rules."""
    host = normalize_domain(hostname) or str(hostname or "").strip().lower().removeprefix("www.")
    if not host:
        return ("tool", "com")

    for paas_host in PAAS_SHARED_HOSTS:
        suffix = f".{paas_host}"
        if host.endswith(suffix):
            sub_labels = [p for p in host[: -len(suffix)].split(".") if p]
            while len(sub_labels) > 1 and sub_labels[0] in GENERIC_SUBDOMAIN_PREFIXES:
                sub_labels.pop(0)
            stem = "-".join(sub_labels) or paas_host.split(".", 1)[0]
            return (stem, paas_host.replace(".", "-"))

    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return (host, "com")

    if len(labels) >= 3 and ".".join(labels[-2:]) in COMMON_THREE_LABEL_SUFFIXES:
        suffix_count = 2
    else:
        suffix_count = 1

    public_suffix_parts = labels[-suffix_count:]
    root_stem = labels[-(suffix_count + 1)]
    registrable = ".".join([root_stem, *public_suffix_parts])
    public_suffix = "-".join(public_suffix_parts)
    sub_labels = labels[: -(suffix_count + 1)]
    non_generic_subs = [label for label in sub_labels if label not in GENERIC_SUBDOMAIN_PREFIXES]

    is_multi_product = registrable in MULTI_PRODUCT_ROOT_DOMAINS or host.endswith(".aws.amazon.com")
    if is_multi_product and non_generic_subs:
        if root_stem in GENERIC_SUBDOMAIN_PREFIXES:
            return ("-".join(non_generic_subs), public_suffix or "com")
        sub_stem = "-".join(non_generic_subs)
        stem = sub_stem if root_stem in non_generic_subs else f"{root_stem}-{sub_stem}"
        return (stem, public_suffix or "com")

    if root_stem in GENERIC_SUBDOMAIN_PREFIXES and non_generic_subs:
        return ("-".join(non_generic_subs), public_suffix or "com")

    return (root_stem, public_suffix or "com")


def format_domain_brand_name(hostname: str, *, include_tld: bool = False) -> str:
    """Format the Scheme A display name from a hostname, optionally appending ``.tld``."""
    stem, tld = extract_domain_brand_parts(hostname)
    raw_parts = [part for part in re.split(r"[-_]+", stem) if part]
    if not raw_parts:
        base = "Tool"
    elif (
        "-" in stem
        and len(raw_parts) == 2
        and len(raw_parts[0]) == 1
        and len(raw_parts[1]) <= 3
    ):
        base = "-".join(
            _UPPER_BRAND_ACRONYMS.get(part.lower(), part.upper() if len(part) <= 2 else part.capitalize())
            for part in raw_parts
        )
    else:
        base = " ".join(part[:1].upper() + part[1:] for part in raw_parts)
    base = base[:120] or "Tool"
    if include_tld and tld:
        tld_dot = tld.replace("-", ".")
        return f"{base}.{tld_dot}"[:120]
    return base
