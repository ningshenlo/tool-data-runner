"""Route production D1 through the shared budget controller, with no direct fallback."""
import os
import time

PRODUCTION_DATABASE = "3025073a-9e43-4000-b5c9-9e975ed27b7b"
GUARD_URL = "https://sigpik-cost-control.fengmozc.workers.dev/query"
_paused_until = 0.0


def guard_endpoint(direct_url: str) -> str:
    required = os.getenv("CLOUDFLARE_D1_GUARD_REQUIRED", "0").lower() in {"1", "true"}
    configured = os.getenv("CLOUDFLARE_D1_GUARD_URL", "").strip()
    production = f"/database/{PRODUCTION_DATABASE}/" in direct_url
    if configured and configured != GUARD_URL:
        raise ValueError("D1 guard URL must match the Sigpik account endpoint")
    if required and not production:
        raise ValueError("D1 guard is only configured for the production ainav database")
    return GUARD_URL if production or required else direct_url


def before_request(url: str) -> None:
    if url == GUARD_URL and time.monotonic() < _paused_until:
        raise RuntimeError("D1 cost guard paused: waiting before checking operator recovery")


def observe_response(url: str, response) -> bool:
    """A budget rejection is terminal for this attempt, never an immediate retry."""
    global _paused_until
    if url != GUARD_URL:
        return False
    if response.status_code in {401, 403, 429, 502, 503, 504}:
        _paused_until = time.monotonic() + 300
        return True
    return False
