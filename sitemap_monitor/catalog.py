from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .normalize import SitemapUrlError, normalize_sitemap_url
from .storage import site_id_for


class CatalogD1Client(Protocol):
    async def query(
        self,
        sql: str,
        params: list[object] | None = None,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class CatalogSiteSnapshot:
    duplicate_origins: int
    homepage_urls: tuple[str, ...]
    invalid_urls: int
    source_rows: int


PUBLISHED_CATALOG_SITE_PAGE_SQL = """
WITH canonical_tools AS (
    SELECT
        id,
        official_url,
        normalized_domain,
        row_number() OVER (
            PARTITION BY lower(trim(normalized_domain))
            ORDER BY id
        ) AS domain_row
    FROM tools
    WHERE status = 'published'
      AND content_safety_status = 'safe'
      AND duplicate_of_tool_id IS NULL
      AND trim(official_url) <> ''
      AND trim(normalized_domain) <> ''
)
SELECT id, official_url, normalized_domain
FROM canonical_tools
WHERE domain_row = 1 AND id > ?
ORDER BY id
LIMIT ?
"""


def _homepage_origin(value: str) -> str:
    normalized = normalize_sitemap_url(value)
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


async def load_published_catalog_sites(
    client: CatalogD1Client,
    *,
    page_size: int = 500,
) -> CatalogSiteSnapshot:
    if page_size <= 0 or page_size > 1_000:
        raise ValueError("Catalog page_size must be between 1 and 1000.")

    cursor_id = 0
    duplicate_origins = 0
    invalid_urls = 0
    source_rows = 0
    by_site_id: dict[str, str] = {}
    while True:
        result = await client.query(
            PUBLISHED_CATALOG_SITE_PAGE_SQL,
            [cursor_id, page_size],
        )
        rows = result.get("results") or []
        if not isinstance(rows, list):
            raise RuntimeError("Cloudflare D1 returned invalid catalog rows.")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Cloudflare D1 returned an invalid catalog row.")
            row_id = int(row.get("id") or 0)
            if row_id <= cursor_id:
                raise RuntimeError("Published catalog cursor did not advance.")
            cursor_id = row_id
            source_rows += 1
            try:
                homepage_url = _homepage_origin(str(row.get("official_url") or ""))
            except SitemapUrlError:
                try:
                    homepage_url = _homepage_origin(
                        f"https://{str(row.get('normalized_domain') or '').strip()}/"
                    )
                except SitemapUrlError:
                    invalid_urls += 1
                    continue
            site_id = site_id_for(homepage_url)
            if site_id in by_site_id:
                duplicate_origins += 1
                continue
            by_site_id[site_id] = homepage_url
        if len(rows) < page_size:
            break

    return CatalogSiteSnapshot(
        duplicate_origins=duplicate_origins,
        homepage_urls=tuple(by_site_id.values()),
        invalid_urls=invalid_urls,
        source_rows=source_rows,
    )
