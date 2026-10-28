from __future__ import annotations

from collections.abc import Sequence

from .models import DiffResult, SitemapEntry


def _metadata_identity(entry: SitemapEntry) -> tuple[str, str | None]:
    return (entry.normalized_url, entry.lastmod)


def diff_states(
    previous: Sequence[SitemapEntry],
    current: Sequence[SitemapEntry],
) -> DiffResult:
    """Merge-diff two URL-hash-sorted sitemap states in O(N)."""

    old = sorted(previous, key=lambda item: (item.url_hash, item.normalized_url))
    new = sorted(current, key=lambda item: (item.url_hash, item.normalized_url))
    added: list[SitemapEntry] = []
    removed: list[SitemapEntry] = []
    modified: list[SitemapEntry] = []
    old_index = 0
    new_index = 0

    while old_index < len(old) or new_index < len(new):
        if old_index >= len(old):
            added.extend(new[new_index:])
            break
        if new_index >= len(new):
            removed.extend(old[old_index:])
            break

        old_entry = old[old_index]
        new_entry = new[new_index]
        old_key = (old_entry.url_hash, old_entry.normalized_url)
        new_key = (new_entry.url_hash, new_entry.normalized_url)
        if old_key == new_key:
            if _metadata_identity(old_entry) != _metadata_identity(new_entry):
                modified.append(new_entry)
            old_index += 1
            new_index += 1
        elif old_key < new_key:
            removed.append(old_entry)
            old_index += 1
        else:
            added.append(new_entry)
            new_index += 1

    return DiffResult(tuple(added), tuple(modified), tuple(removed))
