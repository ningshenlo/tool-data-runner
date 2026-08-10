"""Pure legacy materializer (Python mirror of ainav/lib/taxonomy/materialize-legacy-category.ts).

P4A+ dual-write only. Do not call from Shadow Mode pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_CATEGORY_MATERIALIZATION_VERSION = "legacy-materializer-v1-2026-08-06"


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
