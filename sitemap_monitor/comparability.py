from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import (
    ComparabilityResult,
    ComparabilityStatus,
    SiteScanResource,
    SiteScanResult,
    SiteScanSnapshot,
    StoredSiteScan,
)


COMPARABILITY_POLICY_VERSION = "sitemap-comparability-v1"
_PARTITION_SUFFIX = re.compile(
    r"(?i)(?P<prefix>.*sitemap[^/?#]*?)(?P<number>\d+)(?P<suffix>\.xml(?:\.gz)?|\.txt)?$"
)


@dataclass(frozen=True, slots=True)
class ComparabilityPolicy:
    migration_url_ratio_min: float = 0.80
    migration_url_ratio_max: float = 1.25
    policy_version: str = COMPARABILITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.migration_url_ratio_min <= 0:
            raise ValueError("migration_url_ratio_min must be positive.")
        if self.migration_url_ratio_max < self.migration_url_ratio_min:
            raise ValueError("migration_url_ratio_max cannot be below the minimum.")
        if not self.policy_version.strip():
            raise ValueError("policy_version is required.")


def _stable_hash(values: tuple[str, ...]) -> str:
    payload = "\0".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_resource_family(url: str) -> str:
    """Collapse common numbered sitemap partitions into a stable logical family."""

    parsed = urlsplit(url)
    path = parsed.path.lower() or "/"
    match = _PARTITION_SUFFIX.match(path)
    if match is not None:
        path = f"{match.group('prefix')}:n{match.group('suffix') or ''}"
    if len(path) > 512:
        path = f"{path[:480]}#{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}"
    return path


def build_site_scan_snapshot(result: SiteScanResult) -> SiteScanSnapshot:
    relevant_outcomes = tuple(
        outcome
        for outcome in result.outcomes
        if result.discovery_mode != "fallback"
        or outcome.result != "failed"
        or outcome.parent_id is not None
    )
    successful = tuple(
        outcome for outcome in relevant_outcomes if outcome.result != "failed"
    )
    resources = tuple(
        sorted(
            (
                SiteScanResource(
                    resource_family=normalize_resource_family(outcome.url),
                    resource_id=outcome.resource_id,
                    sitemap_kind=outcome.sitemap_kind or "urlset",
                    state_key=outcome.state_key,
                    url=outcome.url,
                    url_count=outcome.url_count or 0,
                )
                for outcome in successful
            ),
            key=lambda item: (item.url, item.resource_id),
        )
    )
    raw_urls = tuple(item.url for item in resources)
    family_counts = Counter(item.resource_family for item in resources)
    normalized_families = tuple(sorted(family_counts))
    attempted_count = len(relevant_outcomes)
    successful_count = len(resources)
    successful_ratio = successful_count / attempted_count if attempted_count else 0.0
    url_count = sum(
        item.url_count for item in resources if item.sitemap_kind != "sitemap_index"
    )
    return SiteScanSnapshot(
        attempted_resource_count=attempted_count,
        discovery_mode=result.discovery_mode,
        finished_at_ms=result.finished_at_ms,
        normalized_resource_set_hash=_stable_hash(normalized_families),
        raw_resource_set_hash=_stable_hash(raw_urls),
        resource_family_counts=tuple(sorted(family_counts.items())),
        resources=resources,
        scan_id=result.scan_id,
        site_id=result.site_id,
        started_at_ms=result.started_at_ms,
        successful_resource_count=successful_count,
        successful_resource_ratio=successful_ratio,
        traversal_complete=result.traversal_complete,
        traversal_reason_codes=result.traversal_reason_codes,
        url_count=url_count,
    )


def _family_coverage(current: SiteScanSnapshot, baseline: SiteScanSnapshot) -> float:
    expected = {family for family, _ in baseline.resource_family_counts}
    if not expected:
        return 1.0
    present = {family for family, _ in current.resource_family_counts}
    return len(expected & present) / len(expected)


def _family_overlap(current: SiteScanSnapshot, baseline: SiteScanSnapshot) -> float:
    before = {family for family, _ in baseline.resource_family_counts}
    after = {family for family, _ in current.resource_family_counts}
    union = before | after
    return len(before & after) / len(union) if union else 1.0


def _url_ratio(current: SiteScanSnapshot, baseline: SiteScanSnapshot) -> float:
    if baseline.url_count <= 0:
        return 1.0 if current.url_count <= 0 else float("inf")
    return current.url_count / baseline.url_count


