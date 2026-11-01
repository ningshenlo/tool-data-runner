from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class TrafficTask:
    normalized_domain: str
    traffic_month: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str

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
