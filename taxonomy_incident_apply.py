"""Freeze and apply an audited taxonomy incident rollback plan in D1.

Safety properties:

* requires a fresh dry-run hash match before the first write;
* never calls a model or creates a reclassification request;
* freezes the confirmed cohort before changing current state;
* applies at most 50 members in one atomic D1 batch;
* guards every current entity/profile/assignment effect before mutation;
* preserves immutable runs and copies deleted polluted profiles into audit JSON;
* is resumable and idempotent through incident/member/batch unique constraints.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import httpx

from runner import D1Client, load_config
from dry_run_non_product_classification import ReadOnlyD1Client
from taxonomy_incident_rollback import (
    DEFAULT_INCIDENT_ID,
    _stable_hash,
    build_rollback_plan,
    load_current_snapshot,
    load_frozen_cohort,
)


ALLOWED_ACTIONS = {
    "already_repaired_by_later_clean_run",
    "manual_protected_untouched",
    "restore_previous_clean_auto",
    "reset_entity_to_unresolved",
    "no_current_effect_audit_only",
}
AUDIT_ONLY_ACTIONS = {
    "already_repaired_by_later_clean_run",
    "manual_protected_untouched",
    "no_current_effect_audit_only",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("rollback plan must be a JSON object")
    return payload


def validate_plan(
    report: dict[str, Any],
    *,
    expected_incident_id: str,
    expected_plan_hash: str,
) -> None:
    if str(report.get("incident_id") or "") != expected_incident_id:
        raise RuntimeError("incident_id mismatch")
    if str(report.get("rollback_plan_hash") or "") != expected_plan_hash:
        raise RuntimeError("rollback plan hash mismatch")
    if not bool(report.get("apply_ready")):
        raise RuntimeError("rollback plan is not apply_ready")
    if str(report.get("mode") or "") != "dry-run-read-only-no-model-calls":
        raise RuntimeError("rollback plan did not come from the read-only planner")
    invariants = report.get("invariants") or {}
    if not isinstance(invariants, dict) or any(int(value or 0) != 0 for value in invariants.values()):
        raise RuntimeError("rollback plan has failed hard invariants")
    blockers = report.get("apply_blockers") or {}
    if not isinstance(blockers, dict) or any(int(value or 0) != 0 for value in blockers.values()):
        raise RuntimeError("rollback plan has apply blockers")
    members = report.get("members")
    if not isinstance(members, list) or not members:
        raise RuntimeError("rollback plan has no members")
    if len(members) != int((report.get("summary") or {}).get("cohort_total") or 0):
        raise RuntimeError("rollback plan member count mismatch")
    seen_tools: set[int] = set()
    seen_runs: set[int] = set()
    for member in members:
        tool_id = int(member.get("tool_id") or 0)
        run_id = int(member.get("incident_run_id") or 0)
        if tool_id <= 0 or run_id <= 0 or tool_id in seen_tools or run_id in seen_runs:
            raise RuntimeError(f"invalid or duplicate rollback member: tool={tool_id}, run={run_id}")
        seen_tools.add(tool_id)
        seen_runs.add(run_id)
        if str(member.get("action") or "") not in ALLOWED_ACTIONS:
            raise RuntimeError(f"unsupported rollback action for tool {tool_id}")
        if member.get("blockers"):
            raise RuntimeError(f"blocked rollback member: tool {tool_id}")
        if str((member.get("current_entity") or {}).get("source") or "") == "manual":
            if member.get("effects"):
                raise RuntimeError(f"manual entity mutation planned for tool {tool_id}")
        for effect in member.get("effects") or []:
            if str(effect.get("effect_type") or "") not in {
                "entity_current_state",
                "taxonomy_assignment",
                "product_profile",
            }:
                raise RuntimeError(f"unsupported effect for tool {tool_id}")


async def rebuild_fresh_plan(
    *,
    manifest_path: Path,
    incident_id: str,
) -> dict[str, Any]:
    manifest, members = load_frozen_cohort(manifest_path)
    config = load_config(require_brightdata=False)
    async with ReadOnlyD1Client(config) as d1:
        snapshot = await load_current_snapshot(d1, members, chunk_size=40)
    return build_rollback_plan(
        incident_id=incident_id,
        manifest=manifest,
        members=members,
        snapshot=snapshot,
    )


def member_plan_item(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_id": int(member.get("tool_id") or 0),
        "incident_run_id": int(member.get("incident_run_id") or 0),
        "matched_reason": str(member.get("matched_reason") or ""),
        "action": str(member.get("action") or ""),
        "current_entity": member.get("current_entity") or {},
        "desired_entity": member.get("desired_entity") or {},
        "surviving_run_id": member.get("surviving_run_id"),
        "profile_action": str(member.get("profile_action") or "none"),
        "effects": member.get("effects") or [],
    }


async def freeze_incident(
    d1: D1Client,
    report: dict[str, Any],
    *,
    actor: str,
) -> None:
    incident_id = str(report["incident_id"])
    manifest = report["manifest"]
    now = utc_now_iso()
    await d1.run(
        """
        INSERT INTO taxonomy_incidents (
          incident_id, name, reason, root_cause, status,
          source_candidate_count, confirmed_member_count,
          source_manifest_fingerprint, rollback_plan_hash, metadata_json,
          detected_at, frozen_at, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'frozen', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(incident_id) DO NOTHING
        """,
        [
            incident_id,
            "example.com neutral transport classification pollution",
            "Confirmed neutral transport evidence was treated as product evidence.",
            "Browser classification transport supplied the IANA example page for real product domains.",
            int(manifest.get("source_cohort_total") or 0),
            int(manifest.get("cohort_total") or 0),
            str(manifest.get("member_fingerprint") or ""),
            str(report.get("rollback_plan_hash") or ""),
            compact_json(
                {
                    "summary": report.get("summary") or {},
                    "excluded_non_incident": manifest.get("excluded_non_incident") or [],
                    "source_prompt_version": manifest.get("prompt_version"),
                }
            ),
            str(manifest.get("captured_at") or now),
            now,
            actor,
            now,
            now,
        ],
        operation="taxonomy_incident.freeze_incident",
    )
    existing = await d1.query(
        """
        SELECT incident_id, status, source_candidate_count, confirmed_member_count,
               source_manifest_fingerprint, rollback_plan_hash
        FROM taxonomy_incidents
        WHERE incident_id = ?
        """,
        [incident_id],
        operation="taxonomy_incident.verify_incident",
    )
    if len(existing) != 1:
        raise RuntimeError("failed to freeze taxonomy incident")
    row = existing[0]
    expected = (
        int(manifest.get("source_cohort_total") or 0),
        int(manifest.get("cohort_total") or 0),
        str(manifest.get("member_fingerprint") or ""),
        str(report.get("rollback_plan_hash") or ""),
    )
    actual = (
        int(row.get("source_candidate_count") or 0),
        int(row.get("confirmed_member_count") or 0),
        str(row.get("source_manifest_fingerprint") or ""),
        str(row.get("rollback_plan_hash") or ""),
    )
    if actual != expected:
        raise RuntimeError(f"existing incident freeze mismatch: expected={expected}, actual={actual}")

    members = sorted(report["members"], key=lambda item: int(item.get("tool_id") or 0))
    freeze_chunk_size = 25
    for offset in range(0, len(members), freeze_chunk_size):
        statements: list[tuple[str, list[Any]]] = []
        for member in members[offset : offset + freeze_chunk_size]:
            item = member_plan_item(member)
            action = str(member["action"])
            desired = member.get("desired_entity") or {}
            statements.append(
                (
                    """
                    INSERT INTO taxonomy_incident_members (
                      incident_id, tool_id, run_id, matched_reason, captured_at,
                      rollback_action, planned_entity_kind, surviving_run_id,
                      profile_action, before_state_json, planned_state_json,
                      plan_item_hash, apply_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(incident_id, tool_id) DO NOTHING
                    """,
                    [
                        incident_id,
                        int(member["tool_id"]),
                        int(member["incident_run_id"]),
                        str(member.get("matched_reason") or "neutral_transport"),
                        str(manifest.get("captured_at") or now),
                        action,
                        str(desired.get("kind") or "") or None,
                        int(member.get("surviving_run_id") or 0) or None,
                        str(member.get("profile_action") or "none"),
                        compact_json(
                            {
                                "entity": member.get("current_entity") or {},
                                "incident_run_created_at": member.get("incident_run_created_at"),
                            }
                        ),
                        compact_json(
                            {
                                "entity": desired,
                                "effects": member.get("effects") or [],
                            }
                        ),
                        _stable_hash(item),
                        now,
                        now,
                    ],
                )
            )
        operation = f"taxonomy_incident.freeze_members.{offset // freeze_chunk_size + 1}"
        for attempt in range(1, 4):
            try:
                await d1.batch(statements, operation=operation)
                break
            except (OSError, httpx.RequestError):
                # These inserts are uniquely keyed and use DO NOTHING, so a
                # response lost after commit is safe to replay. Business-state
                # batches below deliberately do not have this retry loop.
                if attempt == 3:
                    raise
                print(
                    compact_json(
                        {
                            "event": "taxonomy_incident.freeze_batch_retry",
                            "operation": operation,
                            "attempt": attempt + 1,
                        }
                    )
                )
                await asyncio.sleep(float(attempt))

    await verify_frozen_incident(d1, report)


async def verify_frozen_incident(
    d1: D1Client,
    report: dict[str, Any],
) -> None:
    incident_id = str(report["incident_id"])
    manifest = report["manifest"]
    incident_rows = await d1.query(
        """
        SELECT incident_id, status, source_candidate_count, confirmed_member_count,
               source_manifest_fingerprint, rollback_plan_hash
        FROM taxonomy_incidents
        WHERE incident_id = ?
        """,
        [incident_id],
        operation="taxonomy_incident.verify_frozen_incident",
    )
    if len(incident_rows) != 1:
        raise RuntimeError("frozen incident does not exist")
    incident = incident_rows[0]
    if str(incident.get("status") or "") not in {"frozen", "applying", "closed"}:
        raise RuntimeError(f"incident is not applyable: {incident.get('status')}")
    expected_incident = (
        int(manifest.get("source_cohort_total") or 0),
        int(manifest.get("cohort_total") or 0),
        str(manifest.get("member_fingerprint") or ""),
        str(report.get("rollback_plan_hash") or ""),
    )
    actual_incident = (
        int(incident.get("source_candidate_count") or 0),
        int(incident.get("confirmed_member_count") or 0),
        str(incident.get("source_manifest_fingerprint") or ""),
        str(incident.get("rollback_plan_hash") or ""),
    )
    if actual_incident != expected_incident:
        raise RuntimeError(
            "frozen incident metadata mismatch: "
            f"expected={expected_incident}, actual={actual_incident}"
        )

    members = sorted(report["members"], key=lambda item: int(item.get("tool_id") or 0))
    expected_by_tool = {
        int(member["tool_id"]): {
            "run_id": int(member["incident_run_id"]),
            "rollback_action": str(member["action"]),
            "plan_item_hash": _stable_hash(member_plan_item(member)),
        }
        for member in members
    }
    actual_by_tool: dict[int, dict[str, Any]] = {}
    tool_ids = sorted(expected_by_tool)
    for offset in range(0, len(tool_ids), 50):
        chunk = tool_ids[offset : offset + 50]
        placeholders = ",".join("?" for _ in chunk)
        rows = await d1.query(
            f"""
            SELECT tool_id, run_id, rollback_action, plan_item_hash, apply_status
            FROM taxonomy_incident_members
            WHERE incident_id = ? AND tool_id IN ({placeholders})
            """,
            [incident_id, *chunk],
            operation=f"taxonomy_incident.verify_frozen_member_hashes.{offset // 50 + 1}",
        )
        for row in rows:
            actual_by_tool[int(row["tool_id"])] = row
    if set(actual_by_tool) != set(expected_by_tool):
        missing = sorted(set(expected_by_tool) - set(actual_by_tool))
        extra = sorted(set(actual_by_tool) - set(expected_by_tool))
        raise RuntimeError(
            f"frozen incident member set mismatch: missing={missing[:10]}, extra={extra[:10]}"
        )
    mismatches: list[int] = []
    for tool_id, expected in expected_by_tool.items():
        actual = actual_by_tool[tool_id]
        actual_values = {
            "run_id": int(actual.get("run_id") or 0),
            "rollback_action": str(actual.get("rollback_action") or ""),
            "plan_item_hash": str(actual.get("plan_item_hash") or ""),
        }
        if actual_values != expected:
            mismatches.append(tool_id)
    if mismatches:
        raise RuntimeError(f"frozen incident member plan mismatch: tools={mismatches[:10]}")

    member_check = await d1.query(
        """
        SELECT COUNT(*) AS member_count,
               COUNT(DISTINCT tool_id) AS tool_count,
               COUNT(DISTINCT run_id) AS run_count,
               SUM(CASE WHEN apply_status = 'pending' THEN 1 ELSE 0 END) AS pending_count
        FROM taxonomy_incident_members
        WHERE incident_id = ?
        """,
        [incident_id],
        operation="taxonomy_incident.verify_frozen_members",
    )
    row = member_check[0] if member_check else {}
    expected_count = len(members)
    if (
        int(row.get("member_count") or 0) != expected_count
        or int(row.get("tool_count") or 0) != expected_count
        or int(row.get("run_count") or 0) != expected_count
    ):
        raise RuntimeError(f"frozen member count mismatch: {row}")


def _guard_sql(predicate: str) -> str:
    return (
        "SELECT CASE WHEN EXISTS(SELECT 1 "
        + predicate
        + ") THEN 1 ELSE json_extract('taxonomy_incident_guard_failed', '$') END AS guard_ok"
    )


def build_member_statements(
    member: dict[str, Any],
    *,
    incident_id: str,
    member_id: int,
    actor: str,
    now: str,
    frozen_at: str = "",
) -> list[tuple[str, list[Any]]]:
    tool_id = int(member["tool_id"])
    run_id = int(member["incident_run_id"])
    plan_item_hash = _stable_hash(member_plan_item(member))
    statements: list[tuple[str, list[Any]]] = [
        (
            _guard_sql(
                "FROM taxonomy_incident_members WHERE id = ? AND incident_id = ? "
                "AND tool_id = ? AND run_id = ? AND plan_item_hash = ? "
                "AND apply_status = 'pending'"
            ),
            [member_id, incident_id, tool_id, run_id, plan_item_hash],
        ),
        (
            _guard_sql(
                "FROM classification_runs incident_run "
                "WHERE incident_run.id = ? AND incident_run.tool_id = ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM classification_runs newer "
                "WHERE newer.tool_id = incident_run.tool_id AND newer.created_at > ?"
                ")"
            ),
            [run_id, tool_id, frozen_at],
        ),
        (
            _guard_sql(
                "FROM tools guarded_tool WHERE guarded_tool.id = ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM classification_reprocess_requests request "
                "WHERE request.tool_id = guarded_tool.id "
                "AND request.status IN ('queued', 'running')"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM classification_decisions decision "
                "WHERE decision.tool_id = guarded_tool.id "
                "AND decision.dimension = 'entity_kind' "
                "AND decision.decision LIKE 'set_entity_kind:%'"
                ")"
            ),
            [tool_id],
        ),
        (
            """
            INSERT INTO taxonomy_incident_invalidations (
              incident_id, member_id, tool_id, run_id, effect_type, effect_id,
              reason, before_state_json, after_state_json, applied_by, applied_at
            )
            SELECT ?, ?, r.tool_id, r.id, 'classification_run', CAST(r.id AS TEXT),
                   'INC-2026-08-EXAMPLE-COM confirmed neutral transport pollution',
                   json_object(
                     'run_status', r.run_status,
                     'error', r.error,
                     'prompt_version', r.prompt_version,
                     'created_at', r.created_at
                   ),
                   json_object('valid_for_current_projection', 0),
                   ?, ?
            FROM classification_runs r
            WHERE r.id = ? AND r.tool_id = ?
            ON CONFLICT(incident_id, run_id, effect_type, effect_id) DO NOTHING
            """,
            [incident_id, member_id, actor, now, run_id, tool_id],
        ),
    ]

    for effect in member.get("effects") or []:
        effect_type = str(effect.get("effect_type") or "")
        if effect_type == "entity_current_state":
            before = effect.get("before_state") or {}
            after = effect.get("after_state") or {}
            before_kind = str(before.get("kind") or "")
            before_source = str(before.get("source") or "")
            after_kind = str(after.get("kind") or "")
            after_source = str(after.get("source") or "")
            statements.extend(
                [
                    (
                        _guard_sql(
                            "FROM tools WHERE id = ? AND entity_kind = ? "
                            "AND COALESCE(entity_kind_source, '') = ?"
                        ),
                        [tool_id, before_kind, before_source],
                    ),
                    (
                        """
                        INSERT INTO taxonomy_incident_invalidations (
                          incident_id, member_id, tool_id, run_id, effect_type, effect_id,
                          reason, before_state_json, after_state_json, applied_by, applied_at
                        ) VALUES (?, ?, ?, ?, 'entity_current_state', ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(incident_id, run_id, effect_type, effect_id) DO NOTHING
                        """,
                        [
                            incident_id,
                            member_id,
                            tool_id,
                            run_id,
                            str(tool_id),
                            "Remove current entity effect of confirmed neutral transport run",
                            compact_json(before),
                            compact_json(after),
                            actor,
                            now,
                        ],
                    ),
                    (
                        """
                        UPDATE tools
                        SET entity_kind = ?, entity_kind_source = ?
                        WHERE id = ? AND entity_kind = ?
                          AND COALESCE(entity_kind_source, '') = ?
                        """,
                        [after_kind, after_source, tool_id, before_kind, before_source],
                    ),
                    (
                        """
                        INSERT INTO classification_decisions (
                          tool_id, assignment_id, previous_term_id, decided_term_id,
                          dimension, decision, reviewer_id, reason, created_at
                        ) VALUES (?, NULL, NULL, NULL, 'entity_kind', ?, ?, ?, ?)
                        """,
                        [
                            tool_id,
                            f"incident_rollback:set_entity_kind:{after_kind}",
                            actor,
                            f"{incident_id}; invalidated run {run_id}",
                            now,
                        ],
                    ),
                ]
            )
        elif effect_type == "product_profile":
            before = effect.get("before_state") or {}
            version = int(before.get("profile_version") or 0)
            extracted_at = str(before.get("extracted_at") or "")
            updated_at = str(before.get("updated_at") or "")
            profile_predicate = (
                "FROM product_profiles WHERE tool_id = ? AND profile_version = ? "
                "AND extracted_at = ? AND updated_at = ? "
                "AND (instr(lower(profile_json), 'this domain is for use in documentation example') > 0 "
                "OR instr(lower(profile_json), 'example domain') > 0 "
                "OR instr(lower(profile_json), 'iana example') > 0)"
            )
            statements.extend(
                [
                    (
                        _guard_sql(profile_predicate),
                        [tool_id, version, extracted_at, updated_at],
                    ),
                    (
                        """
                        INSERT INTO taxonomy_incident_invalidations (
                          incident_id, member_id, tool_id, run_id, effect_type, effect_id,
                          reason, before_state_json, after_state_json, applied_by, applied_at
                        )
                        SELECT ?, ?, p.tool_id, ?, 'product_profile', CAST(p.tool_id AS TEXT),
                               'Remove materialized profile built from neutral transport evidence',
                               json_object(
                                 'profile_json', json(p.profile_json),
                                 'profile_version', p.profile_version,
                                 'extracted_at', p.extracted_at,
                                 'created_at', p.created_at,
                                 'updated_at', p.updated_at
                               ),
                               json_object('materialized_state', 'absent', 'needs_revalidation', 1),
                               ?, ?
                        FROM product_profiles p
                        WHERE p.tool_id = ? AND p.profile_version = ?
                          AND p.extracted_at = ? AND p.updated_at = ?
                        ON CONFLICT(incident_id, run_id, effect_type, effect_id) DO NOTHING
                        """,
                        [
                            incident_id,
                            member_id,
                            run_id,
                            actor,
                            now,
                            tool_id,
                            version,
                            extracted_at,
                            updated_at,
                        ],
                    ),
                    (
                        """
                        DELETE FROM product_profiles
                        WHERE tool_id = ? AND profile_version = ?
                          AND extracted_at = ? AND updated_at = ?
                        """,
                        [tool_id, version, extracted_at, updated_at],
                    ),
                ]
            )
        elif effect_type == "taxonomy_assignment":
            assignment_id = int(effect.get("effect_id") or 0)
            before_status = str(effect.get("before_state") or "")
            statements.extend(
                [
                    (
                        _guard_sql(
                            "FROM product_taxonomy_assignments WHERE id = ? AND tool_id = ? "
                            "AND run_id = ? AND source = 'auto' AND decision_status = ?"
                        ),
                        [assignment_id, tool_id, run_id, before_status],
                    ),
                    (
                        """
                        INSERT INTO taxonomy_incident_invalidations (
                          incident_id, member_id, tool_id, run_id, effect_type, effect_id,
                          reason, before_state_json, after_state_json, applied_by, applied_at
                        ) VALUES (?, ?, ?, ?, 'taxonomy_assignment', ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(incident_id, run_id, effect_type, effect_id) DO NOTHING
                        """,
                        [
                            incident_id,
                            member_id,
                            tool_id,
                            run_id,
                            str(assignment_id),
                            "Supersede assignment produced by invalidated incident run",
                            compact_json({"decision_status": before_status}),
                            compact_json({"decision_status": "superseded", "is_primary": 0}),
                            actor,
                            now,
                        ],
                    ),
                    (
                        """
                        UPDATE product_taxonomy_assignments
                        SET decision_status = 'superseded', is_primary = 0, updated_at = ?
                        WHERE id = ? AND tool_id = ? AND run_id = ?
                          AND source = 'auto' AND decision_status = ?
                        """,
                        [now, assignment_id, tool_id, run_id, before_status],
                    ),
                ]
            )

    apply_status = "audit_only" if str(member.get("action") or "") in AUDIT_ONLY_ACTIONS and not member.get("effects") else "applied"
    statements.append(
        (
            """
            UPDATE taxonomy_incident_members
            SET apply_status = ?, applied_at = ?, updated_at = ?
            WHERE id = ? AND incident_id = ? AND apply_status = 'pending'
            """,
            [apply_status, now, now, member_id, incident_id],
        )
    )
    return statements


async def load_member_ids(d1: D1Client, incident_id: str) -> dict[int, int]:
    rows = await d1.query(
        "SELECT id, tool_id FROM taxonomy_incident_members WHERE incident_id = ?",
        [incident_id],
        operation="taxonomy_incident.load_member_ids",
    )
    return {int(row["tool_id"]): int(row["id"]) for row in rows}


async def verify_applied_members(
    d1: D1Client,
    members: list[dict[str, Any]],
    *,
    incident_id: str,
) -> None:
    tool_ids = [int(member["tool_id"]) for member in members]
    placeholders = ",".join("?" for _ in tool_ids)
    status_rows = await d1.query(
        f"""
        SELECT tool_id, apply_status
        FROM taxonomy_incident_members
        WHERE incident_id = ? AND tool_id IN ({placeholders})
        """,
        [incident_id, *tool_ids],
        operation="taxonomy_incident.verify_member_status",
    )
    statuses = {int(row["tool_id"]): str(row["apply_status"]) for row in status_rows}
    if any(statuses.get(tool_id) not in {"applied", "audit_only"} for tool_id in tool_ids):
        raise RuntimeError(f"batch member status verification failed: {statuses}")

    invalidation_rows = await d1.query(
        f"""
        SELECT tool_id, effect_type, effect_id
        FROM taxonomy_incident_invalidations
        WHERE incident_id = ? AND tool_id IN ({placeholders})
        """,
        [incident_id, *tool_ids],
        operation="taxonomy_incident.verify_effect_invalidations",
    )
    actual_invalidations = {
        (
            int(row["tool_id"]),
            str(row["effect_type"]),
            str(row["effect_id"]),
        )
        for row in invalidation_rows
    }
    expected_invalidations: set[tuple[int, str, str]] = set()
    for member in members:
        tool_id = int(member["tool_id"])
        expected_invalidations.add(
            (tool_id, "classification_run", str(int(member["incident_run_id"])))
        )
        for effect in member.get("effects") or []:
            expected_invalidations.add(
                (
                    tool_id,
                    str(effect["effect_type"]),
                    str(effect["effect_id"]),
                )
            )
    if actual_invalidations != expected_invalidations:
        missing = sorted(expected_invalidations - actual_invalidations)
        extra = sorted(actual_invalidations - expected_invalidations)
        raise RuntimeError(
            f"effect invalidation verification failed: missing={missing[:10]}, extra={extra[:10]}"
        )

    tool_rows = await d1.query(
        f"""
        SELECT t.id AS tool_id, t.entity_kind, t.entity_kind_source,
               CASE WHEN p.tool_id IS NULL THEN 0 ELSE 1 END AS has_profile
        FROM tools t
        LEFT JOIN product_profiles p ON p.tool_id = t.id
        WHERE t.id IN ({placeholders})
        """,
        tool_ids,
        operation="taxonomy_incident.verify_current_state",
    )
    state = {int(row["tool_id"]): row for row in tool_rows}
    violations: list[str] = []
    for member in members:
        tool_id = int(member["tool_id"])
        row = state.get(tool_id) or {}
        for effect in member.get("effects") or []:
            if effect.get("effect_type") == "entity_current_state":
                after = effect.get("after_state") or {}
                if (
                    str(row.get("entity_kind") or "") != str(after.get("kind") or "")
                    or str(row.get("entity_kind_source") or "") != str(after.get("source") or "")
                ):
                    violations.append(f"tool {tool_id}: entity state mismatch")
            elif effect.get("effect_type") == "product_profile":
                if int(row.get("has_profile") or 0) != 0:
                    violations.append(f"tool {tool_id}: polluted profile still materialized")
    if violations:
        raise RuntimeError("; ".join(violations[:10]))

    assignment_ids = [
        int(effect["effect_id"])
        for member in members
        for effect in member.get("effects") or []
        if effect.get("effect_type") == "taxonomy_assignment"
    ]
    if assignment_ids:
        assignment_placeholders = ",".join("?" for _ in assignment_ids)
        assignment_rows = await d1.query(
            f"""
            SELECT id, decision_status, is_primary
            FROM product_taxonomy_assignments
            WHERE id IN ({assignment_placeholders})
            """,
            assignment_ids,
            operation="taxonomy_incident.verify_assignments",
        )
        assignment_state = {int(row["id"]): row for row in assignment_rows}
        if any(
            str((assignment_state.get(assignment_id) or {}).get("decision_status") or "")
            != "superseded"
            or int((assignment_state.get(assignment_id) or {}).get("is_primary") or 0) != 0
            for assignment_id in assignment_ids
        ):
            raise RuntimeError("taxonomy assignment verification failed")

    entity_effect_tools = {
        int(member["tool_id"])
        for member in members
        if any(
            effect.get("effect_type") == "entity_current_state"
            for effect in member.get("effects") or []
        )
    }
    if entity_effect_tools:
        decision_rows = await d1.query(
            f"""
            SELECT tool_id, COUNT(*) AS decision_count
            FROM classification_decisions
            WHERE tool_id IN ({placeholders}) AND reason LIKE ?
            GROUP BY tool_id
            """,
            [*tool_ids, f"{incident_id};%"],
            operation="taxonomy_incident.verify_entity_decisions",
        )
        decision_counts = {
            int(row["tool_id"]): int(row.get("decision_count") or 0)
            for row in decision_rows
        }
        if any(decision_counts.get(tool_id) != 1 for tool_id in entity_effect_tools):
            raise RuntimeError("entity decision audit verification failed")


async def apply_batches(
    d1: D1Client,
    report: dict[str, Any],
    *,
    actor: str,
    batch_size: int,
    max_batches: int,
) -> dict[str, Any]:
    incident_id = str(report["incident_id"])
    plan_hash = str(report["rollback_plan_hash"])
    incident_state_rows = await d1.query(
        "SELECT frozen_at FROM taxonomy_incidents WHERE incident_id = ? AND rollback_plan_hash = ?",
        [incident_id, plan_hash],
        operation="taxonomy_incident.load_frozen_at",
    )
    if len(incident_state_rows) != 1 or not str(incident_state_rows[0].get("frozen_at") or ""):
        raise RuntimeError("incident frozen_at is unavailable")
    frozen_at = str(incident_state_rows[0]["frozen_at"])
    member_ids = await load_member_ids(d1, incident_id)
    members = sorted(report["members"], key=lambda item: int(item["tool_id"]))
    existing_rows = await d1.query(
        "SELECT tool_id, apply_status FROM taxonomy_incident_members WHERE incident_id = ?",
        [incident_id],
        operation="taxonomy_incident.load_apply_status",
    )
    status_by_tool = {int(row["tool_id"]): str(row["apply_status"]) for row in existing_rows}
    pending = [
        member
        for member in members
        if status_by_tool.get(int(member["tool_id"])) == "pending"
    ]
    await d1.run(
        """
        UPDATE taxonomy_incidents
        SET status = 'applying', started_at = COALESCE(started_at, ?), updated_at = ?
        WHERE incident_id = ? AND rollback_plan_hash = ? AND status IN ('frozen', 'applying')
        """,
        [utc_now_iso(), utc_now_iso(), incident_id, plan_hash],
        operation="taxonomy_incident.mark_applying",
    )

    completed_batches = 0
    applied_members = 0
    action_counts: Counter[str] = Counter()
    for offset in range(0, len(pending), batch_size):
        if max_batches > 0 and completed_batches >= max_batches:
            break
        batch_members = pending[offset : offset + batch_size]
        if not batch_members:
            break
        first_tool = int(batch_members[0]["tool_id"])
        last_tool = int(batch_members[-1]["tool_id"])
        batch_number_rows = await d1.query(
            "SELECT COALESCE(MAX(batch_number), 0) + 1 AS next_batch FROM taxonomy_incident_batches WHERE incident_id = ?",
            [incident_id],
            operation="taxonomy_incident.next_batch_number",
        )
        batch_number = int(batch_number_rows[0].get("next_batch") or 1)
        now = utc_now_iso()
        batch_actions = Counter(str(member["action"]) for member in batch_members)
        statements: list[tuple[str, list[Any]]] = [
            (
                _guard_sql(
                    "FROM taxonomy_incidents WHERE incident_id = ? "
                    "AND rollback_plan_hash = ? AND status = 'applying'"
                ),
                [incident_id, plan_hash],
            ),
            (
                """
                INSERT INTO taxonomy_incident_batches (
                  incident_id, batch_number, rollback_plan_hash, first_tool_id,
                  last_tool_id, member_count, status, result_json,
                  started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', '{}', ?, ?, ?)
                """,
                [
                    incident_id,
                    batch_number,
                    plan_hash,
                    first_tool,
                    last_tool,
                    len(batch_members),
                    now,
                    now,
                    now,
                ],
            ),
        ]
        for member in batch_members:
            tool_id = int(member["tool_id"])
            statements.extend(
                build_member_statements(
                    member,
                    incident_id=incident_id,
                    member_id=member_ids[tool_id],
                    actor=actor,
                    now=now,
                    frozen_at=frozen_at,
                )
            )
        result_json = compact_json(
            {
                "member_count": len(batch_members),
                "action_counts": dict(sorted(batch_actions.items())),
                "first_tool_id": first_tool,
                "last_tool_id": last_tool,
            }
        )
        statements.append(
            (
                """
                UPDATE taxonomy_incident_batches
                SET status = 'succeeded', result_json = ?, completed_at = ?, updated_at = ?
                WHERE incident_id = ? AND batch_number = ? AND status = 'running'
                """,
                [result_json, now, now, incident_id, batch_number],
            )
        )
        await d1.batch(
            statements,
            operation=f"taxonomy_incident.apply_batch.{batch_number}",
        )
        await verify_applied_members(d1, batch_members, incident_id=incident_id)
        completed_batches += 1
        applied_members += len(batch_members)
        action_counts.update(batch_actions)
        print(
            compact_json(
                {
                    "event": "taxonomy_incident.batch_succeeded",
                    "batch_number": batch_number,
                    "member_count": len(batch_members),
                    "first_tool_id": first_tool,
                    "last_tool_id": last_tool,
                }
            )
        )

    remaining_rows = await d1.query(
        """
        SELECT COUNT(*) AS remaining
        FROM taxonomy_incident_members
        WHERE incident_id = ? AND apply_status = 'pending'
        """,
        [incident_id],
        operation="taxonomy_incident.remaining_members",
    )
    remaining = int((remaining_rows[0] if remaining_rows else {}).get("remaining") or 0)
    if remaining == 0:
        now = utc_now_iso()
        await d1.run(
            """
            UPDATE taxonomy_incidents
            SET status = 'closed', closed_at = ?, updated_at = ?
            WHERE incident_id = ? AND rollback_plan_hash = ? AND status = 'applying'
            """,
            [now, now, incident_id, plan_hash],
            operation="taxonomy_incident.close_incident",
        )
    return {
        "completed_batches": completed_batches,
        "applied_members": applied_members,
        "remaining_members": remaining,
        "action_counts": dict(sorted(action_counts.items())),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.plan).resolve()
    manifest_path = Path(args.manifest).resolve()
    stored_plan = load_plan(plan_path)
    validate_plan(
        stored_plan,
        expected_incident_id=args.incident_id,
        expected_plan_hash=args.expected_plan_hash,
    )
    if not args.apply:
        fresh_plan = await rebuild_fresh_plan(
            manifest_path=manifest_path,
            incident_id=args.incident_id,
        )
        validate_plan(
            fresh_plan,
            expected_incident_id=args.incident_id,
            expected_plan_hash=args.expected_plan_hash,
        )
        if str(stored_plan["rollback_plan_hash"]) != str(fresh_plan["rollback_plan_hash"]):
            raise RuntimeError("stored plan is stale compared with current D1 state")
        return {
            "mode": "validation-only",
            "incident_id": args.incident_id,
            "rollback_plan_hash": args.expected_plan_hash,
            "cohort_total": len(fresh_plan["members"]),
            "writes": 0,
        }

    config = load_config(require_brightdata=False)
    async with D1Client(config) as d1:
        incident_rows = await d1.query(
            "SELECT incident_id, status FROM taxonomy_incidents WHERE incident_id = ?",
            [args.incident_id],
            operation="taxonomy_incident.find_existing",
        )
        if incident_rows:
            # Once a canary batch has changed current projections, rebuilding the
            # original whole-plan hash would be expected to differ. Resume from
            # the immutable frozen incident/member hashes instead; each pending
            # mutation still has an atomic before-state guard.
            if str(incident_rows[0].get("status") or "") == "frozen":
                # Cohort freezing itself is chunked. If the process stopped
                # between chunks, replay the idempotent inserts before checking
                # the complete frozen member set.
                await freeze_incident(d1, stored_plan, actor=args.actor)
            else:
                await verify_frozen_incident(d1, stored_plan)
            apply_plan = stored_plan
        else:
            fresh_plan = await rebuild_fresh_plan(
                manifest_path=manifest_path,
                incident_id=args.incident_id,
            )
            validate_plan(
                fresh_plan,
                expected_incident_id=args.incident_id,
                expected_plan_hash=args.expected_plan_hash,
            )
            if str(stored_plan["rollback_plan_hash"]) != str(fresh_plan["rollback_plan_hash"]):
                raise RuntimeError("stored plan is stale compared with current D1 state")
            await freeze_incident(d1, fresh_plan, actor=args.actor)
            apply_plan = fresh_plan
        result = await apply_batches(
            d1,
            apply_plan,
            actor=args.actor,
            batch_size=max(1, min(args.batch_size, 50)),
            max_batches=max(0, args.max_batches),
        )
    return {
        "mode": "apply",
        "incident_id": args.incident_id,
        "rollback_plan_hash": args.expected_plan_hash,
        **result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-plan-hash", required=True)
    parser.add_argument("--incident-id", default=DEFAULT_INCIDENT_ID)
    parser.add_argument("--actor", default="incident-repair:codex")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(compact_json(result))


if __name__ == "__main__":
    main()
