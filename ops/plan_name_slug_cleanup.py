"""Read-only Phase 1 Scheme A name & public slug cleanup planner (0 LLM calls).

Reads active tools (`published`, `pending_review`) and slug occupancy/aliases
from PostgreSQL in a strictly read-only transaction, applies Scheme A rules
from `libs.domains` and `workloads.assets.content`, and emits dry-run preview
CSVs plus summary Markdown for human approval before Phase 1 execution.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import runner  # noqa: E402  (import runner first to avoid assets.content circular import)
from libs.domains import (  # noqa: E402
    extract_domain_brand_parts,
    format_domain_brand_name,
    get_registrable_domain,
)
from workloads.assets.content import (  # noqa: E402
    RESERVED_PUBLIC_SLUGS,
    _TITLE_SEPARATOR_RE,
    _compact_name,
    _matches_domain_brand,
    clean_asset_text,
    clean_public_slug,
    invalid_tool_name_reason,
    public_tool_slug_candidates,
)
from workloads.assets.types import AssetTask  # noqa: E402

_HEX8_OR_DIGIT8_RE = re.compile(r"-[0-9a-f]{8}$")
_TRAILING_NUMBER_RE = re.compile(r"-\d+$")
_NOISE_SLUG_RE = re.compile(
    r"^(?:access-denied|just-a-moment|403-forbidden|404(?:-|$)|500(?:-|$)|"
    r"error(?:-|$)|attention-required|verify-you-are-human|security-check|"
    r"deployment-paused|under-construction|ai-directory-|home(?:-\d+)?$|"
    r"homepage(?:-\d+)?$|untitled(?:-\d+)?$)"
)


@dataclass(frozen=True)
class ToolSnapshot:
    tool_id: int
    status: str
    normalized_domain: str
    official_url: str
    canonical_slug: str
    has_localization: bool
    current_slug: str
    current_name: str
    name_source: str
    name_review_status: str
    monthly_visits: int


def _strict_brand_match(name: str, domain: str) -> bool:
    stem, tld = extract_domain_brand_parts(domain)
    stem_key = _compact_name(stem)
    tld_key = _compact_name(tld)
    host_key = _compact_name(domain.lower().removeprefix("www."))
    name_key = _compact_name(name)
    if len(stem_key) < 2:
        return False
    if name_key in {stem_key, f"{stem_key}ai", f"{stem_key}{tld_key}", host_key}:
        return True
    words = name.split()
    return (
        1 <= len(words) <= 3
        and len(stem_key) >= 4
        and (name_key.startswith(stem_key) or name_key.endswith(stem_key))
        and len(name_key) <= len(stem_key) + 6
    )


def _format_disambiguated_name(
    base_name: str,
    domain: str,
    *,
    include_subdomain: bool = False,
    force_full_domain: bool = False,
) -> str:
    _, tld = extract_domain_brand_parts(domain)
    tld_dot = tld.replace("-", ".")
    cleaned = clean_asset_text(base_name, 120)
    host = domain.lower().removeprefix("www.")
    if force_full_domain and host:
        return f"{host[:1].upper() + host[1:]}"[:120]
    if include_subdomain:
        first_label = host.split(".", 1)[0]
        reg = get_registrable_domain(host)
        if first_label and reg and host != reg:
            return f"{first_label[:1].upper() + first_label[1:]}.{reg}"[:120]
    if (
        cleaned
        and "." not in cleaned
        and _matches_domain_brand(cleaned, domain)
        and len(cleaned) + 1 + len(tld_dot) <= 50
    ):
        return f"{cleaned}.{tld_dot}"
    return format_domain_brand_name(domain, include_tld=True)


def _propose_name_initial(row: ToolSnapshot) -> tuple[str, str, str]:
    """Return ``(proposed_name, name_action, name_reason)`` before cross-tool disambiguation."""
    if not row.has_localization and row.status == "pending_review":
        return ("", "skip_unlocalized", "no_localization_row")

    current = clean_asset_text(row.current_name, 120)
    domain = row.normalized_domain
    if row.name_review_status == "human_approved" and current:
        return (current, "keep", "human_approved")

    invalid_reason = invalid_tool_name_reason(current, domain)
    if not invalid_reason:
        if row.name_source != "discovery_direct" or _strict_brand_match(current, domain):
            return (current, "keep", "clean_existing")
        invalid_reason = "discovery_direct_non_brand_headline"

    if current and _TITLE_SEPARATOR_RE.search(current):
        segments = [
            clean_asset_text(part, 120).strip(" -|–—:·•")
            for part in _TITLE_SEPARATOR_RE.split(current)
        ]
        matcher = _strict_brand_match if row.name_source == "discovery_direct" else _matches_domain_brand
        matching = next(
            (seg for seg in segments if seg and not invalid_tool_name_reason(seg, domain) and matcher(seg, domain)),
            "",
        )
        if matching:
            return (matching, "extract_brand_segment", invalid_reason or "title_segment")

    return (format_domain_brand_name(domain, include_tld=False), "domain_fallback", invalid_reason or "empty_name")


def _slug_dirty_reason(row: ToolSnapshot, proposed_name_changed: bool, allowed_base_slugs: set[str]) -> str:
    if not row.has_localization and row.status == "pending_review":
        return ""
    slug = (row.current_slug or "").strip().lower()
    if not slug or len(slug) < 2:
        return "empty_or_too_short"
    if _HEX8_OR_DIGIT8_RE.search(slug):
        return "hex8_hash_suffix"
    if len(slug) > 40:
        return "slug_too_long"
    if slug in RESERVED_PUBLIC_SLUGS or _NOISE_SLUG_RE.match(slug):
        return "noise_or_reserved_slug"
    if slug in allowed_base_slugs:
        return ""
    if _TRAILING_NUMBER_RE.search(slug):
        return "numbered_collision_slug"
    stem, _ = extract_domain_brand_parts(row.normalized_domain)
    stem_slug = clean_public_slug(stem)
    if proposed_name_changed and slug.count("-") >= 2 and (not stem_slug or stem_slug not in slug):
        return "dirty_seo_sentence_slug"
    if slug.count("-") >= 4 and (not stem_slug or stem_slug not in slug):
        return "over_hyphenated_non_brand_slug"
    return ""


def build_cleanup_plan(
    tools_rows: list[dict[str, Any]],
    *,
    alias_rows: list[dict[str, Any]] | None = None,
    inactive_slug_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute Scheme A Phase 1 plan rows, squatter aliases, and brand duplicates."""
    snapshots = [
        ToolSnapshot(
            tool_id=int(item["tool_id"]),
            status=str(item.get("status") or ""),
            normalized_domain=str(item.get("normalized_domain") or "").strip().lower(),
            official_url=str(item.get("official_url") or ""),
            canonical_slug=str(item.get("canonical_slug") or ""),
            has_localization=bool(item.get("localized_slug") or item.get("name")),
            current_slug=str(item.get("localized_slug") or "").strip().lower(),
            current_name=str(item.get("name") or ""),
            name_source=str(item.get("name_source") or ""),
            name_review_status=str(item.get("name_review_status") or ""),
            monthly_visits=int(item.get("monthly_visits") or 0),
        )
        for item in tools_rows
    ]

    ordered = sorted(
        snapshots,
        key=lambda r: (0 if r.status == "published" else 1, -r.monthly_visits, r.tool_id),
    )

    initial_names: dict[int, tuple[str, str, str]] = {
        r.tool_id: _propose_name_initial(r) for r in ordered
    }

    # First resolve false collisions where one tool holds a generic or mismatched name (e.g. gptagent.com named "Julius AI").
    pre_groups: dict[str, list[ToolSnapshot]] = defaultdict(list)
    for r in ordered:
        pname, paction, _ = initial_names[r.tool_id]
        if pname and paction != "skip_unlocalized":
            pre_groups[pname.strip().lower()].append(r)

    for key, peers in pre_groups.items():
        if len(peers) < 2:
            continue
        for r in peers:
            pname, paction, _ = initial_names[r.tool_id]
            if r.name_review_status == "human_approved" or paction == "domain_fallback":
                continue
            if not _strict_brand_match(pname, r.normalized_domain):
                fallback = format_domain_brand_name(r.normalized_domain, include_tld=False)
                if fallback.lower() != key:
                    initial_names[r.tool_id] = (fallback, "domain_fallback", f"collision_non_domain_name:{key}")

    by_lower_name: dict[str, list[ToolSnapshot]] = defaultdict(list)
    for r in ordered:
        pname, paction, _ = initial_names[r.tool_id]
        if pname and paction != "skip_unlocalized":
            by_lower_name[pname.strip().lower()].append(r)

    final_names: dict[int, tuple[str, str, str, bool]] = {}
    for r in ordered:
        pname, paction, preason = initial_names[r.tool_id]
        if paction == "skip_unlocalized" or not pname:
            final_names[r.tool_id] = (pname, paction, preason, False)

    brand_duplicates: list[dict[str, Any]] = []
    for key, peers in by_lower_name.items():
        if len(peers) < 2:
            r = peers[0]
            pname, paction, preason = initial_names[r.tool_id]
            final_names[r.tool_id] = (pname, paction, preason, False)
            continue

        # Check whether multiple peers share the exact same registrable root domain (subdomain split).
        reg_counts: dict[str, int] = defaultdict(int)
        for r in peers:
            reg_counts[get_registrable_domain(r.normalized_domain)] += 1

        seen_tld_names: set[str] = set()
        for idx, r in enumerate(peers):
            pname, paction, preason = initial_names[r.tool_id]
            reg = get_registrable_domain(r.normalized_domain)
            if r.name_review_status == "human_approved":
                final_names[r.tool_id] = (pname, paction, preason, True)
                seen_tld_names.add(pname.lower())
                continue
            same_root = reg_counts[reg] > 1 and r.normalized_domain.removeprefix("www.") != reg
            candidate_name = _format_disambiguated_name(pname, r.normalized_domain, include_subdomain=same_root)
            if candidate_name.lower() in seen_tld_names:
                candidate_name = _format_disambiguated_name(pname, r.normalized_domain, include_subdomain=True)
            if candidate_name.lower() in seen_tld_names:
                candidate_name = _format_disambiguated_name(pname, r.normalized_domain, force_full_domain=True)
            seen_tld_names.add(candidate_name.lower())
            changed = candidate_name != clean_asset_text(r.current_name, 120)
            final_names[r.tool_id] = (
                candidate_name,
                f"{paction}_with_tld" if changed else "keep",
                f"{preason};brand_collision:{key}",
                True,
            )
            brand_duplicates.append(
                {
                    "collision_key": key,
                    "tool_id": r.tool_id,
                    "status": r.status,
                    "monthly_visits": r.monthly_visits,
                    "normalized_domain": r.normalized_domain,
                    "current_name": r.current_name,
                "proposed_name": candidate_name,
            }
        )

    # Final global uniqueness sweep (handles e.g. MaxAI.me from collision pass vs pre-existing literal "MaxAI.me" on app.maxai.me).
    global_by_name: dict[str, list[ToolSnapshot]] = defaultdict(list)
    for r in ordered:
        pname, paction, _, _ = final_names[r.tool_id]
        if pname and paction != "skip_unlocalized":
            global_by_name[pname.strip().lower()].append(r)
    global_used_names: set[str] = {k for k in global_by_name}
    dup_by_id: dict[int, dict[str, Any]] = {int(d["tool_id"]): d for d in brand_duplicates}
    for key, peers in global_by_name.items():
        if len(peers) < 2:
            continue
        ordered_peers = sorted(
            peers,
            key=lambda p: (
                0 if p.name_review_status == "human_approved" else 1,
                0 if p.normalized_domain.removeprefix("www.") == get_registrable_domain(p.normalized_domain) else 1,
                0 if p.status == "published" else 1,
                -p.monthly_visits,
                p.tool_id,
            ),
        )
        for peer in ordered_peers[1:]:
            if peer.name_review_status == "human_approved":
                continue
            pname, paction, preason, _ = final_names[peer.tool_id]
            candidate = _format_disambiguated_name(pname, peer.normalized_domain, include_subdomain=True)
            if candidate.lower() in global_used_names:
                candidate = _format_disambiguated_name(pname, peer.normalized_domain, force_full_domain=True)
            if candidate.lower() in global_used_names:
                candidate = f"{candidate} ({peer.normalized_domain})"[:120]
            global_used_names.add(candidate.lower())
            base_action = paction.removesuffix("_with_tld")
            changed = candidate != clean_asset_text(peer.current_name, 120)
            final_names[peer.tool_id] = (
                candidate,
                f"{base_action}_with_tld" if changed else "keep",
                f"{preason};global_collision:{key}",
                True,
            )
            dup_by_id[peer.tool_id] = {
                "collision_key": key,
                "tool_id": peer.tool_id,
                "status": peer.status,
                "monthly_visits": peer.monthly_visits,
                "normalized_domain": peer.normalized_domain,
                "current_name": peer.current_name,
                "proposed_name": candidate,
            }
    brand_duplicates = list(dup_by_id.values())

    alias_by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for alias in alias_rows or []:
        slug = str(alias.get("slug") or "").strip().lower()
        if slug:
            alias_by_slug[slug].append(
                {"slug": slug, "tool_id": int(alias["tool_id"]), "created_at": str(alias.get("created_at") or "")}
            )

    inactive_by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inactive_slug_rows or []:
        slug = str(row.get("localized_slug") or "").strip().lower()
        if slug:
            inactive_by_slug[slug].append(
                {
                    "tool_id": int(row["tool_id"]),
                    "status": str(row.get("status") or ""),
                    "normalized_domain": str(row.get("normalized_domain") or ""),
                    "localized_slug": slug,
                }
            )

    candidates_by_id: dict[int, list[str]] = {}
    dirty_reason_by_id: dict[int, str] = {}
    used_active_slugs: set[str] = set()

    for r in ordered:
        pname, paction, _, _ = final_names[r.tool_id]
        if paction == "skip_unlocalized":
            candidates_by_id[r.tool_id] = []
            dirty_reason_by_id[r.tool_id] = "skip_unlocalized"
            continue
        name_changed = pname != clean_asset_text(r.current_name, 120)
        task = AssetTask(
            tool_id=r.tool_id,
            canonical_slug=r.canonical_slug,
            normalized_domain=r.normalized_domain,
            official_url=r.official_url,
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="dry-run",
        )
        cands = public_tool_slug_candidates(task, pname)
        candidates_by_id[r.tool_id] = cands
        dirty_reason = _slug_dirty_reason(r, name_changed, set(cands[:3]))
        dirty_reason_by_id[r.tool_id] = dirty_reason
        if not dirty_reason and r.current_slug not in used_active_slugs:
            used_active_slugs.add(r.current_slug)

    plan_rows: list[dict[str, Any]] = []
    squatter_conflicts: list[dict[str, Any]] = []

    for r in ordered:
        pname, paction, preason, is_brand_dup = final_names[r.tool_id]
        cands = candidates_by_id[r.tool_id]
        dirty_reason = dirty_reason_by_id[r.tool_id]

        if dirty_reason == "skip_unlocalized":
            proposed_slug = r.current_slug
            slug_action = "skip_unlocalized"
            slug_reason = "no_localization_row"
        elif not dirty_reason and r.current_slug in used_active_slugs:
            proposed_slug = r.current_slug
            slug_action = "keep"
            slug_reason = "clean_existing"
        else:
            effective_reason = dirty_reason or "active_slug_duplicate"
            proposed_slug = next((c for c in cands if c not in used_active_slugs), f"tool-{r.tool_id}")
            used_active_slugs.add(proposed_slug)
            if proposed_slug == r.current_slug:
                slug_action = "keep"
                slug_reason = "matches_scheme_a"
            else:
                slug_action = "replace_slug"
                slug_reason = effective_reason

        if slug_action == "replace_slug":
            for alias_hit in alias_by_slug.get(proposed_slug, []):
                if alias_hit["tool_id"] != r.tool_id:
                    squatter_conflicts.append(
                        {
                            "conflict_type": "tool_slug_alias",
                            "desired_slug": proposed_slug,
                            "winner_tool_id": r.tool_id,
                            "winner_domain": r.normalized_domain,
                            "winner_status": r.status,
                            "winner_visits": r.monthly_visits,
                            "squatter_tool_id": alias_hit["tool_id"],
                            "squatter_status": "alias",
                            "squatter_domain": "",
                        }
                    )
            for inactive_hit in inactive_by_slug.get(proposed_slug, []):
                if inactive_hit["tool_id"] != r.tool_id:
                    squatter_conflicts.append(
                        {
                            "conflict_type": "inactive_tool_localization",
                            "desired_slug": proposed_slug,
                            "winner_tool_id": r.tool_id,
                            "winner_domain": r.normalized_domain,
                            "winner_status": r.status,
                            "winner_visits": r.monthly_visits,
                            "squatter_tool_id": inactive_hit["tool_id"],
                            "squatter_status": inactive_hit["status"],
                            "squatter_domain": inactive_hit["normalized_domain"],
                        }
                    )

        name_changed = paction != "skip_unlocalized" and pname != r.current_name
        slug_changed = slug_action != "skip_unlocalized" and proposed_slug != r.current_slug
        plan_rows.append(
            {
                "tool_id": r.tool_id,
                "status": r.status,
                "monthly_visits": r.monthly_visits,
                "normalized_domain": r.normalized_domain,
                "name_source": r.name_source,
                "name_review_status": r.name_review_status,
                "current_name": r.current_name,
                "proposed_name": pname,
                "name_changed": int(name_changed),
                "name_action": paction,
                "name_reason": preason,
                "current_slug": r.current_slug,
                "proposed_slug": proposed_slug,
                "slug_changed": int(slug_changed),
                "slug_action": slug_action,
                "slug_reason": slug_reason,
                "is_brand_duplicate": int(is_brand_dup),
                "canonical_slug": r.canonical_slug,
            }
        )

    return {
        "plan_rows": plan_rows,
        "squatter_conflicts": squatter_conflicts,
        "brand_duplicates": brand_duplicates,
    }


