"""Legacy category compatibility projection for the canonical taxonomy tables.

The multidimensional taxonomy remains the authority. This module projects its
effective primary assignment into ``tools.primary_category_id`` and
``tool_categories`` for consumers that have not yet migrated to taxonomy terms.
It is intentionally separate from model execution and never creates taxonomy
assignments itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

LEGACY_CATEGORY_MATERIALIZATION_VERSION = "legacy-materializer-v1-2026-08-06"


PENDING_LEGACY_PROJECTIONS_SQL = """
WITH ranked_assignments AS (
  SELECT
    assignment.id AS assignment_id,
    assignment.tool_id,
    assignment.term_id,
    assignment.source AS assignment_source,
    assignment.decision_status,
    term.slug,
    term.parent_id,
    term.source_category_id,
    ROW_NUMBER() OVER (
      PARTITION BY assignment.tool_id
      ORDER BY
        CASE assignment.decision_status
          WHEN 'verified' THEN 0
          WHEN 'auto_accepted' THEN 1
          WHEN 'legacy' THEN 2
          ELSE 3
        END,
        CASE assignment.source
          WHEN 'manual' THEN 0
          WHEN 'auto' THEN 1
          ELSE 2
        END,
        COALESCE(
          assignment.reviewed_at,
          assignment.updated_at,
          assignment.assigned_at
        ) DESC,
        assignment.id DESC
    ) AS assignment_rank
  FROM product_taxonomy_assignments assignment
  JOIN taxonomy_terms term
    ON term.id = assignment.term_id
   AND term.dimension = 'primary_category'
   AND term.status = 'active'
  WHERE assignment.is_primary = 1
    AND assignment.decision_status IN ('verified', 'auto_accepted', 'legacy')
),
effective_assignments AS (
  SELECT *
  FROM ranked_assignments
  WHERE assignment_rank = 1
)
SELECT
  effective.assignment_id,
  effective.tool_id,
  effective.term_id,
  effective.assignment_source,
  effective.decision_status,
  effective.slug,
  effective.parent_id,
  effective.source_category_id,
  parent.slug AS parent_slug,
  parent.source_category_id AS parent_source_category_id
FROM effective_assignments effective
JOIN tools tool ON tool.id = effective.tool_id
LEFT JOIN taxonomy_terms parent
  ON parent.id = effective.parent_id
 AND parent.dimension = 'primary_category'
 AND parent.status = 'active'
WHERE tool.status IN ('pending_enrich', 'pending_review', 'published')
  AND tool.duplicate_of_tool_id IS NULL
  AND effective.source_category_id IS NOT NULL
  AND (effective.parent_id IS NULL OR parent.source_category_id IS NOT NULL)
  AND (
    NOT EXISTS (
      SELECT 1
      FROM legacy_category_materializations materialization
      WHERE materialization.tool_id = effective.tool_id
        AND materialization.leaf_term_id = effective.term_id
        AND materialization.materialization_version = ?
        AND materialization.primary_category_id = COALESCE(
          parent.source_category_id,
          effective.source_category_id
        )
    )
    OR tool.primary_category_id IS NOT COALESCE(
      parent.source_category_id,
      effective.source_category_id
    )
    OR NOT json_valid(COALESCE(tool.category_classification_raw, ''))
    OR CASE
         WHEN json_valid(COALESCE(tool.category_classification_raw, ''))
         THEN json_extract(tool.category_classification_raw, '$.mode')
       END IS NOT 'taxonomy_compatibility_projection'
    OR CASE
         WHEN json_valid(COALESCE(tool.category_classification_raw, ''))
         THEN json_extract(tool.category_classification_raw, '$.assignment_id')
       END IS NOT effective.assignment_id
    OR NOT EXISTS (
      SELECT 1 FROM tool_categories projected_leaf
      WHERE projected_leaf.tool_id = effective.tool_id
        AND projected_leaf.category_id = effective.source_category_id
    )
    OR (
      parent.source_category_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM tool_categories projected_parent
        WHERE projected_parent.tool_id = effective.tool_id
          AND projected_parent.category_id = parent.source_category_id
      )
    )
  )
