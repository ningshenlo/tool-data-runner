"""Build a read-only rollback plan for a frozen taxonomy incident cohort.

The command is deliberately incapable of mutating D1.  It consumes the JSON
artifact produced by ``dry_run_non_product_classification.py`` so repaired tools
remain members of the incident, reloads their current production state, and
classifies each member into an auditable rollback action.

This is a planner, not an apply command.  Ambiguous provenance is reported and
never converted into an automatic mutation.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from dry_run_non_product_classification import ReadOnlyD1Client
from runner import load_config


DEFAULT_INCIDENT_ID = "INC-2026-08-EXAMPLE-COM"
DEFAULT_INCIDENT_PROMPT = "shadow-top2-v2-entity-gated-capability-optional-v2-2026-08-10"
ACTIVE_REPROCESS_STATUSES = {"queued", "running"}
TERMINAL_ASSIGNMENT_STATUSES = {"superseded", "rejected"}
ENTITY_KINDS = {
    "independent_product",
    "product_module",
    "feature_landing",
    "company_site",
    "app_or_extension",
    "regional_mirror",
    "duplicate_alias",
    "non_product",
    "unresolved",
}


@dataclass(frozen=True)
class IncidentMember:
    tool_id: int
    incident_run_id: int | None
    tool_name: str
    canonical_slug: str
    matched_reason: str
    provenance_tier: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_timestamp(value: Any) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: Any) -> datetime | None:
    text = _normalize_timestamp(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), max(1, size)):
        yield values[index : index + max(1, size)]


def load_frozen_cohort(path: Path) -> tuple[dict[str, Any], list[IncidentMember]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entity_rows = payload.get("entity_decisions")
    if not isinstance(entity_rows, list) or not entity_rows:
        raise RuntimeError("incident manifest has no entity_decisions cohort")

    records_by_tool: dict[int, dict[str, Any]] = {}
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        tool_id = int(record.get("tool_id") or 0)
        if tool_id > 0:
            records_by_tool[tool_id] = record

    members: list[IncidentMember] = []
    excluded_non_incident: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in entity_rows:
        if not isinstance(row, dict):
            continue
        tool_id = int(row.get("tool_id") or 0)
        if tool_id <= 0 or tool_id in seen:
            raise RuntimeError(f"invalid or duplicate incident tool_id: {tool_id}")
        seen.add(tool_id)
        record = records_by_tool.get(tool_id) or {}
        matches = record.get("matches") if isinstance(record.get("matches"), list) else []
        neutral_matches = [
            match
            for match in matches
            if isinstance(match, dict)
            and str(match.get("provider") or "") == "neutral_transport"
        ]
        if not neutral_matches:
            excluded_non_incident.append(
                {
                    "tool_id": tool_id,
                    "tool_name": str(row.get("tool_name") or ""),
                    "canonical_slug": str(row.get("canonical_slug") or ""),
                    "reason": "no_neutral_transport_evidence",
                }
            )
            continue
        run_id = int(record.get("latest_run_id") or 0) or None
        reasons = sorted(
            {
                f"{match.get('provider')}:{match.get('code')}"
                for match in neutral_matches
                if isinstance(match, dict) and (match.get("provider") or match.get("code"))
            }
        )
        members.append(
            IncidentMember(
                tool_id=tool_id,
                incident_run_id=run_id,
                tool_name=str(row.get("tool_name") or ""),
                canonical_slug=str(row.get("canonical_slug") or ""),
                matched_reason=",".join(reasons) or "captured_v2_non_product_cohort",
                provenance_tier="captured_run" if run_id else "missing_run_pointer",
            )
        )

    source_total = int((payload.get("summary") or {}).get("cohort_total") or 0)
    if source_total and len(seen) != source_total:
        raise RuntimeError(
            f"source manifest cohort mismatch: summary={source_total}, rows={len(seen)}"
        )
    members.sort(key=lambda member: member.tool_id)
    metadata = {
        "source_path": str(path.resolve()),
        "captured_at": str(payload.get("generated_at") or ""),
        "prompt_version": str(payload.get("prompt_version") or ""),
        "source_cohort_total": len(seen),
        "cohort_total": len(members),
        "excluded_non_incident_count": len(excluded_non_incident),
        "excluded_non_incident": excluded_non_incident,
        "member_fingerprint": _stable_hash(
            [
                [member.tool_id, member.incident_run_id, member.matched_reason]
                for member in members
            ]
        ),
    }
    return metadata, members


TOOLS_SQL = """
SELECT
  t.id AS tool_id,
  t.canonical_slug,
  t.status AS tool_status,
  t.duplicate_of_tool_id,
  t.entity_kind,
  t.entity_kind_source,
  t.primary_category_id,
  p.profile_json,
  p.profile_version,
  p.extracted_at AS profile_extracted_at,
  p.updated_at AS profile_updated_at,
  CASE
    WHEN instr(lower(COALESCE(p.profile_json, '')), 'this domain is for use in documentation example') > 0
      OR instr(lower(COALESCE(p.profile_json, '')), 'example domain') > 0
      OR instr(lower(COALESCE(p.profile_json, '')), 'iana example') > 0
    THEN 1 ELSE 0
  END AS profile_neutral_transport
