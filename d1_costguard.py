"""Route production D1 through the shared budget controller, with no direct fallback."""
import os
import time
import math

PRODUCTION_DATABASE = "3025073a-9e43-4000-b5c9-9e975ed27b7b"
GUARD_URL = "https://sigpik-cost-control.fengmozc.workers.dev/query"
_paused_until = 0.0
_pauses = {}


class CostGuardPause(RuntimeError):
    def __init__(self, code, retry_seconds, recovery):
        self.code = code
        self.retry_seconds = retry_seconds
        self.recovery = recovery
        super().__init__(f"D1 cost guard: {code}; retry in {retry_seconds}s; recovery={recovery}")


def remaining_pause_seconds(service=None):
    key = service or "default"
    pause = _pauses.get(key)
    until = pause[0] if pause else (_paused_until if key == "default" else 0)
    return max(0, math.ceil(until - time.monotonic()))


def pause_error(service=None):
    pause = _pauses.get(service or "default")
    return CostGuardPause(pause[1] if pause else "budget_paused", max(1, remaining_pause_seconds(service)), pause[2] if pause else "operator")


def guard_endpoint(direct_url: str) -> str:
    required = os.getenv("CLOUDFLARE_D1_GUARD_REQUIRED", "0").lower() in {"1", "true"}
    configured = os.getenv("CLOUDFLARE_D1_GUARD_URL", "").strip()
    production = f"/database/{PRODUCTION_DATABASE}/" in direct_url
    if configured and configured != GUARD_URL:
        raise ValueError("D1 guard URL must match the Sigpik account endpoint")
    if required and not production:
        raise ValueError("D1 guard is only configured for the production ainav database")
    return GUARD_URL if production or required else direct_url


def before_request(url: str, service=None) -> None:
    if url == GUARD_URL and remaining_pause_seconds(service):
        raise pause_error(service)


def observe_response(url: str, response, service=None) -> bool:
    """A budget rejection is terminal for this attempt, never an immediate retry."""
    global _paused_until
    if url != GUARD_URL:
        return False
    if response.status_code in {401, 403, 429, 502, 503, 504}:
        try:
            payload = response.json()
        except (AttributeError, ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        errors = payload.get("errors") or []
        if not isinstance(errors, list):
            errors = []
        code = payload.get("error") or ((errors[0].get("code") or errors[0].get("message")) if errors and isinstance(errors[0], dict) else None)
        if response.status_code in {401, 403}:
            code, recovery, default_delay = "authentication_failed", "operator", 300
        elif response.status_code in {502, 503, 504}:
            code, recovery, default_delay = "guard_transport_unavailable", "automatic", 15
        else:
            code = str(code or "rate_limited")
            automatic = code in {"rate_limited", "settlement_pending", "concurrency_limited", "query_cooling_down", "expired_budget_lease"} or code.endswith("budget_exhausted")
            recovery, default_delay = ("automatic", 60) if automatic else ("operator", 300)
        try:
            delay = int(getattr(response, "headers", {}).get("Retry-After", payload.get("retryAfterSeconds", default_delay)))
        except (TypeError, ValueError):
            delay = default_delay
        delay = max(1, min(3600, delay))
        until = time.monotonic() + delay
        _pauses[service or "default"] = (until, str(code), recovery)
        if service is None:
            _paused_until = until
        return True
    return False
