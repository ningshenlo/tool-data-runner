from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class DomainStateTask:
    normalized_domain: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str
    fetch_domain_rating: bool
    fetch_rdap: bool

@dataclass(frozen=True)
class DomainStateResult:
    status: str
    domain_rating: float | None
    domain_created_at: str | None
    error: str | None = None
    rdap_status: str | None = None
    rdap_error: str | None = None
    retry_after_seconds: float | None = None