ORDER BY effective.tool_id
LIMIT ?
"""


class LegacyMaterializationError(ValueError):
    pass


@dataclass(frozen=True)
class TaxonomyTermRef:
    term_id: int
    slug: str
    parent_term_id: int | None
    source_category_id: int | None


@dataclass(frozen=True)
class LegacyCategoryWritePlan:
    materialization_version: str
    leaf_term_id: int
    leaf_category_id: int
    primary_category_id: int
    category_ids: list[int]
    parent_category_id: int | None


def materialize_legacy_category(
    leaf: TaxonomyTermRef,
    parent: TaxonomyTermRef | None = None,
) -> LegacyCategoryWritePlan:
    if not leaf.source_category_id or leaf.source_category_id <= 0:
        raise LegacyMaterializationError(
            f"Leaf term {leaf.term_id} ({leaf.slug}) has no source_category_id"
        )

    if leaf.parent_term_id is not None:
        if parent is None:
            raise LegacyMaterializationError(
                f"Leaf term {leaf.term_id} requires parent {leaf.parent_term_id}"
            )
        if parent.term_id != leaf.parent_term_id:
            raise LegacyMaterializationError(
                f"Parent term id mismatch: expected {leaf.parent_term_id}, got {parent.term_id}"
            )
        if not parent.source_category_id or parent.source_category_id <= 0:
            raise LegacyMaterializationError(
                f"Parent term {parent.term_id} ({parent.slug}) has no source_category_id"
            )
        parent_category_id = parent.source_category_id
        leaf_category_id = leaf.source_category_id
        category_ids = _unique_positive([parent_category_id, leaf_category_id])
        return LegacyCategoryWritePlan(
            materialization_version=LEGACY_CATEGORY_MATERIALIZATION_VERSION,
            leaf_term_id=leaf.term_id,
            leaf_category_id=leaf_category_id,
            primary_category_id=parent_category_id,
            category_ids=category_ids,
            parent_category_id=parent_category_id,
        )

    if parent is not None:
        raise LegacyMaterializationError(
            f"Parent was supplied for root leaf term {leaf.term_id} ({leaf.slug})"
        )

    leaf_category_id = leaf.source_category_id
    return LegacyCategoryWritePlan(
        materialization_version=LEGACY_CATEGORY_MATERIALIZATION_VERSION,
        leaf_term_id=leaf.term_id,
        leaf_category_id=leaf_category_id,
        primary_category_id=leaf_category_id,
        category_ids=[leaf_category_id],
        parent_category_id=None,
    )


def _unique_positive(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in ids:
        if value > 0 and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _term_ref_from_projection_row(
    row: dict[str, Any],
) -> tuple[TaxonomyTermRef, TaxonomyTermRef | None]:
    parent_id = int(row["parent_id"]) if row.get("parent_id") is not None else None
    leaf = TaxonomyTermRef(
        term_id=int(row["term_id"]),
        slug=str(row.get("slug") or ""),
        parent_term_id=parent_id,
        source_category_id=(
            int(row["source_category_id"])
            if row.get("source_category_id") is not None
            else None
        ),
    )
    if parent_id is None:
        return leaf, None
    parent = TaxonomyTermRef(
        term_id=parent_id,
        slug=str(row.get("parent_slug") or ""),
        parent_term_id=None,
        source_category_id=(
            int(row["parent_source_category_id"])
            if row.get("parent_source_category_id") is not None
            else None
        ),
    )
    return leaf, parent


async def materialize_effective_primary_assignments(
    d1: Any,
    limit: int = 100,
) -> dict[str, int]:
    """Project accepted taxonomy primaries into the legacy catalog atomically.

    Manual ``tool_categories`` rows are retained. All non-manual legacy links
    are replaced by the effective taxonomy parent/leaf pair, and every success
    is recorded in the pre-existing materialization audit table.
    """

    rows = await d1.query(
        PENDING_LEGACY_PROJECTIONS_SQL,
        [LEGACY_CATEGORY_MATERIALIZATION_VERSION, max(1, int(limit))],
    )
    counts = {
        "legacy_projection_selected": len(rows),
        "legacy_projection_succeeded": 0,
        "legacy_projection_failed": 0,
    }
    for row in rows:
        try:
            leaf, parent = _term_ref_from_projection_row(row)
            plan = materialize_legacy_category(leaf, parent)
            source = (
                "manual"
                if str(row.get("assignment_source") or "") == "manual"
                else "auto"
            )
            provenance = json.dumps(
                {
                    "mode": "taxonomy_compatibility_projection",
                    "materialization_version": plan.materialization_version,
                    "assignment_id": int(row["assignment_id"]),
                    "decision_status": str(row.get("decision_status") or ""),
                    "leaf_term_id": plan.leaf_term_id,
                    "category_ids": plan.category_ids,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            now_sql = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            statements: list[tuple[str, list[Any]]] = [
                (
                    """
                    DELETE FROM tool_categories
                    WHERE tool_id = ?
                      AND (
                        COALESCE(source, 'auto') <> 'manual'
                        OR CASE
                             WHEN json_valid(COALESCE(raw_output, ''))
                             THEN json_extract(raw_output, '$.mode')
                           END = 'taxonomy_compatibility_projection'
                      )
                    """,
                    [int(row["tool_id"])],
                )
            ]
            for category_id in plan.category_ids:
                statements.append(
                    (
                        f"""
                        INSERT INTO tool_categories (
                          tool_id, category_id, source, raw_output, classified_at
                        )
                        VALUES (?, ?, ?, ?, {now_sql})
                        ON CONFLICT(tool_id, category_id) DO UPDATE SET
                          source = CASE
                            WHEN tool_categories.source = 'manual' THEN tool_categories.source
                            ELSE excluded.source
                          END,
                          raw_output = CASE
                            WHEN tool_categories.source = 'manual' THEN tool_categories.raw_output
                            ELSE excluded.raw_output
                          END,
                          classified_at = CASE
                            WHEN tool_categories.source = 'manual' THEN tool_categories.classified_at
                            ELSE excluded.classified_at
                          END
                        """,
                        [int(row["tool_id"]), category_id, source, provenance],
                    )
                )
            statements.extend(
                [
                    (
                        f"""
                        UPDATE tools
                        SET primary_category_id = ?,
                            category_classification_status = 'auto_ok',
                            category_classification_attempts = 0,
                            category_classification_raw = ?,
                            category_classification_last_error = NULL,
                            category_classification_updated_at = {now_sql},
                            updated_at = {now_sql}
                        WHERE id = ?
                        """,
                        [plan.primary_category_id, provenance, int(row["tool_id"])],
                    ),
                    (
                        f"""
                        INSERT INTO legacy_category_materializations (
                          tool_id, leaf_term_id, materialization_version,
                          primary_category_id, category_ids_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, {now_sql})
                        """,
                        [
                            int(row["tool_id"]),
                            plan.leaf_term_id,
                            plan.materialization_version,
                            plan.primary_category_id,
                            json.dumps(plan.category_ids, separators=(",", ":")),
                        ],
                    ),
                ]
            )
            await d1.batch(statements)
            counts["legacy_projection_succeeded"] += 1
        except Exception:
            counts["legacy_projection_failed"] += 1
    return counts