FROM tools t
LEFT JOIN product_profiles p ON p.tool_id = t.id
WHERE t.id IN ({placeholders})
"""


RUNS_SQL = """
SELECT
  r.id,
  r.tool_id,
  r.taxonomy_version,
  r.prompt_version,
  r.extractor_version,
  r.provider,
  r.model_name,
  r.run_status,
  r.error,
  r.created_at,
  COALESCE(
    json_extract(r.candidate_terms_json, '$.entity.kind'),
    json_extract(r.candidate_terms_json, '$.entity.candidate_kind'),
    json_extract(r.raw_output, '$.entity_decision.kind'),
    json_extract(r.raw_output, '$.profile.entity_decision.kind'),
    ''
  ) AS entity_kind,
  COALESCE(
    json_extract(r.candidate_terms_json, '$.entity.source'),
    json_extract(r.raw_output, '$.entity_decision.source'),
    json_extract(r.raw_output, '$.profile.entity_decision.source'),
    ''
  ) AS entity_source,
  COALESCE(
    json_extract(r.candidate_terms_json, '$.entity.accepted'),
    json_extract(r.raw_output, '$.entity_decision.accepted'),
    json_extract(r.raw_output, '$.profile.entity_decision.accepted'),
    0
  ) AS entity_accepted,
  COALESCE(json_extract(r.raw_output, '$.page_quality.state'), '') AS page_quality_state,
  COALESCE(json_extract(r.raw_output, '$.source_url'), '') AS source_url,
  COALESCE(json_extract(r.raw_output, '$.auto_non_product_recheck'), 0) AS auto_non_product_recheck,
  CASE
    WHEN instr(lower(COALESCE(r.candidate_terms_json, '') || ' ' || COALESCE(r.raw_output, '')), 'this domain is for use in documentation example') > 0
      OR instr(lower(COALESCE(r.candidate_terms_json, '') || ' ' || COALESCE(r.raw_output, '')), 'example domain') > 0
      OR instr(lower(COALESCE(r.candidate_terms_json, '') || ' ' || COALESCE(r.raw_output, '')), 'iana example') > 0
    THEN 1 ELSE 0
  END AS neutral_transport,
  CASE
    WHEN COALESCE(json_extract(r.raw_output, '$.page_quality.state'), '') IN ('anti_bot', 'access_denied', 'error_page')
      OR instr(lower(COALESCE(r.raw_output, '')), 'just a moment') > 0
      OR instr(lower(COALESCE(r.raw_output, '')), 'access denied') > 0
      OR instr(lower(COALESCE(r.raw_output, '')), 'verify you are human') > 0
    THEN 1 ELSE 0
  END AS blocked_evidence
FROM classification_runs r
WHERE r.tool_id IN ({placeholders})
ORDER BY r.tool_id, r.created_at, r.id
"""


ASSIGNMENTS_SQL = """
SELECT
  a.id,
  a.tool_id,
  a.term_id,
  a.run_id,
  a.is_primary,
  a.confidence,
  a.decision_status,
  a.source,
  a.assigned_at,
  a.reviewed_at,
  a.updated_at,
  term.dimension,
  term.slug AS term_slug,
  term.taxonomy_version
