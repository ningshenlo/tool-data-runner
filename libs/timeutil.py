"""UTC timestamp helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_delta(**kwargs: Any) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
