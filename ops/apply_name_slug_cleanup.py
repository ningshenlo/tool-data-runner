"""Safe transactional applier for Phase 1 Scheme A name & public slug cleanup.

Defaults to ``--dry-run`` (strictly read-only validation and manifest output).
Pass ``--apply`` to execute the validated rewrite in a single atomic PostgreSQL
transaction after saving ``before-snapshot.json`` for full rollback capability.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import runner  # noqa: E402
from ops.plan_name_slug_cleanup import (  # noqa: E402
    _fetch_pg_snapshot,
    _resolve_read_dsn,
    build_cleanup_plan,
    write_plan_artifacts,
)


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def validate_cleanup_plan(
    plan: dict[str, Any],
    *,
    canonical_slug_owners: dict[str, int],
    active_slug_owners: dict[str, int],
    active_alias_owners: dict[str, set[int]],
) -> dict[str, Any]:
    """Run strict pre-flight assertions before any database write is permitted."""
    plan_rows: list[dict[str, Any]] = list(plan["plan_rows"])
    seen_names: dict[str, int] = {}
    seen_slugs: dict[str, int] = {}

    for row in plan_rows:
        tid = int(row["tool_id"])
        if row["name_action"] == "skip_unlocalized":
            continue

        if row["name_review_status"] == "human_approved" and int(row["name_changed"]):
            raise ValueError(f"Refusing to overwrite human_approved name on tool {tid}")

        if row["proposed_slug"] == row["canonical_slug"] and int(row["slug_changed"]):
            # Allowed only when canonical_slug already belongs to this same tool_id.
            if canonical_slug_owners.get(row["proposed_slug"], tid) != tid:
                raise ValueError(f"Proposed slug collides with canonical_slug of another tool: {tid}")

        pname_key = str(row["proposed_name"] or "").strip().lower()
        pslug_key = str(row["proposed_slug"] or "").strip().lower()
        if not pname_key:
            raise ValueError(f"Empty proposed_name for localized tool {tid}")
        if not pslug_key:
            raise ValueError(f"Empty proposed_slug for localized tool {tid}")

        if pname_key in seen_names:
            raise ValueError(f"Duplicate proposed_name '{pname_key}' on tools {seen_names[pname_key]} and {tid}")
        seen_names[pname_key] = tid

        if pslug_key in seen_slugs:
            raise ValueError(f"Duplicate proposed_slug '{pslug_key}' on tools {seen_slugs[pslug_key]} and {tid}")
        seen_slugs[pslug_key] = tid

        canon_owner = canonical_slug_owners.get(pslug_key)
        if canon_owner is not None and canon_owner != tid:
            raise ValueError(
                f"Proposed slug '{pslug_key}' for tool {tid} collides with canonical_slug of tool {canon_owner}"
            )

        if int(row["slug_changed"]):
            alias_owners = active_alias_owners.get(pslug_key, set())
            foreign_alias = [aid for aid in alias_owners if aid != tid]
            if foreign_alias:
                raise ValueError(
                    f"Proposed slug '{pslug_key}' for tool {tid} is an active alias of tool(s) {foreign_alias}"
                )
            loc_owner = active_slug_owners.get(pslug_key)
            if loc_owner is not None and loc_owner != tid:
                # Allowed only if loc_owner is ALSO changing away in this same plan.
                owner_row = next((x for x in plan_rows if int(x["tool_id"]) == loc_owner), None)
                if not owner_row or not int(owner_row["slug_changed"]) or owner_row["proposed_slug"] == pslug_key:
                    raise ValueError(
                        f"Proposed slug '{pslug_key}' for tool {tid} is still held by active tool {loc_owner}"
                    )

    changed_rows = [r for r in plan_rows if int(r["name_changed"]) or int(r["slug_changed"])]
    reclaim_loc_by_tid: dict[int, dict[str, Any]] = {}
    reclaim_aliases: dict[tuple[str, int], dict[str, Any]] = {}

    active_id_set = {int(r["tool_id"]) for r in plan_rows}
    for conflict in plan.get("squatter_conflicts", []):
        squatter_id = int(conflict["squatter_tool_id"])
        if squatter_id in active_id_set:
            raise ValueError(f"Refusing to reclaim slug from active tool {squatter_id}")
        desired_slug = str(conflict["desired_slug"]).strip().lower()
        if conflict["conflict_type"] == "inactive_tool_localization":
            reclaim_loc_by_tid[squatter_id] = dict(conflict)
            reclaim_aliases[(desired_slug, squatter_id)] = {
                "slug": desired_slug,
                "tool_id": squatter_id,
                "reason": "reclaimed_from_inactive_localization",
            }
        elif conflict["conflict_type"] == "tool_slug_alias":
            reclaim_aliases[(desired_slug, squatter_id)] = {
                "slug": desired_slug,
                "tool_id": squatter_id,
                "reason": "reclaimed_from_inactive_alias",
            }

    return {
        "changed_rows": changed_rows,
        "reclaim_localizations": list(reclaim_loc_by_tid.values()),
        "reclaim_aliases": list(reclaim_aliases.values()),
        "summary": {
            "total_active_tools": len(plan_rows),
            "changed_tools": len(changed_rows),
            "name_changes": sum(1 for r in changed_rows if int(r["name_changed"])),
            "slug_changes": sum(1 for r in changed_rows if int(r["slug_changed"])),
            "reclaim_localizations": len(reclaim_loc_by_tid),
            "reclaim_aliases": len(reclaim_aliases),
        },
    }


def _name_metadata_for_update(row: dict[str, Any], existing_source: str, existing_conf: int | None) -> tuple[str, int]:
    action = str(row["name_action"] or "")
    if action.startswith("extract_brand_segment"):
        return ("page_title_segment", 86)
    if action.startswith("domain_fallback"):
        return ("domain_fallback", 80)
    src = (existing_source or "").strip() or "domain_fallback"
    conf = int(existing_conf) if existing_conf is not None else 85
    return (src, max(0, min(100, conf)))


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _fetch_before_snapshot(
    dsn: str,
    changed_ids: list[int],
    reclaim_loc_ids: list[int],
    reclaim_alias_pairs: list[tuple[str, int]],
) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                """
                SELECT tool_id, locale_code, localized_slug, name, name_source,
                       name_confidence, name_evidence, name_review_status,
                       last_name_checked_at, updated_at
                FROM tool_localizations
                WHERE locale_code = 'en' AND tool_id = ANY(%s)
                ORDER BY tool_id
                """,
                (changed_ids,),
            )
            loc_cols = [d.name for d in cur.description]
            changed_before = [dict(zip(loc_cols, r)) for r in cur.fetchall()]

            reclaim_loc_before: list[dict[str, Any]] = []
            if reclaim_loc_ids:
                cur.execute(
                    """
                    SELECT tool_id, locale_code, localized_slug, name, updated_at
                    FROM tool_localizations
                    WHERE locale_code = 'en' AND tool_id = ANY(%s)
                    ORDER BY tool_id
                    """,
                    (reclaim_loc_ids,),
                )
                rcols = [d.name for d in cur.description]
                reclaim_loc_before = [dict(zip(rcols, r)) for r in cur.fetchall()]

            reclaim_alias_before: list[dict[str, Any]] = []
            for slug, tid in reclaim_alias_pairs:
                cur.execute(
                    """
                    SELECT locale_code, slug, tool_id, created_at
                    FROM tool_slug_aliases
                    WHERE locale_code = 'en' AND slug = %s AND tool_id = %s
                    """,
                    (slug, tid),
                )
                acols = [d.name for d in cur.description]
                for r in cur.fetchall():
                    reclaim_alias_before.append(dict(zip(acols, r)))

            cur.execute("SELECT lower(canonical_slug) FROM tools WHERE COALESCE(trim(canonical_slug), '') <> ''")
            all_canon = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT lower(localized_slug) FROM tool_localizations WHERE locale_code = 'en'")
            all_locs = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT lower(slug) FROM tool_slug_aliases WHERE locale_code = 'en'")
            all_aliases = [r[0] for r in cur.fetchall()]

    return {
        "captured_at": _utc_now_iso(),
        "changed_localizations": changed_before,
        "reclaimed_inactive_localizations": reclaim_loc_before,
        "reclaimed_inactive_aliases": reclaim_alias_before,
        "reserved_slugs": sorted(set(all_canon) | set(all_locs) | set(all_aliases)),
    }


def apply_cleanup(dsn: str, out_dir: Path, *, apply: bool = False) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tools_rows, alias_rows, inactive_rows, canon_owners, active_alias_owners = _fetch_pg_snapshot(dsn)
    plan = build_cleanup_plan(
        tools_rows,
        alias_rows=alias_rows,
        inactive_slug_rows=inactive_rows,
        canonical_slug_owners=canon_owners,
        active_alias_owners=active_alias_owners,
    )
    active_slug_owners = {
        str(r["localized_slug"]).strip().lower(): int(r["tool_id"])
        for r in tools_rows
        if str(r.get("localized_slug") or "").strip()
    }
    validated = validate_cleanup_plan(
        plan,
        canonical_slug_owners=canon_owners,
        active_slug_owners=active_slug_owners,
        active_alias_owners=active_alias_owners,
    )

    write_plan_artifacts(plan, out_dir)
    changed_rows = validated["changed_rows"]
    reclaim_locs = validated["reclaim_localizations"]
    reclaim_aliases = validated["reclaim_aliases"]

    changed_ids = [int(r["tool_id"]) for r in changed_rows]
    reclaim_loc_ids = [int(r["squatter_tool_id"]) for r in reclaim_locs]
    reclaim_alias_pairs = [(str(r["slug"]), int(r["tool_id"])) for r in reclaim_aliases]

    snapshot = _fetch_before_snapshot(dsn, changed_ids, reclaim_loc_ids, reclaim_alias_pairs)
    reserved_slugs: set[str] = set(snapshot.pop("reserved_slugs"))
    before_by_tid = {int(r["tool_id"]): r for r in snapshot["changed_localizations"]}

    if len(before_by_tid) != len(changed_ids):
        missing = sorted(set(changed_ids) - set(before_by_tid))
        raise RuntimeError(f"Missing tool_localizations rows for changed IDs: {missing[:10]}")

    reclaim_manifest_rows: list[dict[str, Any]] = []
    parked_by_squatter_id: dict[int, str] = {}
    for row in snapshot["reclaimed_inactive_localizations"]:
        stid = int(row["tool_id"])
        old_s = str(row["localized_slug"]).strip().lower()
        base = f"{old_s[:52]}-inactive-{stid}"
        cand = base
        idx = 2
        while cand in reserved_slugs:
            cand = f"{base}-{idx}"
            idx += 1
        reserved_slugs.add(cand)
        parked_by_squatter_id[stid] = cand
        reclaim_manifest_rows.append(
            {
                "squatter_tool_id": stid,
                "old_localized_slug": old_s,
                "parked_localized_slug": cand,
                "reclaimed_alias_slugs": ";".join(
                    s for s, aid in reclaim_alias_pairs if aid == stid
                ),
            }
        )

    manifest_fields = [
        "tool_id",
        "status",
        "monthly_visits",
        "normalized_domain",
        "current_name",
        "proposed_name",
        "name_changed",
        "name_action",
        "name_reason",
        "current_slug",
        "proposed_slug",
        "slug_changed",
        "slug_action",
        "slug_reason",
    ]
    _write_csv(out_dir / "phase1-applied-manifest.csv", [{k: r[k] for k in manifest_fields} for r in changed_rows], manifest_fields)
    _write_csv(
        out_dir / "phase1-reclaimed-squatters.csv",
        reclaim_manifest_rows,
        ["squatter_tool_id", "old_localized_slug", "parked_localized_slug", "reclaimed_alias_slugs"],
    )

    if not apply:
        return {"mode": "dry_run", **validated["summary"]}

    (out_dir / "before-snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    import psycopg

    now_iso = _utc_now_iso()
    slug_changes = [r for r in changed_rows if int(r["slug_changed"])]
    wanted_changed_slugs = {r["proposed_slug"] for r in slug_changes}
    temp_slug_by_tid: dict[int, str] = {}
    for r in slug_changes:
        tid = int(r["tool_id"])
        # If another changing tool wants this row's current_slug (or vice versa), park it briefly.
        if r["current_slug"] in wanted_changed_slugs or active_slug_owners.get(r["proposed_slug"]) not in (None, tid):
            tmp = f"p1tmp-{tid}"
            idx = 2
            while tmp in reserved_slugs:
                tmp = f"p1tmp-{tid}-{idx}"
                idx += 1
            reserved_slugs.add(tmp)
            temp_slug_by_tid[tid] = tmp

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # 1. Park inactive squatter localizations & delete reclaimed inactive aliases.
            for stid, parked_slug in parked_by_squatter_id.items():
                cur.execute(
                    """
                    UPDATE tool_localizations
                    SET localized_slug = %s, updated_at = %s
                    WHERE tool_id = %s AND locale_code = 'en'
                    """,
                    (parked_slug, now_iso, stid),
                )
            for slug, aid in reclaim_alias_pairs:
                cur.execute(
                    """
                    DELETE FROM tool_slug_aliases
                    WHERE locale_code = 'en' AND slug = %s AND tool_id = %s
                    """,
                    (slug, aid),
                )
            for stid, parked_slug in parked_by_squatter_id.items():
                cur.execute(
                    """
                    DELETE FROM tool_slug_aliases
                    WHERE locale_code = 'en' AND slug = %s AND tool_id = %s
                    """,
                    (parked_slug, stid),
                )

            # 2. Park any active rows involved in slug swaps/chains.
            for tid, tmp_slug in temp_slug_by_tid.items():
                cur.execute(
                    """
                    UPDATE tool_localizations
                    SET localized_slug = %s, updated_at = %s
                    WHERE tool_id = %s AND locale_code = 'en'
                    """,
                    (tmp_slug, now_iso, tid),
                )

            # 3. Apply all Scheme A name & public slug updates + tool_change_log audit entries.
            for r in changed_rows:
                tid = int(r["tool_id"])
                before = before_by_tid[tid]
                name_changed = bool(int(r["name_changed"]))
                slug_changed = bool(int(r["slug_changed"]))

                set_clauses = ["updated_at = %s"]
                params: list[Any] = [now_iso]

                if name_changed:
                    new_src, new_conf = _name_metadata_for_update(
                        r, str(before.get("name_source") or ""), before.get("name_confidence")
                    )
                    new_evidence = json.dumps(
                        {
                            "ruleset": "phase1-scheme-a-2026-11-24",
                            "previous_name": r["current_name"],
                            "action": r["name_action"],
                            "reason": r["name_reason"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    set_clauses.extend(
                        [
                            "name = %s",
                            "name_source = %s",
                            "name_confidence = %s",
                            "name_evidence = %s",
                            "name_review_status = 'auto_approved'",
                            "last_name_checked_at = %s",
                        ]
                    )
                    params.extend([r["proposed_name"], new_src, new_conf, new_evidence, now_iso])

                if slug_changed:
                    set_clauses.append("localized_slug = %s")
                    params.append(r["proposed_slug"])

                params.append(tid)
                cur.execute(
                    f"UPDATE tool_localizations SET {', '.join(set_clauses)} WHERE tool_id = %s AND locale_code = 'en'",
                    params,
                )

                old_json = json.dumps(
                    {
                        "name": before["name"],
                        "localized_slug": before["localized_slug"],
                        "name_source": before["name_source"],
                        "name_review_status": before["name_review_status"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                new_json = json.dumps(
                    {
                        "name": r["proposed_name"],
                        "localized_slug": r["proposed_slug"],
                        "name_action": r["name_action"],
                        "slug_action": r["slug_action"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cur.execute(
                    """
                    INSERT INTO tool_change_log (
                      tool_id, change_type, old_value, new_value, detected_at, verified_at, notes, created_at, updated_at
                    ) VALUES (%s, 'localization_updated', %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tid, old_json, new_json, now_iso, now_iso, "Phase 1 Scheme A name/slug cleanup", now_iso, now_iso),
                )

            if temp_slug_by_tid:
                cur.execute(
                    "DELETE FROM tool_slug_aliases WHERE locale_code = 'en' AND slug LIKE 'p1tmp-%%'"
                )

            cur.execute(
                "UPDATE tools SET updated_at = %s WHERE id = ANY(%s)",
                (now_iso, changed_ids),
            )

            # 4. In-transaction safety invariants check before commit.
            cur.execute("SELECT COUNT(*)::int FROM tool_slug_aliases WHERE locale_code = 'en' AND slug LIKE 'p1tmp-%%'")
            tmp_leftovers = cur.fetchone()[0]
            if tmp_leftovers != 0:
                raise RuntimeError(f"Temp aliases left over in transaction: {tmp_leftovers}")

            cur.execute(
                """
                SELECT COUNT(*)::int FROM (
                  SELECT lower(l.localized_slug)
                  FROM tool_localizations l
                  JOIN tools t ON t.id = l.tool_id
                  WHERE l.locale_code = 'en'
                    AND t.status IN ('published', 'pending_review')
                    AND t.duplicate_of_tool_id IS NULL
                  GROUP BY 1
                  HAVING COUNT(*) > 1
                ) dup_slugs
                """
            )
            dup_slugs = cur.fetchone()[0]
            if dup_slugs != 0:
                raise RuntimeError(f"Active localized_slug duplicates detected in transaction: {dup_slugs}")

            cur.execute(
                """
                SELECT COUNT(*)::int FROM (
                  SELECT lower(l.name)
                  FROM tool_localizations l
                  JOIN tools t ON t.id = l.tool_id
                  WHERE l.locale_code = 'en'
                    AND t.status IN ('published', 'pending_review')
                    AND t.duplicate_of_tool_id IS NULL
                  GROUP BY 1
                  HAVING COUNT(*) > 1
                ) dup_names
                """
            )
            dup_names = cur.fetchone()[0]
            if dup_names != 0:
                raise RuntimeError(f"Active name duplicates detected in transaction: {dup_names}")

        conn.commit()

    return {
        "mode": "applied",
        "applied_at": now_iso,
        "temp_parked_chains": len(temp_slug_by_tid),
        **validated["summary"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transactional Phase 1 Scheme A cleanup applier")
    parser.add_argument("--out-dir", required=True, help="Directory for manifests, backup snapshot, and logs")
    parser.add_argument("--dsn", default="", help="Optional PostgreSQL DSN")
    parser.add_argument("--apply", action="store_true", help="Commit changes to PostgreSQL (default is read-only dry-run)")
    args = parser.parse_args(argv)

    dsn = _resolve_read_dsn(args.dsn)
    summary = apply_cleanup(dsn, Path(args.out_dir), apply=bool(args.apply))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
