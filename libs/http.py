"""HTTP / browser request helpers and pacing."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from config.constants import (
    AHREFS_MAX_REQUESTS_PER_MINUTE,
    BROWSER_USER_AGENT,
)

try:
    from fake_useragent import UserAgent
except ImportError:
    UserAgent = None

_BROWSER_UA_GENERATOR: Any | None = None


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


def random_browser_user_agent() -> str:
    global _BROWSER_UA_GENERATOR
    if UserAgent is not None:
        try:
            if _BROWSER_UA_GENERATOR is None:
                _BROWSER_UA_GENERATOR = UserAgent(
                    browsers=["Chrome", "Edge"],
                    platforms=["desktop"],
                    fallback=BROWSER_USER_AGENT,
                )
            user_agent = _BROWSER_UA_GENERATOR.random
            if user_agent:
                return str(user_agent)
        except Exception:
            pass
    return BROWSER_USER_AGENT


def browser_request_headers(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "User-Agent": random_browser_user_agent(),
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


class AsyncRequestPacer:
    """Space request starts evenly so a concurrent batch cannot burst the provider."""

    def __init__(self, requests_per_minute: int) -> None:
        bounded_rate = min(
            AHREFS_MAX_REQUESTS_PER_MINUTE,
            max(1, int(requests_per_minute)),
        )
        self.requests_per_minute = bounded_rate
        self.interval_seconds = 60.0 / bounded_rate
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            scheduled_at = max(now, self._next_request_at)
            self._next_request_at = scheduled_at + self.interval_seconds
            delay = scheduled_at - now
        if delay > 0:
            await asyncio.sleep(delay)


def parse_retry_after_seconds(value: str | None, default: float = 60.0) -> float:
    if not value:
        return default
    try:
        return max(1.0, float(value.strip()))
    except (TypeError, ValueError):
        return default
