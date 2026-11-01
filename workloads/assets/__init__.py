"""assets-worker: site assets, enrichment reconcile, guarded catalog auto-publish.

Keep this package init free of heavy imports so `from workloads.assets.constants`
does not pull in runner-dependent modules during runner startup.
"""

from workloads.assets.loops import assets_next_delay_seconds, run_assets_loop

__all__ = [
    "D1AssetStore",
    "D1CatalogPublisher",
    "D1EnrichmentStore",
    "assets_next_delay_seconds",
    "process_asset_task",
    "run_assets_loop",
    "run_assets_once",
]


def __getattr__(name: str):
    if name in {"run_assets_once", "process_asset_task"}:
        from workloads.assets import pipeline

        return getattr(pipeline, name)
    if name == "D1AssetStore":
        from workloads.assets.store import D1AssetStore

        return D1AssetStore
    if name == "D1EnrichmentStore":
        from workloads.assets.enrichment import D1EnrichmentStore

        return D1EnrichmentStore
    if name == "D1CatalogPublisher":
        from workloads.assets.publish import D1CatalogPublisher

        return D1CatalogPublisher
    raise AttributeError(name)
