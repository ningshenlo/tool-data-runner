import unittest

from taxonomy_materialize import (
    LEGACY_CATEGORY_MATERIALIZATION_VERSION,
    LegacyMaterializationError,
    TaxonomyTermRef,
    materialize_legacy_category,
)


class MaterializeLegacyCategoryTests(unittest.TestCase):
    def test_leaf_with_parent(self) -> None:
        plan = materialize_legacy_category(
            TaxonomyTermRef(200, "video-generation-conversion", 100, 74),
            TaxonomyTermRef(100, "video-animation", None, 27),
        )
        self.assertEqual(plan.materialization_version, LEGACY_CATEGORY_MATERIALIZATION_VERSION)
        self.assertEqual(plan.primary_category_id, 27)
        self.assertEqual(plan.category_ids, [27, 74])

    def test_root_leaf(self) -> None:
        plan = materialize_legacy_category(TaxonomyTermRef(50, "orphan", None, 99))
        self.assertEqual(plan.primary_category_id, 99)
        self.assertEqual(plan.category_ids, [99])

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(LegacyMaterializationError):
            materialize_legacy_category(TaxonomyTermRef(1, "x", None, None))


if __name__ == "__main__":
    unittest.main()
