from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Sequence

from .models import DiffResult, SitemapEntry
from .normalize import sitemap_url_hash


DEFAULT_MAX_STATE_BYTES = 256 * 1024 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_state(entries: Sequence[SitemapEntry]) -> bytes:
    lines = [
        _json_bytes(
            {
                "h": entry.url_hash,
                "lm": entry.lastmod,
                "r": entry.raw_url,
                "u": entry.normalized_url,
            }
        )
        for entry in sorted(entries, key=lambda item: (item.url_hash, item.normalized_url))
    ]
    return gzip.compress(b"\n".join(lines) + (b"\n" if lines else b""), mtime=0)


def _decompress_state(payload: bytes, max_decompressed_bytes: int) -> bytes:
    if max_decompressed_bytes <= 0:
        raise ValueError("State decompression limit must be positive.")
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decompressor.decompress(payload, max_decompressed_bytes + 1)
        if len(raw) <= max_decompressed_bytes:
            raw += decompressor.flush(max_decompressed_bytes - len(raw) + 1)
    except zlib.error as error:
        raise ValueError("Stored sitemap state is malformed.") from error
    if len(raw) > max_decompressed_bytes or not decompressor.eof:
        raise ValueError("Stored sitemap state exceeds its decompressed byte limit.")
    return raw


def decode_state(
    payload: bytes,
    *,
    max_decompressed_bytes: int = DEFAULT_MAX_STATE_BYTES,
    max_entries: int = 50_000,
) -> tuple[SitemapEntry, ...]:
    try:
        raw = _decompress_state(payload, max_decompressed_bytes)
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Stored sitemap state is malformed.") from error
    if len(rows) > max_entries:
        raise ValueError("Stored sitemap state exceeds its entry limit.")
    entries: list[SitemapEntry] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("u"), str):
            raise ValueError("Stored sitemap state row is malformed.")
        normalized_url = row["u"]
        expected_hash = sitemap_url_hash(normalized_url)
        if row.get("h") != expected_hash:
            raise ValueError("Stored sitemap state URL hash does not match its URL.")
        entries.append(
            SitemapEntry(
                normalized_url=normalized_url,
                raw_url=row.get("r") if isinstance(row.get("r"), str) else normalized_url,
                url_hash=expected_hash,
                lastmod=row.get("lm") if isinstance(row.get("lm"), str) else None,
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.url_hash, item.normalized_url)))


def _entry_json(entry: SitemapEntry) -> dict[str, str | None]:
    return {
        "hash": entry.url_hash,
        "lastmod": entry.lastmod,
        "raw_url": entry.raw_url,
        "url": entry.normalized_url,
    }


def encode_diff(diff: DiffResult) -> bytes:
    payload = {
        "added": [_entry_json(entry) for entry in diff.added],
        "modified": [_entry_json(entry) for entry in diff.modified],
        "removed": [_entry_json(entry) for entry in diff.removed],
        "version": "sitemap-diff-v1",
    }
    return gzip.compress(_json_bytes(payload), mtime=0)
