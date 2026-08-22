from __future__ import annotations

import hashlib

from .models import FingerprintedDocument, SitemapDocument


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fingerprint_document(document: SitemapDocument) -> FingerprintedDocument:
    entries = tuple(sorted(document.entries, key=lambda item: (item.url_hash, item.normalized_url)))
    urlset_payload = b"".join(bytes.fromhex(entry.url_hash) for entry in entries)
    metadata_payload = b"".join(
        bytes.fromhex(entry.url_hash)
        + b"\0"
        + (entry.lastmod or "").encode("utf-8")
        + b"\n"
        for entry in entries
    )
    return FingerprintedDocument(
        content_hash=_sha256_hex(document.raw_content),
        entries=entries,
        kind=document.kind,
        metadata_hash=_sha256_hex(metadata_payload),
        source_url=document.source_url,
        urlset_hash=_sha256_hex(urlset_payload),
    )
