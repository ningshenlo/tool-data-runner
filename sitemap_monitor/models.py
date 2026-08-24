from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SitemapKind = Literal["sitemap_index", "text", "urlset"]
RunResult = Literal[
    "baseline",
    "changed",
    "failed",
    "not_modified",
    "semantic_unchanged",
]
JobStatus = Literal["pending", "running", "retry", "succeeded", "dead"]
SiteStatus = Literal["active", "paused", "blocked"]
DiscoveryMode = Literal["explicit", "fallback", "robots"]
ComparabilityStatus = Literal[
    "baseline_invalid",
    "comparable",
    "fetch_incomplete",
    "partial",
    "possible_migration",
    "resource_set_changed",
]


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    normalized_url: str
    raw_url: str
    url_hash: str
    lastmod: str | None = None


@dataclass(frozen=True, slots=True)
class SitemapDocument:
    entries: tuple[SitemapEntry, ...]
    kind: SitemapKind
    source_url: str
    raw_content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class FingerprintedDocument:
    content_hash: str
    entries: tuple[SitemapEntry, ...]
    kind: SitemapKind
    metadata_hash: str
    source_url: str
    urlset_hash: str

    @property
    def url_count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class DiffResult:
    added: tuple[SitemapEntry, ...]
    modified: tuple[SitemapEntry, ...]
    removed: tuple[SitemapEntry, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.removed)


@dataclass(frozen=True, slots=True)
class FetchValidators:
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    body: bytes | None
    bytes_downloaded: int
    content_type: str | None
    etag: str | None
    final_url: str
    last_modified: str | None
    retry_after: str | None
    status_code: int


@dataclass(frozen=True, slots=True)
class ResourceState:
    content_hash: str
    current_state_key: str
    etag: str | None
    last_modified: str | None
    metadata_hash: str
    resource_id: str
    sitemap_kind: SitemapKind
    url: str
    url_count: int
    urlset_hash: str


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    added_count: int
    bytes_downloaded: int
    diff_key: str | None
    error_code: str | None
    final_url: str
    http_status: int | None
    modified_count: int
    resource_id: str
    result: RunResult
    sitemap_kind: SitemapKind | None
    state_key: str | None
    url: str
    url_count: int | None
    removed_count: int
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class SiteScanResult:
    homepage_url: str
    outcomes: tuple[CheckOutcome, ...]
    site_id: str
    discovery_mode: DiscoveryMode = "fallback"
    finished_at_ms: int = 0
    scan_id: str = ""
    started_at_ms: int = 0
    traversal_complete: bool = True
    traversal_reason_codes: tuple[str, ...] = ()

    @property
    def changed_resources(self) -> int:
        return sum(outcome.result == "changed" for outcome in self.outcomes)

    @property
    def successful(self) -> bool:
        """A site scan succeeds once at least one usable sitemap was checked.

        Common-path discovery is intentionally allowed to produce 404 failures
        before it finds the site's actual sitemap, so an all-outcomes check is
        too strict for site-level scheduling.
        """

        return any(outcome.result != "failed" for outcome in self.outcomes)


@dataclass(frozen=True, slots=True)
class DueSite:
    check_interval_sec: int
    dispatch_lease_token: str
    homepage_url: str
    schedule_version: int
    scheduled_for: int
    site_id: str


@dataclass(frozen=True, slots=True)
class RegisteredSite:
    check_interval_sec: int
    homepage_url: str
    site_id: str
    status: SiteStatus


@dataclass(frozen=True, slots=True)
class SitemapJob:
    attempts: int
    base_error_streak: int
    check_interval_sec: int
    homepage_url: str
    idempotency_key: str
    job_id: str
    lease_token: str
    max_attempts: int
    schedule_version: int
    scheduled_for: int
    site_id: str
    status: JobStatus


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    expired_jobs: int = 0
    pruned_jobs: int = 0
    pruned_runs: int = 0
    pruned_scans: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.expired_jobs,
                self.pruned_jobs,
                self.pruned_runs,
                self.pruned_scans,
            )
        )


@dataclass(frozen=True, slots=True)
class SiteScanResource:
    resource_family: str
    resource_id: str
    sitemap_kind: SitemapKind
    state_key: str | None
    url: str
    url_count: int


@dataclass(frozen=True, slots=True)
class SiteScanSnapshot:
    attempted_resource_count: int
    discovery_mode: DiscoveryMode
    finished_at_ms: int
    normalized_resource_set_hash: str
    raw_resource_set_hash: str
    resource_family_counts: tuple[tuple[str, int], ...]
    resources: tuple[SiteScanResource, ...]
    scan_id: str
    site_id: str
    started_at_ms: int
    successful_resource_count: int
    successful_resource_ratio: float
    traversal_complete: bool
    traversal_reason_codes: tuple[str, ...]
    url_count: int

    @property
    def complete(self) -> bool:
        return (
            self.traversal_complete
            and self.successful_resource_count > 0
            and self.successful_resource_count == self.attempted_resource_count
        )


@dataclass(frozen=True, slots=True)
class StoredSiteScan:
    is_comparable: bool
    promoted_semantic_baseline: bool
    snapshot: SiteScanSnapshot
    status: ComparabilityStatus


@dataclass(frozen=True, slots=True)
class ComparabilityResult:
    baseline_scan_id: str | None
    coverage_ratio: float
    is_comparable: bool
    normalized_resource_set_hash_after: str
    normalized_resource_set_hash_before: str | None
    policy_version: str
    promote_semantic_baseline: bool
    raw_resource_set_hash_after: str
    raw_resource_set_hash_before: str | None
    reason_codes: tuple[str, ...]
    resource_count_after: int
    resource_count_before: int
    status: ComparabilityStatus
    successful_resource_ratio: float
    url_count_after: int
    url_count_before: int
