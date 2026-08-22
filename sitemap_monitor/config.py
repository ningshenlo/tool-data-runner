from __future__ import annotations

from dataclasses import dataclass


MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MonitorLimits:
    """Hard safety boundaries for one sitemap resource."""

    max_download_bytes: int = 20 * MIB
    max_decompressed_bytes: int = 50 * MIB
    max_url_count: int = 50_000
    max_index_depth: int = 5
    max_resources_per_site: int = 500
    max_url_length: int = 8_192
    request_timeout_seconds: float = 20.0
    robots_max_bytes: int = 1 * MIB
    redirect_limit: int = 5

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_download_bytes,
            self.max_decompressed_bytes,
            self.max_url_count,
            self.max_index_depth,
            self.max_resources_per_site,
            self.max_url_length,
            self.robots_max_bytes,
            self.redirect_limit,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("Sitemap monitor limits must be positive.")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
