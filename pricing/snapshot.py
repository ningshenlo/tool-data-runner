"""Content-addressed snapshot artifact and extraction-skip decisions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable


ARTIFACT_TYPES = frozenset(
    {"html", "rendered_html", "text", "structured_data", "dom_map", "screenshot"}
)
RETENTION_CLASSES = frozenset(
    {
        "published_evidence",
        "review_evidence",
        "gold_evidence",
        "changed_snapshot",
        "diagnostic",
        "ephemeral",
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotArtifact:
    artifact_type: str
    content_hash: str
    object_key: str
    byte_size: int
    content_type: str
    retention_class: str
    state_key: str = "default"


@dataclass(frozen=True, slots=True)
class SnapshotCapturePlan:
    region_changed: bool
    run_extraction: bool
    capture_screenshot: bool
    upload_artifacts: tuple[SnapshotArtifact, ...]


def build_snapshot_artifact(
    artifact_type: str,
    body: bytes | str,
    *,
    content_type: str,
    retention_class: str,
    state_key: str = "default",
) -> SnapshotArtifact:
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"unknown pricing snapshot artifact type: {artifact_type}")
    if retention_class not in RETENTION_CLASSES:
        raise ValueError(f"unknown pricing snapshot retention class: {retention_class}")
    normalized_body = body.encode("utf-8") if isinstance(body, str) else body
    digest = hashlib.sha256(normalized_body).hexdigest()
    return SnapshotArtifact(
        artifact_type=artifact_type,
        content_hash=digest,
        object_key=f"pricing/artifacts/{artifact_type}/{digest}",
        byte_size=len(normalized_body),
        content_type=content_type,
        retention_class=retention_class,
        state_key=state_key or "default",
    )


def plan_snapshot_capture(
    artifacts: Iterable[SnapshotArtifact],
    *,
    previous_region_hash: str | None,
    current_region_hash: str | None,
    existing_artifact_keys: Iterable[str] = (),
    pipeline_version_changed: bool = False,
    visual_fallback_required: bool = False,
) -> SnapshotCapturePlan:
    region_changed = not previous_region_hash or previous_region_hash != current_region_hash
    run_extraction = region_changed or pipeline_version_changed
    capture_screenshot = region_changed or visual_fallback_required
    existing = frozenset(existing_artifact_keys)
    upload_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.object_key not in existing
        and (artifact.artifact_type != "screenshot" or capture_screenshot)
    )
    return SnapshotCapturePlan(
        region_changed=region_changed,
        run_extraction=run_extraction,
        capture_screenshot=capture_screenshot,
        upload_artifacts=upload_artifacts,
    )
