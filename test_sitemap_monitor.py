from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from sitemap_monitor.catalog import load_published_catalog_sites
from sitemap_monitor.cloudflare import (
    CloudflareD1Client,
    CloudflareD1MetadataStore,
    CloudflareR2ObjectStore,
)
from sitemap_monitor.cli import parse_args
from sitemap_monitor.codec import decode_state, encode_state
from sitemap_monitor.comparability import (
    assess_comparability,
    build_site_scan_snapshot,
    normalize_resource_family,
)
from sitemap_monitor.config import MonitorLimits
from sitemap_monitor.diff import diff_states
from sitemap_monitor.discovery import discovery_candidates, parse_robots_sitemaps
from sitemap_monitor.engine import SitemapMonitor
from sitemap_monitor.fetch import (
    SitemapFetchError,
    SitemapHttpFetcher,
    assert_public_http_target,
    decode_sitemap_payload,
)
from sitemap_monitor.fingerprint import fingerprint_document
from sitemap_monitor.models import (
    CheckOutcome,
    FetchResult,
    FetchValidators,
    ResourceState,
    SiteScanResult,
    StoredSiteScan,
)
from sitemap_monitor.normalize import SitemapUrlError, normalize_sitemap_url
from sitemap_monitor.parser import SitemapParseError, parse_sitemap
from sitemap_monitor.scheduler import SchedulerPolicy, SitemapScheduler
from sitemap_monitor.storage import FileObjectStore, SqliteMetadataStore, site_id_for


URLSET_A = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/Product/</loc><lastmod>2026-08-01</lastmod></url>
  <url><loc>https://example.com/search?q=AI</loc></url>
</urlset>"""

URLSET_A_REORDERED = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/search?q=AI</loc></url>
<url><lastmod>2026-08-01</lastmod><loc>https://example.com/Product/</loc></url>
</urlset>"""

URLSET_B = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/Product/</loc><lastmod>2026-08-02</lastmod></url>
<url><loc>https://example.com/integrations/slack</loc></url>
</urlset>"""

INDEX = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/pages.xml</loc></sitemap>
</sitemapindex>"""

INDEX_WITH_CROSS_HOST = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/pages.xml</loc></sitemap>
<sitemap><loc>https://other.example/foreign.xml</loc></sitemap>
</sitemapindex>"""


INDEX_WITH_APEX_CHILD = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/pages.xml</loc></sitemap>
</sitemapindex>"""


def response(
    url: str,
    body: bytes | None,
    status: int = 200,
    *,
    etag: str | None = None,
    final_url: str | None = None,
) -> FetchResult:
    return FetchResult(
        body=body,
        bytes_downloaded=len(body or b""),
        content_type="application/xml",
        etag=etag,
        final_url=final_url or url,
        last_modified=None,
        retry_after=None,
        status_code=status,
    )


class FakeFetcher:
    def __init__(self, responses: dict[str, list[FetchResult | Exception]]):
        self.responses = {url: deque(values) for url, values in responses.items()}
        self.validators: dict[str, list[FetchValidators]] = defaultdict(list)

    async def fetch(
        self,
        url: str,
        validators: FetchValidators | None = None,
        *,
        max_download_bytes: int | None = None,
    ) -> FetchResult:
        del max_download_bytes
        self.validators[url].append(validators or FetchValidators())
        values = self.responses.get(url)
        if not values:
            return response(url, None, 404)
        value = values.popleft()
        if isinstance(value, Exception):
            raise value
        return value