FROM product_taxonomy_assignments a
JOIN taxonomy_terms term ON term.id = a.term_id
WHERE a.tool_id IN ({placeholders})
ORDER BY a.tool_id, a.id
"""


DECISIONS_SQL = """
SELECT id, tool_id, assignment_id, dimension, decision, reviewer_id, reason, created_at
FROM classification_decisions
WHERE tool_id IN ({placeholders})
ORDER BY tool_id, created_at, id
"""


REQUESTS_SQL = """
SELECT id, tool_id, status, request_source, requested_at, started_at, classification_run_id
FROM classification_reprocess_requests
WHERE tool_id IN ({placeholders})
  AND status IN ('queued', 'running')
ORDER BY tool_id, id
"""


async def load_current_snapshot(
    d1: ReadOnlyD1Client,
    members: list[IncidentMember],
    *,
    chunk_size: int = 40,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "tools": [],
        "runs": [],
        "assignments": [],
        "decisions": [],
        "requests": [],
    }
    ids = [member.tool_id for member in members]
    queries = (
        ("tools", TOOLS_SQL),
        ("runs", RUNS_SQL),
        ("assignments", ASSIGNMENTS_SQL),
        ("decisions", DECISIONS_SQL),
        ("requests", REQUESTS_SQL),
    )
    for chunk_index, chunk in enumerate(_chunks(ids, chunk_size)):
        placeholders = ",".join("?" for _ in chunk)
        for key, template in queries:
            rows = await d1.query(
                template.format(placeholders=placeholders),
                list(chunk),
                operation=f"taxonomy_incident_rollback.{key}.{chunk_index}",
            )
            result[key].extend(rows)
    return result


def _run_entity_kind(run: dict[str, Any]) -> str:
    kind = str(run.get("entity_kind") or "").strip().lower()
    return kind if kind in ENTITY_KINDS else ""


def _run_is_polluted(run: dict[str, Any]) -> bool:
    return bool(int(run.get("neutral_transport") or 0))


def _run_is_clean_entity_decision(run: dict[str, Any]) -> bool:
    kind = _run_entity_kind(run)
    return bool(
        kind
        and not _run_is_polluted(run)
        and not bool(int(run.get("blocked_evidence") or 0))
    )


def _run_sort_key(run: dict[str, Any]) -> tuple[str, int]:
    return (_normalize_timestamp(run.get("created_at")), int(run.get("id") or 0))


def _profile_matches_incident(tool: dict[str, Any], incident_run: dict[str, Any]) -> bool:
    if not bool(int(tool.get("profile_neutral_transport") or 0)):
        return False
    profile_at = _parse_timestamp(tool.get("profile_extracted_at") or tool.get("profile_updated_at"))
    run_at = _parse_timestamp(incident_run.get("created_at"))
    if profile_at is None or run_at is None:
        return False
    return abs((run_at - profile_at).total_seconds()) <= 3600


def _find_incident_run(
    member: IncidentMember,
    runs: list[dict[str, Any]],
    *,
    captured_at: str,
    prompt_version: str,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    blockers: list[str] = []
    by_id = {int(run.get("id") or 0): run for run in runs}
    if member.incident_run_id:
        run = by_id.get(member.incident_run_id)
        if not run:
            return None, "missing_captured_run", ["captured_run_not_found"]
        if int(run.get("tool_id") or 0) != member.tool_id:
            return None, "captured_run_mismatch", ["captured_run_tool_mismatch"]
        if _run_entity_kind(run) != "non_product":
            blockers.append("captured_run_entity_is_not_non_product")
        if str(run.get("error") or "") != "entity_not_eligible:non_product":
            blockers.append("captured_run_error_mismatch")
        return run, "captured_run", blockers

    cutoff = _parse_timestamp(captured_at)
    candidates = []
    for run in runs:
        run_at = _parse_timestamp(run.get("created_at"))
        if cutoff and run_at and run_at > cutoff:
            continue
        if prompt_version and str(run.get("prompt_version") or "") != prompt_version:
            continue
        if str(run.get("error") or "") != "entity_not_eligible:non_product":
            continue
        if _run_entity_kind(run) != "non_product":
            continue
        candidates.append(run)
    if not candidates:
        return None, "missing_run_pointer", ["incident_run_cannot_be_reconstructed"]
    chosen = sorted(candidates, key=_run_sort_key)[-1]
    return chosen, "derived_run", ["incident_run_pointer_was_not_captured"]


def build_rollback_plan(
    *,
    incident_id: str,
    manifest: dict[str, Any],
    members: list[IncidentMember],
    snapshot: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    tools_by_id = {int(row.get("tool_id") or 0): row for row in snapshot["tools"]}
    rows_by_tool: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for key in ("runs", "assignments", "decisions", "requests"):
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in snapshot[key]:
            grouped[int(row.get("tool_id") or 0)].append(row)
        rows_by_tool[key] = grouped

    plans: list[dict[str, Any]] = []
    for member in members:
        tool = tools_by_id.get(member.tool_id)
        runs = sorted(rows_by_tool["runs"].get(member.tool_id, []), key=_run_sort_key)
        assignments = rows_by_tool["assignments"].get(member.tool_id, [])
        decisions = rows_by_tool["decisions"].get(member.tool_id, [])
        active_requests = rows_by_tool["requests"].get(member.tool_id, [])
        blockers: list[str] = []
        effects: list[dict[str, Any]] = []
        if tool is None:
            plans.append(
                {
                    **asdict(member),
                    "action": "needs_manual_investigation",
                    "apply_eligible": False,
                    "blockers": ["tool_not_found"],
                    "effects": [],
                }
            )
            continue

        incident_run, run_provenance, run_blockers = _find_incident_run(
            member,
            runs,
            captured_at=str(manifest.get("captured_at") or ""),
            prompt_version=str(manifest.get("prompt_version") or ""),
        )
        blockers.extend(run_blockers)
        current_kind = str(tool.get("entity_kind") or "unresolved")
        current_source = str(tool.get("entity_kind_source") or "")
        manual_decisions = [
            row
            for row in decisions
            if str(row.get("dimension") or "") == "entity_kind"
            and str(row.get("decision") or "").startswith("set_entity_kind:")
        ]
        manual_primary = [
            row
            for row in assignments
            if str(row.get("dimension") or "") == "primary_category"
            and str(row.get("source") or "") == "manual"
            and str(row.get("decision_status") or "") == "verified"
            and bool(int(row.get("is_primary") or 0))
        ]
        legacy = [
            row
            for row in assignments
            if str(row.get("source") or "") == "legacy"
            and str(row.get("decision_status") or "") == "legacy"
        ]
        incident_assignments: list[dict[str, Any]] = []
        later_clean: list[dict[str, Any]] = []
        prior_clean: list[dict[str, Any]] = []

        if incident_run is not None:
            incident_run_id = int(incident_run.get("id") or 0)
            incident_key = _run_sort_key(incident_run)
            incident_assignments = [
                row
                for row in assignments
                if int(row.get("run_id") or 0) == incident_run_id
            ]
            later_clean = [
                run
                for run in runs
                if _run_sort_key(run) > incident_key and _run_is_clean_entity_decision(run)
            ]
            prior_clean = [
                run
                for run in runs
                if _run_sort_key(run) < incident_key
                and _run_is_clean_entity_decision(run)
                and _run_entity_kind(run) not in {"", "unresolved"}
                and bool(int(run.get("entity_accepted") or 0))
            ]
            for assignment in incident_assignments:
                source = str(assignment.get("source") or "")
                status = str(assignment.get("decision_status") or "")
                if source != "auto":
                    blockers.append("incident_run_points_to_non_auto_assignment")
                    continue
                if status not in TERMINAL_ASSIGNMENT_STATUSES:
                    effects.append(
                        {
                            "effect_type": "taxonomy_assignment",
                            "effect_id": int(assignment.get("id") or 0),
                            "before_state": status,
                            "after_state": "superseded",
                            "run_id": incident_run_id,
                        }
                    )

        if active_requests:
            blockers.append("active_reprocess_request")

        action = ""
        desired_kind = current_kind
        chosen_surviving_run: dict[str, Any] | None = None
        if current_source == "manual" or manual_decisions:
            action = "manual_protected_untouched"
        elif incident_run is None:
            action = "ambiguous_provenance"
        elif later_clean:
            chosen_surviving_run = sorted(later_clean, key=_run_sort_key)[-1]
            later_kind = _run_entity_kind(chosen_surviving_run)
            if current_source == "auto" and current_kind == later_kind:
                action = "already_repaired_by_later_clean_run"
            else:
                action = "ambiguous_provenance"
                blockers.append("current_entity_disagrees_with_later_clean_run")
        elif current_source != "auto" or current_kind != "non_product":
            action = "no_current_effect_audit_only"
        elif run_provenance != "captured_run":
            action = "ambiguous_provenance"
        elif prior_clean:
            chosen_surviving_run = sorted(prior_clean, key=_run_sort_key)[-1]
            desired_kind = _run_entity_kind(chosen_surviving_run)
            action = "restore_previous_clean_auto"
            effects.append(
                {
                    "effect_type": "entity_current_state",
                    "effect_id": member.tool_id,
                    "before_state": {"kind": current_kind, "source": current_source},
                    "after_state": {"kind": desired_kind, "source": "auto"},
                    "surviving_run_id": int(chosen_surviving_run.get("id") or 0),
                }
            )
        else:
            desired_kind = "unresolved"
            action = "reset_entity_to_unresolved"
            effects.append(
                {
                    "effect_type": "entity_current_state",
                    "effect_id": member.tool_id,
                    "before_state": {"kind": current_kind, "source": current_source},
                    "after_state": {"kind": "unresolved", "source": "auto"},
                }
            )

        profile_action = "none"
        if incident_run is not None and _profile_matches_incident(tool, incident_run):
            if later_clean:
                profile_action = "ambiguous_profile_provenance"
                blockers.append("polluted_profile_survived_later_clean_run")
            else:
                profile_action = "invalidate_incident_profile"
                effects.append(
                    {
                        "effect_type": "product_profile",
                        "effect_id": member.tool_id,
                        "before_state": {
                            "profile_version": tool.get("profile_version"),
                            "extracted_at": tool.get("profile_extracted_at"),
                            "profile_hash": _stable_hash(
                                _json_object(tool.get("profile_json"))
                            ),
                        },
                        "after_state": "invalidated_needs_revalidation",
                    }
                )

        if action in {"ambiguous_provenance", "needs_manual_investigation"}:
            effects = []
        apply_eligible = not blockers and action in {
            "restore_previous_clean_auto",
            "reset_entity_to_unresolved",
            "no_current_effect_audit_only",
        }
        if action in {
            "manual_protected_untouched",
            "already_repaired_by_later_clean_run",
        }:
            apply_eligible = not blockers

        plans.append(
            {
                **asdict(member),
                "incident_run_id": int(incident_run.get("id") or 0)
                if incident_run
                else member.incident_run_id,
                "incident_run_created_at": str(incident_run.get("created_at") or "")
                if incident_run
                else "",
                "run_provenance": run_provenance,
                "current_entity": {"kind": current_kind, "source": current_source},
                "desired_entity": {"kind": desired_kind, "source": current_source},
                "action": action,
                "apply_eligible": apply_eligible,
                "blockers": sorted(set(blockers)),
                "active_reprocess_request_ids": [
                    int(row.get("id") or 0) for row in active_requests
                ],
                "manual_entity_decision_ids": [
                    int(row.get("id") or 0) for row in manual_decisions
                ],
                "manual_primary_assignment_ids": [
                    int(row.get("id") or 0) for row in manual_primary
                ],
                "legacy_assignment_ids": [int(row.get("id") or 0) for row in legacy],
                "incident_assignment_ids": [
                    int(row.get("id") or 0) for row in incident_assignments
                ],
                "surviving_run_id": int(chosen_surviving_run.get("id") or 0)
                if chosen_surviving_run
                else None,
                "profile_action": profile_action,
                "effects": effects,
            }
        )

    action_counts = Counter(plan["action"] for plan in plans)
    blocker_counts = Counter(
        blocker for plan in plans for blocker in plan.get("blockers") or []
    )
    effect_counts = Counter(
        effect["effect_type"] for plan in plans for effect in plan.get("effects") or []
    )
    incident_assignment_count = sum(
        len(plan.get("incident_assignment_ids") or []) for plan in plans
    )
    legacy_retained = sum(bool(plan.get("legacy_assignment_ids")) for plan in plans)
    manual_entity_change_planned = sum(
        plan["current_entity"]["source"] == "manual" and bool(plan.get("effects"))
        for plan in plans
        if "current_entity" in plan
    )
    manual_gold_change_planned = sum(
        bool(plan.get("manual_primary_assignment_ids"))
        and any(
            effect.get("effect_type") == "taxonomy_assignment"
            for effect in plan.get("effects") or []
        )
        for plan in plans
    )
    later_clean_superseded = sum(
        plan.get("action") == "already_repaired_by_later_clean_run"
        and bool(plan.get("effects"))
        for plan in plans
    )
    ambiguous_mutations = sum(
        plan.get("action") == "ambiguous_provenance" and bool(plan.get("effects"))
        for plan in plans
    )
    active_requests = sum(bool(plan.get("active_reprocess_request_ids")) for plan in plans)
    invariants = {
        "manual_entity_changed": manual_entity_change_planned,
        "verified_gold_changed": manual_gold_change_planned,
        "legacy_assignments_deleted": 0,
        "legacy_assignments_modified": 0,
        "clean_post_incident_runs_superseded": later_clean_superseded,
        "non_cohort_tools_changed": 0,
        "paid_model_calls": 0,
        "ambiguous_provenance_automatically_mutated": ambiguous_mutations,
    }
    blockers_for_apply = {
        "ambiguous_members": action_counts.get("ambiguous_provenance", 0)
        + action_counts.get("needs_manual_investigation", 0),
        "active_reprocess_requests": active_requests,
        "members_with_blockers": sum(bool(plan.get("blockers")) for plan in plans),
    }
    plan_basis = {
        "incident_id": incident_id,
        "manifest": {
            "captured_at": manifest.get("captured_at"),
            "prompt_version": manifest.get("prompt_version"),
            "member_fingerprint": manifest.get("member_fingerprint"),
        },
        "members": plans,
    }
    report = {
        "generated_at": utc_now_iso(),
        "mode": "dry-run-read-only-no-model-calls",
        "incident_id": incident_id,
        "manifest": manifest,
        "summary": {
            "cohort_total": len(plans),
            "apply_eligible": sum(bool(plan.get("apply_eligible")) for plan in plans),
            "legacy_mapping_retained": legacy_retained,
            "incident_assignments_found": incident_assignment_count,
            "profile_invalidations_planned": effect_counts.get("product_profile", 0),
            "action_counts": dict(sorted(action_counts.items())),
            "effect_counts": dict(sorted(effect_counts.items())),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "invariants": invariants,
        "apply_blockers": blockers_for_apply,
        "apply_ready": all(value == 0 for value in invariants.values())
        and all(value == 0 for value in blockers_for_apply.values()),
        "automatic_trigger_audit": {
            "required_runtime_freeze": "TAXONOMY_RECHECK_AUTO_NON_PRODUCT=0",
            "standard_backlog": "prior shadow terminal runs are skipped by current runner policy",
            "anomaly_scanner": "detector creates candidates only; admin approval is required to queue",
            "active_reprocess_requests_in_cohort": active_requests,
        },
        "rollback_plan_hash": _stable_hash(plan_basis),
        "members": plans,
    }
    return report


def _markdown_table(report: dict[str, Any]) -> str:
    counts = report["summary"]["action_counts"]
    labels = (
        ("cohort total", report["summary"]["cohort_total"]),
        ("manual protected / untouched", counts.get("manual_protected_untouched", 0)),
        (
            "already repaired by later clean run",
            counts.get("already_repaired_by_later_clean_run", 0),
        ),
        ("restore previous clean auto", counts.get("restore_previous_clean_auto", 0)),
        ("reset entity -> unresolved", counts.get("reset_entity_to_unresolved", 0)),
        ("no current effect / audit only", counts.get("no_current_effect_audit_only", 0)),
        ("ambiguous provenance", counts.get("ambiguous_provenance", 0)),
        ("needs manual investigation", counts.get("needs_manual_investigation", 0)),
        (
            "incident assignments -> superseded",
            report["summary"]["effect_counts"].get("taxonomy_assignment", 0),
        ),
        ("legacy mapping retained", report["summary"]["legacy_mapping_retained"]),
        (
            "polluted product profiles -> invalidated",
            report["summary"]["profile_invalidations_planned"],
        ),
    )
    lines = ["| rollback action | count |", "|---|---:|"]
    lines.extend(f"| {label} | {count} |" for label, count in labels)
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# {report['incident_id']} rollback dry-run",
            "",
            f"Generated: `{report['generated_at']}`",
            f"Mode: `{report['mode']}`",
            f"Frozen cohort fingerprint: `{report['manifest']['member_fingerprint']}`",
            f"Source candidate set: **{report['manifest']['source_cohort_total']}**; confirmed incident members: **{report['manifest']['cohort_total']}**; excluded non-incident records: **{report['manifest']['excluded_non_incident_count']}**",
            f"Rollback plan hash: `{report['rollback_plan_hash']}`",
            f"Apply ready: **{str(report['apply_ready']).lower()}**",
            "",
            "## Action buckets",
            "",
            _markdown_table(report),
            "",
            "## Hard invariants",
            "",
            "```json",
            json.dumps(report["invariants"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Apply blockers",
            "",
            "```json",
            json.dumps(report["apply_blockers"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Automatic-trigger audit",
            "",
            "```json",
            json.dumps(report["automatic_trigger_audit"], ensure_ascii=False, indent=2),
            "```",
            "",
            "The JSON companion contains every member, surviving run, blocker and proposed effect.",
        ]
    )


async def run(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    manifest_path = Path(args.manifest).resolve()
    manifest, members = load_frozen_cohort(manifest_path)
    if args.expected_count and len(members) != args.expected_count:
        raise RuntimeError(
            f"cohort safety check failed: expected {args.expected_count}, got {len(members)}"
        )
    if (
        args.expected_source_count
        and int(manifest.get("source_cohort_total") or 0) != args.expected_source_count
    ):
        raise RuntimeError(
            "source cohort safety check failed: "
            f"expected {args.expected_source_count}, "
            f"got {manifest.get('source_cohort_total')}"
        )
    if args.expected_prompt and manifest["prompt_version"] != args.expected_prompt:
        raise RuntimeError(
            "incident prompt mismatch: "
            f"expected {args.expected_prompt}, got {manifest['prompt_version']}"
        )

    config = load_config(require_brightdata=False)
    async with ReadOnlyD1Client(config) as d1:
        snapshot = await load_current_snapshot(
            d1, members, chunk_size=max(1, min(args.chunk_size, 80))
        )
    report = build_rollback_plan(
        incident_id=args.incident_id,
        manifest=manifest,
        members=members,
        snapshot=snapshot,
    )
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = output_dir / f"taxonomy-incident-rollback-{args.incident_id}-{stamp}"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report) + "\n", encoding="utf-8")
    return json_path, markdown_path, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--incident-id", default=DEFAULT_INCIDENT_ID)
    parser.add_argument("--expected-count", type=int, default=1369)
    parser.add_argument("--expected-source-count", type=int, default=1371)
    parser.add_argument("--expected-prompt", default=DEFAULT_INCIDENT_PROMPT)
    parser.add_argument("--chunk-size", type=int, default=40)
    parser.add_argument("--output-dir", default="logs")
    args = parser.parse_args()
    json_path, markdown_path, report = asyncio.run(run(args))
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print(f"apply_ready={str(report['apply_ready']).lower()}")
    print(f"rollback_plan_hash={report['rollback_plan_hash']}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")


if __name__ == "__main__":
    main()
