"""Market snapshot build / activate / preview (used by traffic batch + CLI)."""

from __future__ import annotations

__all__ = [
    "activate_market_snapshot_from_d1",
    "build_market_snapshot_from_d1",
    "preview_market_snapshot_from_d1",
]


def __getattr__(name: str):
    if name in __all__:
        from workloads.market import snapshot

        return getattr(snapshot, name)
    raise AttributeError(name)