def _resolve_read_dsn(explicit_dsn: str = "") -> str:
    if explicit_dsn.strip():
        return explicit_dsn.strip()
    if os.environ.get("DATABASE_URL", "").strip():
        return os.environ["DATABASE_URL"].strip()
    dev_vars_path = _REPO_ROOT.parent / "sigpik" / ".dev.vars"
    if dev_vars_path.exists():
        for raw_line in dev_vars_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("SIGPIK_DEV_DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("No PostgreSQL DSN provided; pass --dsn or set DATABASE_URL")


def _fetch_pg_snapshot(dsn: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                """
                SELECT
                  t.id AS tool_id,
                  t.status,
                  t.normalized_domain,
                  t.official_url,
                  t.canonical_slug,
                  COALESCE(l.localized_slug, '') AS localized_slug,
                  COALESCE(l.name, '') AS name,
                  COALESCE(l.name_source, '') AS name_source,
                  COALESCE(l.name_review_status, '') AS name_review_status,
                  COALESCE(m.visits, 0)::bigint AS monthly_visits
                FROM tools t
                LEFT JOIN tool_localizations l
                  ON l.tool_id = t.id AND l.locale_code = 'en'
                LEFT JOIN (
                  SELECT normalized_domain, MAX(visits) AS visits
                  FROM domain_traffic_monthly
                  GROUP BY normalized_domain
                ) m ON m.normalized_domain = t.normalized_domain
                WHERE t.status IN ('published', 'pending_review')
                  AND t.duplicate_of_tool_id IS NULL
                ORDER BY t.id ASC
                """
            )
            cols = [d.name for d in cur.description]
            tools_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

            cur.execute("SELECT slug, tool_id, created_at FROM tool_slug_aliases WHERE locale_code = 'en'")
            acols = [d.name for d in cur.description]
            alias_rows = [dict(zip(acols, row)) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT t.id AS tool_id, t.status, t.normalized_domain, l.localized_slug
                FROM tool_localizations l
                JOIN tools t ON t.id = l.tool_id
                WHERE l.locale_code = 'en'
                  AND COALESCE(trim(l.localized_slug), '') <> ''
                  AND NOT (t.status IN ('published', 'pending_review') AND t.duplicate_of_tool_id IS NULL)
                """
            )
            icols = [d.name for d in cur.description]
            inactive_rows = [dict(zip(icols, row)) for row in cur.fetchall()]

    return tools_rows, alias_rows, inactive_rows


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_plan_artifacts(result: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_rows: list[dict[str, Any]] = result["plan_rows"]
    changed_rows = [r for r in plan_rows if r["name_changed"] or r["slug_changed"]]
    changed_top500 = sorted(
        changed_rows,
        key=lambda r: (-int(r["monthly_visits"]), int(r["tool_id"])),
    )[:500]

    plan_fields = [
        "tool_id",
        "status",
        "monthly_visits",
        "normalized_domain",
        "name_source",
        "name_review_status",
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
        "is_brand_duplicate",
        "canonical_slug",
    ]
    _write_csv(out_dir / "phase1-top500-preview.csv", changed_top500, plan_fields)
    _write_csv(out_dir / "phase1-full-preview.csv", plan_rows, plan_fields)

    squatter_fields = [
        "conflict_type",
        "desired_slug",
        "winner_tool_id",
        "winner_domain",
        "winner_status",
        "winner_visits",
        "squatter_tool_id",
        "squatter_status",
        "squatter_domain",
    ]
    _write_csv(out_dir / "phase1-squatter-aliases.csv", result["squatter_conflicts"], squatter_fields)

    dup_fields = [
        "collision_key",
        "tool_id",
        "status",
        "monthly_visits",
        "normalized_domain",
        "current_name",
        "proposed_name",
    ]
    _write_csv(out_dir / "phase1-brand-duplicates.csv", result["brand_duplicates"], dup_fields)

    stats = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_active_tools": len(plan_rows),
        "published_tools": sum(1 for r in plan_rows if r["status"] == "published"),
        "pending_review_tools": sum(1 for r in plan_rows if r["status"] == "pending_review"),
        "skipped_unlocalized_pending": sum(1 for r in plan_rows if r["name_action"] == "skip_unlocalized"),
        "name_changes": sum(1 for r in plan_rows if r["name_changed"]),
        "slug_changes": sum(1 for r in plan_rows if r["slug_changed"]),
        "total_changed_tools": len(changed_rows),
        "brand_duplicate_tools": len(result["brand_duplicates"]),
        "squatter_conflicts": len(result["squatter_conflicts"]),
    }
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 1 Scheme A cleanup planner")
    parser.add_argument("--out-dir", required=True, help="Output directory for preview CSVs")
    parser.add_argument("--dsn", default="", help="Optional PostgreSQL DSN (defaults to env or local dev vars)")
    args = parser.parse_args(argv)

    dsn = _resolve_read_dsn(args.dsn)
    tools_rows, alias_rows, inactive_rows = _fetch_pg_snapshot(dsn)
    result = build_cleanup_plan(
        tools_rows,
        alias_rows=alias_rows,
        inactive_slug_rows=inactive_rows,
    )
    stats = write_plan_artifacts(result, Path(args.out_dir))
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