class SqliteD1ClientDouble:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        schema = Path("sitemap_monitor/schema.sql").read_text(encoding="utf-8")
        self.connection.executescript(schema)

    async def query(self, sql: str, params: list[object] | None = None) -> dict[str, object]:
        with self.connection:
            cursor = self.connection.execute(sql, params or [])
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        return {"results": rows, "success": True}

    async def batch(self, statements: list[tuple[str, list[object]]]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        with self.connection:
            for sql, params in statements:
                cursor = self.connection.execute(sql, params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append({"results": rows, "success": True})
        return results


class NormalizeAndParserTests(unittest.TestCase):
    def test_normalization_is_conservative_and_rejects_private_targets(self) -> None:
        self.assertEqual(
            normalize_sitemap_url("HTTPS://Example.COM:443/Product/%7eA/?q=AI#fragment"),
            "https://example.com/Product/~A/?q=AI",
        )
        self.assertNotEqual(
            normalize_sitemap_url("https://example.com/product"),
            normalize_sitemap_url("https://example.com/product/"),
        )
        self.assertNotEqual(
            normalize_sitemap_url("https://example.com/search?q=a"),
            normalize_sitemap_url("https://example.com/search?q=b"),
        )
        with self.assertRaises(SitemapUrlError):
            normalize_sitemap_url("http://127.0.0.1/sitemap.xml")
        with self.assertRaises(SitemapUrlError):
            normalize_sitemap_url("file:///etc/passwd")

    def test_parses_urlset_index_and_text_sitemaps(self) -> None:
        urlset = parse_sitemap(URLSET_A, "https://example.com/sitemap.xml")
        index = parse_sitemap(INDEX, "https://example.com/sitemap-index.xml")
        text = parse_sitemap(
            b"https://example.com/a\nhttps://example.com/b\n",
            "https://example.com/sitemap.txt",
            content_type="text/plain",
        )
        self.assertEqual(urlset.kind, "urlset")
        self.assertEqual(len(urlset.entries), 2)
        self.assertEqual(index.kind, "sitemap_index")
        self.assertEqual(index.entries[0].normalized_url, "https://example.com/pages.xml")
        self.assertEqual(text.kind, "text")

    def test_rejects_unsafe_xml_malformed_xml_and_url_overflow(self) -> None:
        unsafe = b'<!DOCTYPE x [<!ENTITY e "boom">]><urlset><url><loc>&e;</loc></url></urlset>'
        with self.assertRaisesRegex(SitemapParseError, "DOCTYPE"):
            parse_sitemap(unsafe, "https://example.com/sitemap.xml")
        with self.assertRaises(SitemapParseError) as malformed:
            parse_sitemap(b"<urlset>", "https://example.com/sitemap.xml")
        self.assertEqual(malformed.exception.code, "malformed_xml")
        with self.assertRaises(SitemapParseError) as overflow:
            parse_sitemap(
                URLSET_A,
                "https://example.com/sitemap.xml",
                limits=MonitorLimits(max_url_count=1),
            )
        self.assertEqual(overflow.exception.code, "url_limit_exceeded")

    def test_html_fallback_is_not_reported_as_unsafe_xml(self) -> None:
        with self.assertRaises(SitemapParseError) as fallback:
            parse_sitemap(
                b"<!DOCTYPE html><html><body>app shell</body></html>",
                "https://example.com/sitemap.xml",
                content_type="text/html",
            )
        self.assertEqual(fallback.exception.code, "unsupported_xml_root")


class FingerprintAndDiffTests(unittest.TestCase):
    def test_three_layer_hash_filters_order_and_serialization_noise(self) -> None:
        first = fingerprint_document(parse_sitemap(URLSET_A, "https://example.com/sitemap.xml"))
        second = fingerprint_document(
            parse_sitemap(URLSET_A_REORDERED, "https://example.com/sitemap.xml")
        )
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.urlset_hash, second.urlset_hash)
        self.assertEqual(first.metadata_hash, second.metadata_hash)

    def test_merge_diff_reports_added_removed_and_modified(self) -> None:
        previous = fingerprint_document(parse_sitemap(URLSET_A, "https://example.com/sitemap.xml"))
        current = fingerprint_document(parse_sitemap(URLSET_B, "https://example.com/sitemap.xml"))
        result = diff_states(previous.entries, current.entries)
        self.assertEqual([item.normalized_url for item in result.added], [
            "https://example.com/integrations/slack"
        ])
        self.assertEqual([item.normalized_url for item in result.removed], [
            "https://example.com/search?q=AI"
        ])
        self.assertEqual([item.normalized_url for item in result.modified], [
            "https://example.com/Product/"
        ])

    def test_state_codec_is_deterministic_and_tamper_evident(self) -> None:
        entries = parse_sitemap(URLSET_A, "https://example.com/sitemap.xml").entries
        first = encode_state(entries)
        second = encode_state(tuple(reversed(entries)))
        self.assertEqual(first, second)
        self.assertEqual(decode_state(first), entries)
        with self.assertRaisesRegex(ValueError, "decompressed byte limit"):
            decode_state(first, max_decompressed_bytes=10)


class ComparabilityGateTests(unittest.TestCase):
    @staticmethod
    def _make_outcome(
        resource_id: str,
        url: str,
        *,
        result: str = "not_modified",
        url_count: int | None = 10,
        parent_id: str | None = None,
        sitemap_kind: str | None = "urlset",
    ) -> CheckOutcome:
        failed = result == "failed"
        return CheckOutcome(
            added_count=0,
            bytes_downloaded=0,
            diff_key=None,
            error_code="http_502" if failed else None,
            final_url=url,
            http_status=502 if failed else 304,
            modified_count=0,
            parent_id=parent_id,
            removed_count=0,
            resource_id=resource_id,
            result=result,  # type: ignore[arg-type]
            sitemap_kind=None if failed else sitemap_kind,  # type: ignore[arg-type]
            state_key=None if failed else f"state/{resource_id}.jsonl.gz",
            url=url,
            url_count=None if failed else url_count,
        )

    @classmethod
    def _snapshot(
        cls,
        scan_id: str,
        outcomes: list[CheckOutcome],
        *,
        discovery_mode: str = "explicit",
        traversal_complete: bool = True,
    ):
        return build_site_scan_snapshot(
            SiteScanResult(
                discovery_mode=discovery_mode,  # type: ignore[arg-type]
                finished_at_ms=2_000,
                homepage_url="https://example.com/",
                outcomes=tuple(outcomes),
                scan_id=scan_id,
                site_id="site_test",
                started_at_ms=1_000,
                traversal_complete=traversal_complete,
                traversal_reason_codes=(
                    () if traversal_complete else ("resource_limit_reached",)
                ),
            )
        )

    @staticmethod
    def _stored(snapshot, assessment) -> StoredSiteScan:
        return StoredSiteScan(
            is_comparable=assessment.is_comparable,
            promoted_semantic_baseline=assessment.promote_semantic_baseline,
            snapshot=snapshot,
            status=assessment.status,
        )

    def test_first_complete_scan_builds_baseline_then_becomes_comparable(self) -> None:
        first = self._snapshot(
            "scan_1",
            [self._make_outcome("resource_1", "https://example.com/sitemap.xml")],
        )
        first_assessment = assess_comparability(first, baseline=None, previous=None)
        self.assertEqual(first_assessment.status, "baseline_invalid")
        self.assertTrue(first_assessment.promote_semantic_baseline)
        self.assertFalse(first_assessment.is_comparable)

        baseline = self._stored(first, first_assessment)
        second = self._snapshot(
            "scan_2",
            [self._make_outcome("resource_1", "https://example.com/sitemap.xml")],
        )
        second_assessment = assess_comparability(
            second,
            baseline=baseline,
            previous=baseline,
        )
        self.assertEqual(second_assessment.status, "comparable")
        self.assertTrue(second_assessment.is_comparable)
        self.assertTrue(second_assessment.promote_semantic_baseline)

    def test_failed_child_sitemap_is_partial_and_cannot_replace_baseline(self) -> None:
        baseline_snapshot = self._snapshot(
            "scan_1",
            [
                self._make_outcome(
                    "index",
                    "https://example.com/sitemap.xml",
                    sitemap_kind="sitemap_index",
                    url_count=1,
                ),
                self._make_outcome(
                    "child",
                    "https://example.com/pages.xml",
                    parent_id="index",
                ),
            ],
        )
        baseline_assessment = assess_comparability(
            baseline_snapshot,
            baseline=None,
            previous=None,
        )
        baseline = self._stored(baseline_snapshot, baseline_assessment)
        partial = self._snapshot(
            "scan_2",
            [
                self._make_outcome(
                    "index",
                    "https://example.com/sitemap.xml",
                    sitemap_kind="sitemap_index",
                    url_count=1,
                ),
                self._make_outcome(
                    "child",
                    "https://example.com/pages.xml",
                    result="failed",
                    parent_id="index",
                ),
            ],
        )

        assessment = assess_comparability(
            partial,
            baseline=baseline,
            previous=baseline,
        )
        self.assertEqual(assessment.status, "partial")
        self.assertFalse(assessment.is_comparable)
        self.assertFalse(assessment.promote_semantic_baseline)
        self.assertEqual(assessment.successful_resource_ratio, 0.5)

    def test_dynamic_partitions_share_a_normalized_resource_family(self) -> None:
        self.assertEqual(
            normalize_resource_family("https://example.com/post-sitemap1.xml"),
            normalize_resource_family("https://example.com/post-sitemap3.xml"),
        )
        baseline_snapshot = self._snapshot(
            "scan_1",
            [
                self._make_outcome("post_1", "https://example.com/post-sitemap1.xml"),
                self._make_outcome("post_2", "https://example.com/post-sitemap2.xml"),
            ],
        )
        baseline_assessment = assess_comparability(
            baseline_snapshot,
            baseline=None,
            previous=None,
        )
        baseline = self._stored(baseline_snapshot, baseline_assessment)
        expanded = self._snapshot(
            "scan_2",
            [
                self._make_outcome("post_1", "https://example.com/post-sitemap1.xml"),
                self._make_outcome("post_2", "https://example.com/post-sitemap2.xml"),
                self._make_outcome("post_3", "https://example.com/post-sitemap3.xml"),
            ],
        )
        assessment = assess_comparability(
            expanded,
            baseline=baseline,
            previous=baseline,
        )
        self.assertEqual(assessment.status, "comparable")
        self.assertIn("dynamic_resource_partition_change", assessment.reason_codes)

    def test_changed_resource_family_requires_a_stable_confirmation_scan(self) -> None:
        baseline_snapshot = self._snapshot(
            "scan_1",
            [self._make_outcome("product", "https://example.com/product-sitemap.xml")],
        )
        baseline_assessment = assess_comparability(
            baseline_snapshot,
            baseline=None,
            previous=None,
        )
        baseline = self._stored(baseline_snapshot, baseline_assessment)
        changed = self._snapshot(
            "scan_2",
            [
                self._make_outcome("product", "https://example.com/product-sitemap.xml"),
                self._make_outcome("blog", "https://example.com/blog-sitemap.xml"),
            ],
        )
        first_change = assess_comparability(
            changed,
            baseline=baseline,
            previous=baseline,
        )
        self.assertEqual(first_change.status, "resource_set_changed")
        self.assertFalse(first_change.promote_semantic_baseline)

        previous = self._stored(changed, first_change)
        confirmed = self._snapshot(
            "scan_3",
            [
                self._make_outcome("product", "https://example.com/product-sitemap.xml"),
                self._make_outcome("blog", "https://example.com/blog-sitemap.xml"),
            ],
        )
        confirmation = assess_comparability(
            confirmed,
            baseline=baseline,
            previous=previous,
        )
        self.assertEqual(confirmation.status, "resource_set_changed")
        self.assertFalse(confirmation.is_comparable)
        self.assertTrue(confirmation.promote_semantic_baseline)
        self.assertIn("resource_set_stable_confirmation", confirmation.reason_codes)

    def test_complete_family_replacement_with_stable_volume_is_possible_migration(self) -> None:
        baseline_snapshot = self._snapshot(
            "scan_1",
            [self._make_outcome("old", "https://example.com/product-sitemap.xml")],
        )
        baseline_assessment = assess_comparability(
            baseline_snapshot,
            baseline=None,
            previous=None,
        )
        baseline = self._stored(baseline_snapshot, baseline_assessment)
        replacement = self._snapshot(
            "scan_2",
            [self._make_outcome("new", "https://example.com/docs-sitemap.xml")],
        )
        assessment = assess_comparability(
            replacement,
            baseline=baseline,
            previous=baseline,
        )
        self.assertEqual(assessment.status, "possible_migration")
        self.assertFalse(assessment.is_comparable)

    def test_traversal_limit_is_fetch_incomplete_even_with_a_success(self) -> None:
        snapshot = self._snapshot(
            "scan_1",
            [self._make_outcome("resource_1", "https://example.com/sitemap.xml")],
            traversal_complete=False,
        )
        assessment = assess_comparability(snapshot, baseline=None, previous=None)
        self.assertEqual(assessment.status, "fetch_incomplete")
        self.assertFalse(assessment.promote_semantic_baseline)

    def test_fallback_discovery_probes_do_not_make_a_good_scan_partial(self) -> None:
        snapshot = self._snapshot(
            "scan_1",
            [
                self._make_outcome(
                    "probe",
                    "https://example.com/sitemap_index.xml",
                    result="failed",
                ),
                self._make_outcome("actual", "https://example.com/sitemap.xml"),
            ],
            discovery_mode="fallback",
        )
        assessment = assess_comparability(snapshot, baseline=None, previous=None)
        self.assertEqual(snapshot.attempted_resource_count, 1)
        self.assertEqual(snapshot.successful_resource_ratio, 1.0)
        self.assertEqual(assessment.status, "baseline_invalid")


class DiscoveryAndFetchTests(unittest.TestCase):
    def test_robots_discovery_deduplicates_and_common_paths_are_fallbacks(self) -> None:
        robots = parse_robots_sitemaps(
            b"User-agent: *\nSitemap: /sitemap.xml\nsitemap: https://example.com/news.xml\n",
            "https://example.com/robots.txt",
        )
        self.assertEqual(robots, (
            "https://example.com/sitemap.xml",
            "https://example.com/news.xml",
        ))
        candidates = discovery_candidates(
            "https://example.com/docs",
            explicit_sitemaps=["/custom.xml"],
            robots_sitemaps=robots,
        )
        self.assertEqual(candidates[0], "https://example.com/custom.xml")
        self.assertEqual(candidates.count("https://example.com/sitemap.xml"), 1)

    def test_gzip_decode_enforces_decompressed_limit(self) -> None:
        zipped = gzip.compress(URLSET_A)
        self.assertEqual(
            decode_sitemap_payload(
                zipped,
                content_encoding=None,
                final_url="https://example.com/sitemap.xml.gz",
                max_decompressed_bytes=len(URLSET_A),
            ),
            URLSET_A,
        )
        with self.assertRaises(SitemapFetchError) as overflow:
            decode_sitemap_payload(
                zipped,
                content_encoding=None,
                final_url="https://example.com/sitemap.xml.gz",
                max_decompressed_bytes=10,
            )
        self.assertEqual(overflow.exception.code, "decompressed_limit_exceeded")


class HttpFetcherTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "sitemap_monitor.fetch.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("198.18.0.10", 443))],
    )
    @patch("sitemap_monitor.fetch._resolve_public_via_doh", new_callable=AsyncMock)
    async def test_synthetic_dns_requires_successful_public_doh_verification(
        self,
        resolve_public_via_doh: AsyncMock,
        _getaddrinfo: object,
    ) -> None:
        resolve_public_via_doh.return_value = True
        await assert_public_http_target(
            "https://example.com/sitemap.xml",
            allow_synthetic_dns_doh=True,
        )
        resolve_public_via_doh.assert_awaited_once_with("example.com")

    @patch(
        "sitemap_monitor.fetch.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("198.18.0.10", 443))],
    )
    async def test_synthetic_dns_stays_blocked_without_doh_fallback(
        self,
        _getaddrinfo: object,
    ) -> None:
        with self.assertRaisesRegex(SitemapFetchError, "non-public network"):
            await assert_public_http_target("https://example.com/sitemap.xml")

    async def test_conditional_get_follows_validated_redirect_without_head(self) -> None:
        methods: list[str] = []
        validator_headers: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            validator_headers.append(request.headers.get("if-none-match"))
            if request.url.path == "/sitemap.xml":
                return httpx.Response(302, headers={"location": "/canonical.xml"})
            return httpx.Response(
                200,
                stream=httpx.ByteStream(URLSET_A),
                headers={"content-type": "application/xml", "etag": '"v2"'},
            )

        fetcher = SitemapHttpFetcher(validate_public_targets=True)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch(
            "sitemap_monitor.fetch.assert_public_http_target",
            new=AsyncMock(return_value=None),
        ) as target_guard:
            result = await fetcher.fetch(
                "https://example.com/sitemap.xml",
                FetchValidators(etag='"v1"'),
            )
        await fetcher.close()

        self.assertEqual(methods, ["GET", "GET"])
        self.assertEqual(validator_headers, ['"v1"', '"v1"'])
        self.assertEqual(result.final_url, "https://example.com/canonical.xml")
        self.assertEqual(target_guard.await_count, 2)

    async def test_redirect_to_private_ip_fails_before_second_request(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private.xml"})

        fetcher = SitemapHttpFetcher(validate_public_targets=True)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch(
            "sitemap_monitor.fetch.assert_public_http_target",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(SitemapFetchError) as blocked:
                await fetcher.fetch("https://example.com/sitemap.xml")
        await fetcher.close()
        self.assertEqual(blocked.exception.code, "invalid_redirect")
        self.assertEqual(requests, 1)


class MonitorEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.metadata = SqliteMetadataStore(root / "monitor.sqlite3")
        self.objects = FileObjectStore(root / "objects")
        self.homepage = "https://example.com/"
        self.site_id = site_id_for(self.homepage)
        await self.metadata.ensure_site(self.site_id, self.homepage, 1)

    async def asyncTearDown(self) -> None:
        self.metadata.close()
        self.temporary.cleanup()

    async def test_baseline_semantic_unchanged_and_real_diff(self) -> None:
        sitemap_url = "https://example.com/sitemap.xml"
        fetcher = FakeFetcher({
            sitemap_url: [
                response(sitemap_url, URLSET_A, etag='"v1"'),
                response(sitemap_url, URLSET_A_REORDERED, etag='"v2"'),
                response(sitemap_url, URLSET_B, etag='"v3"'),
            ]
        })
        monitor = SitemapMonitor(self.metadata, self.objects, fetcher=fetcher)

        baseline = (await monitor.check_resource(self.site_id, sitemap_url)).outcome
        unchanged = (await monitor.check_resource(self.site_id, sitemap_url)).outcome
        changed = (await monitor.check_resource(self.site_id, sitemap_url)).outcome

        self.assertEqual(baseline.result, "baseline")
        self.assertEqual(baseline.added_count, 0)
        self.assertEqual(unchanged.result, "semantic_unchanged")
        self.assertEqual(changed.result, "changed")
        self.assertEqual(
            (changed.added_count, changed.removed_count, changed.modified_count),
            (1, 1, 1),
        )
        self.assertIsNotNone(changed.diff_key)
        self.assertEqual(fetcher.validators[sitemap_url][1].etag, '"v1"')
        self.assertEqual(fetcher.validators[sitemap_url][2].etag, '"v2"')

    async def test_304_index_reuses_stored_children_for_recursive_checks(self) -> None:
        robots = "https://example.com/robots.txt"
        index = "https://example.com/sitemap.xml"
        child = "https://example.com/pages.xml"
        fetcher = FakeFetcher({
            robots: [response(robots, None, 404), response(robots, None, 404)],
            index: [response(index, INDEX, etag='"index-v1"'), response(index, None, 304)],
            child: [response(child, URLSET_A, etag='"child-v1"'), response(child, None, 304)],
        })
        monitor = SitemapMonitor(self.metadata, self.objects, fetcher=fetcher)

        first = await monitor.scan_site(self.homepage, explicit_sitemaps=[index])
        second = await monitor.scan_site(self.homepage, explicit_sitemaps=[index])

        self.assertEqual([item.result for item in first.outcomes], ["baseline", "baseline"])
        self.assertEqual([item.result for item in second.outcomes], ["not_modified", "not_modified"])
        self.assertEqual(fetcher.validators[child][1].etag, '"child-v1"')

    async def test_stale_robots_sitemap_falls_back_to_common_paths(self) -> None:
        robots = "https://example.com/robots.txt"
        stale = "https://example.com/sitemap.xml"
        alternate = "https://example.com/sitemap_index.xml"
        recovered = "https://example.com/sitemap-index.xml"
        fetcher = FakeFetcher({
            robots: [response(robots, b"Sitemap: https://example.com/sitemap.xml\n")],
            stale: [response(stale, None, 404)],
            recovered: [response(recovered, URLSET_A)],
        })
        monitor = SitemapMonitor(self.metadata, self.objects, fetcher=fetcher)

        result = await monitor.scan_site(self.homepage)

        self.assertEqual(result.discovery_mode, "fallback")
        self.assertEqual(
            [item.url for item in result.outcomes],
            [stale, alternate, recovered],
        )
        self.assertEqual(
            [item.result for item in result.outcomes],
            ["failed", "failed", "baseline"],
        )

    async def test_index_does_not_expand_cross_host_children(self) -> None:
        robots = "https://example.com/robots.txt"
        index = "https://example.com/sitemap.xml"
        child = "https://example.com/pages.xml"
        foreign = "https://other.example/foreign.xml"
        fetcher = FakeFetcher({
            robots: [response(robots, None, 404)],
            index: [response(index, INDEX_WITH_CROSS_HOST)],
            child: [response(child, URLSET_A)],
            foreign: [response(foreign, URLSET_A)],
        })
        monitor = SitemapMonitor(self.metadata, self.objects, fetcher=fetcher)

        result = await monitor.scan_site(self.homepage, explicit_sitemaps=[index])

        self.assertEqual([item.url for item in result.outcomes], [index, child])
        self.assertNotIn(foreign, fetcher.validators)

    async def test_index_expands_apex_child_from_www_site(self) -> None:
        homepage = "https://www.example.com/"
        robots = "https://www.example.com/robots.txt"
        index = "https://www.example.com/sitemap-index.xml"
        child = "https://example.com/pages.xml"
        fetcher = FakeFetcher({
            robots: [response(robots, None, 404)],
            index: [response(index, INDEX_WITH_APEX_CHILD)],
            child: [
                response(
                    child,
                    URLSET_A,
                    final_url="https://www.example.com/pages.xml",
                )
            ],
        })
        monitor = SitemapMonitor(self.metadata, self.objects, fetcher=fetcher)

        result = await monitor.scan_site(homepage, explicit_sitemaps=[index])

        self.assertEqual([item.url for item in result.outcomes], [index, child])
        self.assertIn(child, fetcher.validators)


class SchedulerStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SqliteMetadataStore(root / "scheduler.sqlite3")
        self.homepage = "https://example.com/"
        self.site_id = site_id_for(self.homepage)
        await self.store.ensure_site(
            self.site_id,
            self.homepage,
            1_000,
            check_interval_sec=3_600,
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    async def _new_job(self, *, max_attempts: int = 5):
        due = await self.store.claim_due_sites(
            lease_duration_ms=10_000,
            lease_owner="scheduler-a",
            limit=10,
            now_ms=1_000,
        )
        self.assertEqual(len(due), 1)
        job = await self.store.ensure_job(
            due[0],
            max_attempts=max_attempts,
            now_ms=1_000,
        )
        self.assertIsNotNone(job)
        return due[0], job

    async def test_unchanged_site_registration_does_not_write(self) -> None:
        changes_before = self.store.connection.total_changes
        updated_before = self.store.connection.execute(
            "SELECT updated_at FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()[0]

        await self.store.ensure_site(
            self.site_id,
            self.homepage,
            99_999,
            check_interval_sec=3_600,
        )

        updated_after = self.store.connection.execute(
            "SELECT updated_at FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()[0]
        self.assertEqual(self.store.connection.total_changes, changes_before)
        self.assertEqual(updated_after, updated_before)

    async def test_pausing_site_invalidates_schedule_and_stops_due_claims(self) -> None:
        before = self.store.connection.execute(
            "SELECT schedule_version FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()[0]
        await self.store.set_site_status(
            self.site_id,
            "paused",
            2_000,
            from_status="active",
        )
        row = self.store.connection.execute(
            "SELECT status, schedule_version FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["schedule_version"], before + 1)
        due = await self.store.claim_due_sites(
            lease_duration_ms=10_000,
            lease_owner="scheduler-a",
            limit=10,
            now_ms=2_000,
        )
        self.assertEqual(due, ())

        await self.store.set_site_status(
            self.site_id,
            "active",
            3_000,
            from_status="paused",
        )
        reactivated = self.store.connection.execute(
            "SELECT status, schedule_version, next_check_at FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(reactivated["status"], "active")
        self.assertEqual(reactivated["schedule_version"], before + 2)
        self.assertEqual(reactivated["next_check_at"], 3_000)

    async def test_authoritative_catalog_reconciliation_adds_reactivates_and_pauses(self) -> None:
        old_homepage = "https://old.example/"
        returning_homepage = "https://returning.example/"
        old_site_id = site_id_for(old_homepage)
        returning_site_id = site_id_for(returning_homepage)
        await self.store.ensure_site(old_site_id, old_homepage, 1_000)
        await self.store.ensure_site(returning_site_id, returning_homepage, 1_000)
        await self.store.set_site_status(
            returning_site_id,
            "paused",
            2_000,
            from_status="active",
        )
        objects = FileObjectStore(Path(self.temporary.name) / "objects")
        monitor = SitemapMonitor(self.store, objects)
        scheduler = SitemapScheduler(
            self.store,
            monitor,
            batch_size=5,
            clock_ms=lambda: 3_000,
            policy=SchedulerPolicy(check_interval_sec=3_600),
        )
        try:
            result = await scheduler.reconcile_sites(
                [returning_homepage, "https://new.example/"],
                authoritative=True,
            )
        finally:
            await monitor.close()

        rows = {
            row["domain"]: row["status"]
            for row in self.store.connection.execute(
                "SELECT domain, status FROM sitemap_sites"
            )
        }
        self.assertEqual(result.active_sites, 2)
        self.assertEqual(result.inserted_sites, 1)
        self.assertEqual(result.reactivated_sites, 1)
        self.assertEqual(result.paused_sites, 2)
        self.assertEqual(rows["old.example"], "paused")
        self.assertEqual(rows["returning.example"], "active")
        self.assertEqual(rows["new.example"], "active")

    async def test_due_claim_job_idempotency_and_fenced_completion(self) -> None:
        due, first_job = await self._new_job()
        duplicate = await self.store.ensure_job(due, max_attempts=5, now_ms=1_001)
        self.assertEqual(duplicate.job_id, first_job.job_id)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM sitemap_jobs"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        second_due_claim = await self.store.claim_due_sites(
            lease_duration_ms=10_000,
            lease_owner="scheduler-b",
            limit=10,
            now_ms=1_001,
        )
        self.assertEqual(second_due_claim, ())

        claimed = await self.store.claim_jobs(
            lease_duration_ms=1_000,
            lease_owner="worker-a",
            limit=10,
            now_ms=1_010,
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].attempts, 1)
        self.assertEqual(
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-b",
                limit=10,
                now_ms=1_011,
            ),
            (),
        )

        reclaimed = await self.store.claim_jobs(
            lease_duration_ms=1_000,
            lease_owner="worker-b",
            limit=10,
            now_ms=2_011,
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].job_id, claimed[0].job_id)
        self.assertEqual(reclaimed[0].attempts, 2)
        self.assertNotEqual(reclaimed[0].lease_token, claimed[0].lease_token)

        self.assertFalse(
            await self.store.finish_job_success(
                claimed[0],
                finished_at_ms=2_020,
                next_check_at_ms=3_600_000,
            )
        )
        self.assertTrue(
            await self.store.finish_job_success(
                reclaimed[0],
                finished_at_ms=2_021,
                next_check_at_ms=3_602_021,
            )
        )
        site = self.store.connection.execute(
            "SELECT next_check_at, last_success_at, error_streak, dispatch_lease_token "
            "FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(site["next_check_at"], 3_602_021)
        self.assertEqual(site["last_success_at"], 2_021)
        self.assertEqual(site["error_streak"], 0)
        self.assertIsNone(site["dispatch_lease_token"])

    async def test_retry_backoff_and_dead_letter_advance_site_schedule(self) -> None:
        _, _ = await self._new_job(max_attempts=2)
        first = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_010,
            )
        )[0]
        self.assertTrue(
            await self.store.finish_job_failure(
                first,
                available_at_ms=2_000,
                dead=False,
                error="http_503",
                finished_at_ms=1_100,
                next_check_at_ms=None,
            )
        )
        self.assertEqual(
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-b",
                limit=1,
                now_ms=1_999,
            ),
            (),
        )
        second = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-b",
                limit=1,
                now_ms=2_000,
            )
        )[0]
        self.assertEqual(second.attempts, 2)
        self.assertTrue(
            await self.store.finish_job_failure(
                second,
                available_at_ms=4_000,
                dead=True,
                error="http_503",
                finished_at_ms=2_100,
                next_check_at_ms=10_000,
            )
        )
        job = self.store.connection.execute(
            "SELECT status, dead_letter_at FROM sitemap_jobs WHERE id = ?",
            (second.job_id,),
        ).fetchone()
        site = self.store.connection.execute(
            "SELECT next_check_at, error_streak FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(job["status"], "dead")
        self.assertEqual(job["dead_letter_at"], 2_100)
        self.assertEqual(site["next_check_at"], 10_000)
        self.assertEqual(site["error_streak"], 1)

    async def test_schedule_version_fences_completion_after_interval_change(self) -> None:
        due, original = await self._new_job()
        claimed = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_010,
            )
        )[0]
        await self.store.ensure_site(
            self.site_id,
            self.homepage,
            1_500,
            check_interval_sec=7_200,
        )
        changed = self.store.connection.execute(
            "SELECT schedule_version, next_check_at, dispatch_lease_token "
            "FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(changed["schedule_version"], due.schedule_version + 1)
        self.assertEqual(changed["next_check_at"], 1_500)
        self.assertIsNone(changed["dispatch_lease_token"])

        self.assertTrue(
            await self.store.finish_job_success(
                claimed,
                finished_at_ms=1_600,
                next_check_at_ms=9_999_999,
            )
        )
        still_due = self.store.connection.execute(
            "SELECT next_check_at FROM sitemap_sites WHERE id = ?",
            (self.site_id,),
        ).fetchone()
        self.assertEqual(still_due["next_check_at"], 1_500)

        new_due = await self.store.claim_due_sites(
            lease_duration_ms=1_000,
            lease_owner="scheduler-b",
            limit=1,
            now_ms=1_500,
        )
        replacement = await self.store.ensure_job(
            new_due[0],
            max_attempts=5,
            now_ms=1_500,
        )
        self.assertNotEqual(replacement.job_id, original.job_id)

    async def test_maintenance_expires_superseded_jobs(self) -> None:
        _, original = await self._new_job()
        await self.store.ensure_site(
            self.site_id,
            self.homepage,
            1_500,
            check_interval_sec=7_200,
        )

        result = await self.store.perform_maintenance(
            job_cutoff_ms=0,
            limit=100,
            now_ms=2_000,
            run_cutoff_ms=0,
            scan_cutoff_ms=0,
        )

        job = self.store.connection.execute(
            "SELECT status, last_error FROM sitemap_jobs WHERE id = ?",
            (original.job_id,),
        ).fetchone()
        self.assertEqual(result.expired_jobs, 1)
        self.assertEqual(job["status"], "dead")
        self.assertEqual(job["last_error"], "superseded_schedule")

    async def test_maintenance_prunes_low_value_runs_and_unreferenced_jobs(self) -> None:
        _, _ = await self._new_job(max_attempts=1)
        claimed = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_010,
            )
        )[0]
        await self.store.finish_job_failure(
            claimed,
            available_at_ms=2_000,
            dead=True,
            error="http_404",
            finished_at_ms=1_100,
            next_check_at_ms=86_401_100,
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE sitemap_jobs SET updated_at = 100 WHERE id = ?",
                (claimed.job_id,),
            )
            self.store.connection.execute(
                """
                INSERT INTO sitemap_resources (
                    id, site_id, url, type, created_at, updated_at
                ) VALUES ('resource_old', ?, 'https://example.com/sitemap.xml',
                          'unknown', 100, 100)
                """,
                (self.site_id,),
            )
            for run_id, result in (("run_failed", "failed"), ("run_changed", "changed")):
                self.store.connection.execute(
                    """
                    INSERT INTO sitemap_runs (
                        id, run_key, site_id, resource_id, started_at,
                        finished_at, result, created_at
                    ) VALUES (?, ?, ?, 'resource_old', 100, 100, ?, 100)
                    """,
                    (run_id, run_id, self.site_id, result),
                )

        result = await self.store.perform_maintenance(
            job_cutoff_ms=500,
            limit=100,
            now_ms=1_000,
            run_cutoff_ms=500,
            scan_cutoff_ms=500,
        )

        remaining_runs = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT id FROM sitemap_runs"
            ).fetchall()
        }
        self.assertEqual(result.pruned_jobs, 1)
        self.assertEqual(result.pruned_runs, 1)
        self.assertEqual(remaining_runs, {"run_changed"})

    async def test_expired_final_attempt_is_recoverable_instead_of_stuck(self) -> None:
        await self._new_job(max_attempts=1)
        abandoned = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_010,
            )
        )[0]
        self.assertEqual(abandoned.attempts, 1)
        recovered = (
            await self.store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-b",
                limit=1,
                now_ms=2_011,
            )
        )[0]
        self.assertEqual(recovered.job_id, abandoned.job_id)
        self.assertEqual(recovered.attempts, 2)
        self.assertTrue(
            await self.store.finish_job_success(
                recovered,
                finished_at_ms=2_100,
                next_check_at_ms=3_602_100,
            )
        )


class SchedulerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_failure_uses_one_attempt_and_site_cooldown_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SqliteMetadataStore(root / "scheduler.sqlite3")
            homepage = "https://example.com/"
            sitemap = "https://example.com/sitemap.xml"
            robots = "https://example.com/robots.txt"
            fetcher = FakeFetcher(
                {
                    robots: [response(robots, None, 404) for _ in range(3)],
                    sitemap: [response(sitemap, None, 404) for _ in range(3)],
                }
            )
            monitor = SitemapMonitor(
                store,
                FileObjectStore(root / "objects"),
                fetcher=fetcher,
            )
            clock = [1_000]
            scheduler = SitemapScheduler(
                store,
                monitor,
                explicit_sitemaps=[sitemap],
                lease_owner="scheduler-test",
                clock_ms=lambda: clock[0],
                policy=SchedulerPolicy(
                    check_interval_sec=21_600,
                    job_lease_sec=30,
                    jitter_ratio=0,
                ),
            )
            try:
                expected_delays = (86_400_000, 259_200_000, 604_800_000)
                for expected_streak, expected_delay in enumerate(
                    expected_delays,
                    start=1,
                ):
                    tick = await scheduler.run_once([homepage])
                    self.assertEqual(tick.executions[0].status, "dead")
                    job = store.connection.execute(
                        """
                        SELECT attempts
                        FROM sitemap_jobs
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    site = store.connection.execute(
                        """
                        SELECT error_streak, next_check_at
                        FROM sitemap_sites WHERE id = ?
                        """,
                        (site_id_for(homepage),),
                    ).fetchone()
                    self.assertEqual(job["attempts"], 1)
                    self.assertEqual(site["error_streak"], expected_streak)
                    self.assertEqual(site["next_check_at"], clock[0] + expected_delay)
                    clock[0] = site["next_check_at"]
            finally:
                await monitor.close()
                store.close()

    async def test_success_sets_next_due_and_idle_tick_does_not_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SqliteMetadataStore(root / "scheduler.sqlite3")
            sitemap = "https://example.com/sitemap.xml"
            robots = "https://example.com/robots.txt"
            fetcher = FakeFetcher(
                {
                    robots: [response(robots, None, 404), response(robots, None, 404)],
                    sitemap: [
                        response(sitemap, URLSET_A, etag='"v1"'),
                        response(sitemap, None, 304),
                    ],
                }
            )
            monitor = SitemapMonitor(store, FileObjectStore(root / "objects"), fetcher=fetcher)
            clock = [1_000]
            scheduler = SitemapScheduler(
                store,
                monitor,
                explicit_sitemaps=[sitemap],
                lease_owner="scheduler-test",
                clock_ms=lambda: clock[0],
                policy=SchedulerPolicy(
                    check_interval_sec=3_600,
                    job_lease_sec=30,
                    jitter_ratio=0,
                ),
            )
            try:
                first = await scheduler.run_once(["https://example.com/"])
                self.assertEqual(first.jobs_claimed, 1)
                self.assertEqual(first.executions[0].status, "succeeded")
                self.assertEqual(
                    first.executions[0].comparability.status,
                    "baseline_invalid",
                )
                self.assertTrue(
                    first.executions[0].comparability.promote_semantic_baseline
                )
                row = store.connection.execute(
                    "SELECT next_check_at FROM sitemap_sites WHERE id = ?",
                    (site_id_for("https://example.com/"),),
                ).fetchone()
                self.assertEqual(row["next_check_at"], 3_601_000)

                idle = await scheduler.run_once(["https://example.com/"])
                self.assertEqual(idle.jobs_claimed, 0)
                self.assertEqual(len(fetcher.validators[sitemap]), 1)

                clock[0] = 3_601_000
                second = await scheduler.run_once(["https://example.com/"])
                self.assertEqual(second.jobs_claimed, 1)
                self.assertEqual(second.executions[0].status, "succeeded")
                self.assertEqual(second.executions[0].comparability.status, "comparable")
                self.assertTrue(second.executions[0].comparability.is_comparable)
                self.assertEqual(len(fetcher.validators[sitemap]), 2)
                promoted = store.connection.execute(
                    "SELECT semantic_baseline_scan_id FROM sitemap_sites WHERE id = ?",
                    (site_id_for("https://example.com/"),),
                ).fetchone()
                self.assertEqual(
                    promoted["semantic_baseline_scan_id"],
                    second.executions[0].result.scan_id,
                )
                scans = store.connection.execute(
                    """
                    SELECT id, comparability_status, promoted_semantic_baseline
                    FROM sitemap_site_scans
                    ORDER BY finished_at, id
                    """
                ).fetchall()
                self.assertEqual(len(scans), 2)
                self.assertEqual(
                    [row["comparability_status"] for row in scans],
                    ["baseline_invalid", "comparable"],
                )
                linked_runs = store.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sitemap_runs
                    WHERE site_scan_id IS NOT NULL AND state_key IS NOT NULL
                    """
                ).fetchone()[0]
                self.assertEqual(linked_runs, 2)
            finally:
                await monitor.close()
                store.close()

    async def test_partial_child_scan_is_recorded_without_advancing_semantic_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SqliteMetadataStore(root / "scheduler.sqlite3")
            index = "https://example.com/sitemap.xml"
            child = "https://example.com/pages.xml"
            robots = "https://example.com/robots.txt"
            fetcher = FakeFetcher(
                {
                    robots: [response(robots, None, 404), response(robots, None, 404)],
                    index: [
                        response(index, INDEX, etag='"index-v1"'),
                        response(index, None, 304),
                    ],
                    child: [
                        response(child, URLSET_A, etag='"child-v1"'),
                        response(child, None, 502),
                    ],
                }
            )
            monitor = SitemapMonitor(store, FileObjectStore(root / "objects"), fetcher=fetcher)
            clock = [1_000]
            scheduler = SitemapScheduler(
                store,
                monitor,
                explicit_sitemaps=[index],
                lease_owner="scheduler-test",
                clock_ms=lambda: clock[0],
                policy=SchedulerPolicy(
                    check_interval_sec=3_600,
                    job_lease_sec=30,
                    jitter_ratio=0,
                ),
            )
            try:
                first = await scheduler.run_once(["https://example.com/"])
                baseline_scan_id = first.executions[0].result.scan_id
                self.assertEqual(
                    first.executions[0].comparability.status,
                    "baseline_invalid",
                )

                clock[0] = 3_601_000
                second = await scheduler.run_once(["https://example.com/"])
                self.assertEqual(second.executions[0].status, "succeeded")
                self.assertEqual(second.executions[0].comparability.status, "partial")
                self.assertFalse(second.executions[0].comparability.is_comparable)
                site = store.connection.execute(
                    "SELECT semantic_baseline_scan_id FROM sitemap_sites WHERE id = ?",
                    (site_id_for("https://example.com/"),),
                ).fetchone()
                latest_scan = store.connection.execute(
                    """
                    SELECT comparability_status, promoted_semantic_baseline
                    FROM sitemap_site_scans
                    ORDER BY finished_at DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(site["semantic_baseline_scan_id"], baseline_scan_id)
                self.assertEqual(latest_scan["comparability_status"], "partial")
                self.assertEqual(latest_scan["promoted_semantic_baseline"], 0)
            finally:
                await monitor.close()
                store.close()

    async def test_failed_site_attempt_keeps_fetch_incomplete_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SqliteMetadataStore(root / "scheduler.sqlite3")
            sitemap = "https://example.com/sitemap.xml"
            robots = "https://example.com/robots.txt"
            fetcher = FakeFetcher(
                {
                    robots: [response(robots, None, 404)],
                    sitemap: [response(sitemap, None, 502)],
                }
            )
            monitor = SitemapMonitor(store, FileObjectStore(root / "objects"), fetcher=fetcher)
            scheduler = SitemapScheduler(
                store,
                monitor,
                explicit_sitemaps=[sitemap],
                lease_owner="scheduler-test",
                clock_ms=lambda: 1_000,
                policy=SchedulerPolicy(
                    check_interval_sec=3_600,
                    job_lease_sec=30,
                    jitter_ratio=0,
                ),
            )
            try:
                tick = await scheduler.run_once(["https://example.com/"])
                self.assertEqual(tick.executions[0].status, "retry")
                site = store.connection.execute(
                    "SELECT error_streak FROM sitemap_sites LIMIT 1"
                ).fetchone()
                self.assertEqual(site["error_streak"], 0)
                self.assertEqual(
                    tick.executions[0].comparability.status,
                    "fetch_incomplete",
                )
                scan = store.connection.execute(
                    """
                    SELECT comparability_status, is_committed,
                           promoted_semantic_baseline
                    FROM sitemap_site_scans
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(scan["comparability_status"], "fetch_incomplete")
                self.assertEqual(scan["is_committed"], 1)
                self.assertEqual(scan["promoted_semantic_baseline"], 0)
            finally:
                await monitor.close()
                store.close()


class CatalogSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_loader_pages_filters_and_deduplicates_origins(self) -> None:
        client = SqliteD1ClientDouble()
        client.connection.executescript(
            """
            CREATE TABLE tools (
                id INTEGER PRIMARY KEY,
                official_url TEXT NOT NULL,
                normalized_domain TEXT NOT NULL,
                status TEXT NOT NULL,
                content_safety_status TEXT NOT NULL,
                duplicate_of_tool_id INTEGER
            );
            INSERT INTO tools VALUES
                (1, 'https://example.com/product?ref=catalog', 'example.com', 'published', 'safe', NULL),
                (2, 'https://www.example.com/', 'example.com', 'published', 'safe', NULL),
                (3, 'https://unsafe.example/', 'unsafe.example', 'published', 'nsfw', NULL),
                (4, 'https://duplicate.example/', 'duplicate.example', 'published', 'safe', 1),
                (5, 'https://example.com/other', 'alias.example', 'published', 'safe', NULL),
                (6, 'file:///not-public', '127.0.0.1', 'published', 'safe', NULL);
            """
        )
        try:
            snapshot = await load_published_catalog_sites(client, page_size=2)
        finally:
            client.connection.close()

        self.assertEqual(snapshot.homepage_urls, ("https://example.com/",))
        self.assertEqual(snapshot.source_rows, 3)
        self.assertEqual(snapshot.duplicate_origins, 1)
        self.assertEqual(snapshot.invalid_urls, 1)


class DeploymentBoundaryTests(unittest.TestCase):
    def test_cli_accepts_repeatable_site_files(self) -> None:
        args = parse_args([
            "--site-file", "one.txt", "--site-file", "two.txt",
            "--paused-site-file", "paused.txt",
        ])
        self.assertEqual(args.site_file, ["one.txt", "two.txt"])
        self.assertEqual(args.paused_site_file, ["paused.txt"])
        self.assertEqual(args.check_interval_seconds, 21_600)
        self.assertEqual(args.run_detail_retention_days, 7)
        self.assertEqual(args.execution_concurrency, 1)
        self.assertEqual(args.catalog_refresh_seconds, 3_600)
        self.assertEqual(args.catalog_page_size, 500)

    def test_formal_migration_builds_only_phase_one_metadata_tables(self) -> None:
        migration = (
            Path(__file__).parent.parent
            / "ainav"
            / "d1"
            / "migrations"
            / "0060_sitemap_monitor_phase1.sql"
        ).read_text(encoding="utf-8")
        database = sqlite3.connect(":memory:")
        database.executescript("PRAGMA foreign_keys = ON;\n" + migration)
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sitemap_%'"
            )
        }
        database.close()
        self.assertEqual(
            tables,
            {"sitemap_jobs", "sitemap_resources", "sitemap_runs", "sitemap_sites"},
        )
        self.assertNotIn("change_events", migration)

    def test_comparability_migration_adds_site_scan_without_signal_tables(self) -> None:
        migrations = Path(__file__).parent.parent / "ainav" / "d1" / "migrations"
        phase_one = (migrations / "0060_sitemap_monitor_phase1.sql").read_text(
            encoding="utf-8"
        )
        comparability = (
            migrations / "0061_sitemap_comparability_gate.sql"
        ).read_text(encoding="utf-8")
        database = sqlite3.connect(":memory:")
        database.executescript(
            "PRAGMA foreign_keys = ON;\n" + phase_one + "\n" + comparability
        )
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sitemap_%'"
            )
        }
        run_columns = {
            row[1] for row in database.execute("PRAGMA table_info(sitemap_runs)")
        }
        site_columns = {
            row[1] for row in database.execute("PRAGMA table_info(sitemap_sites)")
        }
        database.close()
        self.assertIn("sitemap_site_scans", tables)
        self.assertIn("site_scan_id", run_columns)
        self.assertIn("state_key", run_columns)
        self.assertIn("semantic_baseline_scan_id", site_columns)
        self.assertNotIn("signals", comparability)

    def test_managed_service_is_scaled_to_zero_and_has_a_runtime_kill_switch(self) -> None:
        compose = Path("docker-compose.dokploy.yml").read_text(encoding="utf-8")
        service = compose.split("  sitemap-monitor-worker:", 1)[1].split(
            "\n  taxonomy-worker:", 1
        )[0]
        self.assertIn("replicas: ${SITEMAP_MONITOR_REPLICAS:-0}", service)
        self.assertIn("- --require-enabled", service)
        self.assertIn("${SITEMAP_MONITOR_CHECK_INTERVAL_SECONDS:-21600}", service)
        self.assertIn("${SITEMAP_MONITOR_RUN_DETAIL_RETENTION_DAYS:-7}", service)
        self.assertIn("${SITEMAP_MONITOR_MAINTENANCE_BATCH_SIZE:-500}", service)
        self.assertIn("- --catalog-sync", service)
        self.assertIn("${SITEMAP_MONITOR_CATALOG_REFRESH_SECONDS:-3600}", service)
        self.assertIn("${SITEMAP_MONITOR_CATALOG_PAGE_SIZE:-500}", service)
        self.assertIn("${SITEMAP_MONITOR_EXECUTION_CONCURRENCY:-8}", service)
        self.assertIn("SITEMAP_MONITOR_ENABLED: ${SITEMAP_MONITOR_ENABLED:-0}", service)
        self.assertIn(
            "SITEMAP_MONITOR_PAUSED_SITE_FILE: ${SITEMAP_MONITOR_PAUSED_SITE_FILE:-}",
            service,
        )
        self.assertIn(
            "FOR_ALL_APP_R2_ACCESS_KEY_ID: ${FOR_ALL_APP_R2_ACCESS_KEY_ID}",
            service,
        )
        self.assertIn(
            "FOR_ALL_APP_R2_SECRET_ACCESS_KEY: ${FOR_ALL_APP_R2_SECRET_ACCESS_KEY}",
            service,
        )
        self.assertNotIn("SITEMAP_MONITOR_R2_ACCESS_KEY_ID", service)
        self.assertNotIn("SITEMAP_MONITOR_R2_SECRET_ACCESS_KEY", service)


class CloudflareMetadataAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_d1_batch_uses_cloudflare_batch_envelope(self) -> None:
        received: list[object] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"success": True, "results": []},
                        {"success": True, "results": [{"value": 1}]},
                    ],
                },
            )

        client = CloudflareD1Client(
            account_id="account",
            api_token="token",
            database_id="database",
        )
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await client.batch(
                [("INSERT INTO test(value) VALUES (?)", [1]), ("SELECT 1", [])]
            )
        finally:
            await client.close()

        self.assertEqual(
            received,
            [
                {
                    "batch": [
                        {"sql": "INSERT INTO test(value) VALUES (?)", "params": [1]},
                        {"sql": "SELECT 1", "params": []},
                    ]
                }
            ],
        )
        self.assertEqual(results[1]["results"], [{"value": 1}])

    async def test_d1_site_status_transition_invalidates_schedule(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = site_id_for("https://example.com/")
        await store.ensure_site(site_id, "https://example.com/", 1_000)
        before = client.connection.execute(
            "SELECT schedule_version FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()[0]

        await store.set_site_status(
            site_id,
            "paused",
            2_000,
            from_status="active",
        )
        row = client.connection.execute(
            "SELECT status, schedule_version FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["schedule_version"], before + 1)

        await store.set_site_status(
            site_id,
            "active",
            3_000,
            from_status="paused",
        )
        row = client.connection.execute(
            "SELECT status, schedule_version, next_check_at FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()
        client.connection.close()
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["schedule_version"], before + 2)
        self.assertEqual(row["next_check_at"], 3_000)

    async def test_d1_adapter_bulk_registers_and_lists_catalog_sites(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        registrations = tuple(
            (site_id_for(url), url)
            for url in ("https://one.example/", "https://two.example/")
        )
        await store.ensure_sites(
            registrations,
            1_000,
            check_interval_sec=3_600,
        )
        sites = await store.list_registered_sites()
        client.connection.close()

        self.assertEqual(len(sites), 2)
        self.assertEqual({site.status for site in sites}, {"active"})
        self.assertEqual({site.check_interval_sec for site in sites}, {3_600})

    async def test_d1_failure_completion_is_retry_safe_and_idempotent(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = site_id_for("https://example.com/")
        await store.ensure_site(site_id, "https://example.com/", 1_000)
        due = (
            await store.claim_due_sites(
                lease_duration_ms=1_000,
                lease_owner="scheduler-a",
                limit=1,
                now_ms=1_000,
            )
        )[0]
        await store.ensure_job(due, max_attempts=3, now_ms=1_000)
        job = (
            await store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_001,
            )
        )[0]
        self.assertTrue(
            await store.finish_job_failure(
                job,
                available_at_ms=2_000,
                dead=False,
                error="http_503",
                finished_at_ms=1_100,
                next_check_at_ms=None,
            )
        )
        self.assertFalse(
            await store.finish_job_failure(
                job,
                available_at_ms=2_000,
                dead=False,
                error="http_503",
                finished_at_ms=1_100,
                next_check_at_ms=None,
            )
        )
        streak = client.connection.execute(
            "SELECT error_streak FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()[0]
        client.connection.close()
        self.assertEqual(streak, 0)

    async def test_d1_maintenance_expires_superseded_jobs(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = site_id_for("https://example.com/")
        await store.ensure_site(
            site_id,
            "https://example.com/",
            1_000,
            check_interval_sec=3_600,
        )
        due = (
            await store.claim_due_sites(
                lease_duration_ms=1_000,
                lease_owner="scheduler-a",
                limit=1,
                now_ms=1_000,
            )
        )[0]
        job = await store.ensure_job(due, max_attempts=3, now_ms=1_000)
        await store.ensure_site(
            site_id,
            "https://example.com/",
            1_500,
            check_interval_sec=7_200,
        )

        result = await store.perform_maintenance(
            job_cutoff_ms=0,
            limit=100,
            now_ms=2_000,
            run_cutoff_ms=0,
            scan_cutoff_ms=0,
        )

        stored = client.connection.execute(
            "SELECT status, last_error FROM sitemap_jobs WHERE id = ?",
            (job.job_id,),
        ).fetchone()
        client.connection.close()
        self.assertEqual(result.expired_jobs, 1)
        self.assertEqual(stored["status"], "dead")
        self.assertEqual(stored["last_error"], "superseded_schedule")

    async def test_d1_scheduler_adapter_matches_atomic_lease_contract(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = site_id_for("https://example.com/")
        await store.ensure_site(
            site_id,
            "https://example.com/",
            1_000,
            check_interval_sec=3_600,
        )
        due = await store.claim_due_sites(
            lease_duration_ms=1_000,
            lease_owner="scheduler-a",
            limit=10,
            now_ms=1_000,
        )
        self.assertEqual(len(due), 1)
        job = await store.ensure_job(due[0], max_attempts=3, now_ms=1_000)
        self.assertIsNotNone(job)
        claimed = await store.claim_jobs(
            lease_duration_ms=1_000,
            lease_owner="worker-a",
            limit=10,
            now_ms=1_001,
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].attempts, 1)
        self.assertTrue(
            await store.renew_job_lease(
                claimed[0],
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                now_ms=1_500,
            )
        )
        self.assertTrue(
            await store.finish_job_success(
                claimed[0],
                finished_at_ms=1_600,
                next_check_at_ms=3_601_600,
            )
        )
        site = client.connection.execute(
            "SELECT next_check_at, last_success_at FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()
        client.connection.close()
        self.assertEqual(site["next_check_at"], 3_601_600)
        self.assertEqual(site["last_success_at"], 1_600)

    async def test_d1_adapter_commits_site_scan_and_semantic_baseline_atomically(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = site_id_for("https://example.com/")
        await store.ensure_site(site_id, "https://example.com/", 1_000)
        due = (
            await store.claim_due_sites(
                lease_duration_ms=1_000,
                lease_owner="scheduler-a",
                limit=1,
                now_ms=1_000,
            )
        )[0]
        await store.ensure_job(due, max_attempts=3, now_ms=1_000)
        job = (
            await store.claim_jobs(
                lease_duration_ms=1_000,
                lease_owner="worker-a",
                limit=1,
                now_ms=1_001,
            )
        )[0]
        sitemap = "https://example.com/sitemap.xml"
        outcome = CheckOutcome(
            added_count=0,
            bytes_downloaded=100,
            diff_key=None,
            error_code=None,
            final_url=sitemap,
            http_status=200,
            modified_count=0,
            parent_id=None,
            removed_count=0,
            resource_id="resource_1",
            result="baseline",
            sitemap_kind="urlset",
            state_key="state/resource_1.jsonl.gz",
            url=sitemap,
            url_count=10,
        )
        snapshot = build_site_scan_snapshot(
            SiteScanResult(
                discovery_mode="explicit",
                finished_at_ms=1_500,
                homepage_url="https://example.com/",
                outcomes=(outcome,),
                scan_id="scan_1",
                site_id=site_id,
                started_at_ms=1_100,
            )
        )
        assessment = assess_comparability(snapshot, baseline=None, previous=None)

        self.assertTrue(
            await store.finish_job_success(
                job,
                finished_at_ms=1_600,
                next_check_at_ms=3_601_600,
                comparability=assessment,
                site_scan=snapshot,
            )
        )
        scan = client.connection.execute(
            """
            SELECT comparability_status, is_committed, promoted_semantic_baseline
            FROM sitemap_site_scans WHERE id = 'scan_1'
            """
        ).fetchone()
        site = client.connection.execute(
            "SELECT semantic_baseline_scan_id FROM sitemap_sites WHERE id = ?",
            (site_id,),
        ).fetchone()
        client.connection.close()
        self.assertEqual(scan["comparability_status"], "baseline_invalid")
        self.assertEqual(scan["is_committed"], 1)
        self.assertEqual(scan["promoted_semantic_baseline"], 1)
        self.assertEqual(site["semantic_baseline_scan_id"], "scan_1")

    async def test_d1_adapter_uses_the_same_parameterized_state_contract(self) -> None:
        client = SqliteD1ClientDouble()
        store = CloudflareD1MetadataStore(client)  # type: ignore[arg-type]
        site_id = "site_test"
        resource_id = "smr_test"
        now = 1_786_600_000_000
        await store.ensure_site(site_id, "https://example.com/", now)
        state = ResourceState(
            content_hash="a" * 64,
            current_state_key="state/site_test/smr_test/state.jsonl.gz",
            etag='"v1"',
            last_modified="Wed, 12 Aug 2026 00:00:00 GMT",
            metadata_hash="b" * 64,
            resource_id=resource_id,
            sitemap_kind="urlset",
            url="https://example.com/sitemap.xml",
            url_count=2,
            urlset_hash="c" * 64,
        )
        await store.save_resource(
            site_id,
            state,
            canonical_fetch_url=state.url,
            parent_id=None,
            checked_at_ms=now,
            changed=False,
        )
        self.assertEqual(await store.get_resource(site_id, state.url), state)

        outcome = CheckOutcome(
            added_count=0,
            bytes_downloaded=100,
            diff_key=None,
            error_code=None,
            final_url=state.url,
            http_status=200,
            modified_count=0,
            resource_id=resource_id,
            result="baseline",
            sitemap_kind="urlset",
            state_key=state.current_state_key,
            url=state.url,
            url_count=2,
            removed_count=0,
        )
        await store.record_run(
            site_id,
            outcome,
            finished_at_ms=now + 10,
            run_key="job:test",
            started_at_ms=now,
        )
        count = client.connection.execute("SELECT COUNT(*) FROM sitemap_runs").fetchone()[0]
        client.connection.close()
        self.assertEqual(count, 1)

    async def test_r2_adapter_signs_private_object_paths_and_blocks_traversal(self) -> None:
        store = CloudflareR2ObjectStore(
            access_key_id="access",
            account_id="account",
            bucket="private-sitemap-state",
            secret_access_key="secret",
        )
        url, headers = store._signed_request(
            "PUT",
            "state/site/resource/current.jsonl.gz",
            b"payload",
            "application/gzip",
        )
        self.assertIn("private-sitemap-state/state/site/resource/current.jsonl.gz", url)
        self.assertIn("authorization", headers)
        with self.assertRaises(ValueError):
            store._signed_request("GET", "../secret", b"", None)
        await store.close()


if __name__ == "__main__":
    unittest.main()