def assess_comparability(
    current: SiteScanSnapshot,
    *,
    baseline: StoredSiteScan | None,
    previous: StoredSiteScan | None,
    policy: ComparabilityPolicy | None = None,
) -> ComparabilityResult:
    active_policy = policy or ComparabilityPolicy()
    before = baseline.snapshot if baseline is not None else None
    reasons = list(current.traversal_reason_codes)
    coverage_ratio = (
        min(_family_coverage(current, before), current.successful_resource_ratio)
        if before is not None
        else current.successful_resource_ratio
    )
    status: ComparabilityStatus
    promote = False

    if current.successful_resource_count == 0:
        status = "fetch_incomplete"
        reasons.append("no_successful_resource")
    elif not current.traversal_complete:
        status = "fetch_incomplete"
        reasons.append("traversal_incomplete")
    elif current.successful_resource_count < current.attempted_resource_count:
        status = "partial"
        reasons.append("resource_fetch_failed")
    elif before is None:
        status = "baseline_invalid"
        reasons.append("semantic_baseline_missing")
        promote = True
    elif current.normalized_resource_set_hash == before.normalized_resource_set_hash:
        status = "comparable"
        promote = True
        if current.raw_resource_set_hash != before.raw_resource_set_hash:
            reasons.append("dynamic_resource_partition_change")
    else:
        ratio = _url_ratio(current, before)
        overlap = _family_overlap(current, before)
        if (
            overlap < 0.5
            and active_policy.migration_url_ratio_min
            <= ratio
            <= active_policy.migration_url_ratio_max
        ):
            status = "possible_migration"
            reasons.append("resource_family_replacement")
        else:
            status = "resource_set_changed"
            reasons.append("normalized_resource_set_changed")

        if (
            previous is not None
            and previous.snapshot.scan_id != before.scan_id
            and previous.status == status
            and previous.snapshot.complete
            and previous.snapshot.normalized_resource_set_hash
            == current.normalized_resource_set_hash
        ):
            promote = True
            reasons.append("resource_set_stable_confirmation")

    return ComparabilityResult(
        baseline_scan_id=before.scan_id if before is not None else None,
        coverage_ratio=coverage_ratio,
        is_comparable=status == "comparable",
        normalized_resource_set_hash_after=current.normalized_resource_set_hash,
        normalized_resource_set_hash_before=(
            before.normalized_resource_set_hash if before is not None else None
        ),
        policy_version=active_policy.policy_version,
        promote_semantic_baseline=promote,
        raw_resource_set_hash_after=current.raw_resource_set_hash,
        raw_resource_set_hash_before=(
            before.raw_resource_set_hash if before is not None else None
        ),
        reason_codes=tuple(dict.fromkeys(reasons)),
        resource_count_after=current.successful_resource_count,
        resource_count_before=before.successful_resource_count if before is not None else 0,
        status=status,
        successful_resource_ratio=current.successful_resource_ratio,
        url_count_after=current.url_count,
        url_count_before=before.url_count if before is not None else 0,
    )


def resource_manifest_json(snapshot: SiteScanSnapshot) -> str:
    return json.dumps(
        [
            {
                "family": item.resource_family,
                "id": item.resource_id,
                "kind": item.sitemap_kind,
                "state_key": item.state_key,
                "url_count": item.url_count,
            }
            for item in snapshot.resources
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def resource_family_counts_json(snapshot: SiteScanSnapshot) -> str:
    return json.dumps(
        dict(snapshot.resource_family_counts),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stored_site_scan_from_row(row: Mapping[str, object]) -> StoredSiteScan:
    manifest_value = row.get("resource_manifest_json") or "[]"
    manifest = json.loads(str(manifest_value))
    resources = tuple(
        SiteScanResource(
            resource_family=str(item["family"]),
            resource_id=str(item["id"]),
            sitemap_kind=item["kind"],
            state_key=str(item["state_key"]) if item.get("state_key") is not None else None,
            url=str(item.get("url") or ""),
            url_count=int(item["url_count"]),
        )
        for item in manifest
    )
    family_counts_value = row.get("resource_family_counts_json") or "{}"
    family_counts = json.loads(str(family_counts_value))
    traversal_reasons = json.loads(str(row.get("traversal_reason_codes_json") or "[]"))
    snapshot = SiteScanSnapshot(
        attempted_resource_count=int(row["attempted_resource_count"]),
        discovery_mode=row["discovery_mode"],
        finished_at_ms=int(row["finished_at"]),
        normalized_resource_set_hash=str(row["normalized_resource_set_hash_after"]),
        raw_resource_set_hash=str(row["raw_resource_set_hash_after"]),
        resource_family_counts=tuple(
            sorted((str(key), int(value)) for key, value in family_counts.items())
        ),
        resources=resources,
        scan_id=str(row["id"]),
        site_id=str(row["site_id"]),
        started_at_ms=int(row["started_at"]),
        successful_resource_count=int(row["successful_resource_count"]),
        successful_resource_ratio=float(row["successful_resource_ratio"]),
        traversal_complete=bool(row["traversal_complete"]),
        traversal_reason_codes=tuple(str(value) for value in traversal_reasons),
        url_count=int(row["url_count_after"]),
    )
    return StoredSiteScan(
        is_comparable=bool(row["is_comparable"]),
        promoted_semantic_baseline=bool(row["promoted_semantic_baseline"]),
        snapshot=snapshot,
        status=row["comparability_status"],
    )
