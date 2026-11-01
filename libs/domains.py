"""Hostname / registrable-domain helpers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from config.constants import COMMON_THREE_LABEL_SUFFIXES


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
