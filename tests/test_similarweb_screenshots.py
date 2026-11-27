import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from workloads.assets.similarweb_screenshots import (
    parse_image_dimensions,
    sync_similarweb_screenshots,
)


def make_png(width: int, height: int, pad_bytes: int = 4500) -> bytes:
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    idat_payload = b"\x00" * pad_bytes
    idat = struct.pack(">I", len(idat_payload)) + b"IDAT" + idat_payload + b"\x00\x00\x00\x00"
    iend = b"\x00\x00\x00\x00IEND\xaeB`\x82"
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


class SimilarwebScreenshotsTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_image_dimensions_validates_png_and_rejects_tiny_html(self):
        png = make_png(280, 210)
        self.assertEqual(parse_image_dimensions(png, "image/png"), (280, 210))
        self.assertIsNone(parse_image_dimensions(make_png(50, 40), "image/png"))
        self.assertIsNone(parse_image_dimensions(b"<html><body>Not Found</body></html>" * 80, "image/png"))
        self.assertIsNone(parse_image_dimensions(b"\x89PNG" + b"\x00" * 100, "image/png"))

    async def test_dry_run_skips_r2_and_db_writes_and_isolates_failures(self):
        fake_pg = MagicMock()
        fake_pg.__aenter__ = AsyncMock(return_value=fake_pg)
        fake_pg.__aexit__ = AsyncMock(return_value=False)
        fake_pg.query = AsyncMock(
            return_value=[
                {
                    "tool_id": 101,
                    "canonical_slug": "good-tool",
                    "normalized_domain": "good.ai",
                    "seo_index_status": "indexable",
                    "large_screenshot": "https://site-images.similarcdn.com/good.png",
                },
                {
                    "tool_id": 102,
                    "canonical_slug": "bad-tool",
                    "normalized_domain": "bad.ai",
                    "seo_index_status": "noindex",
                    "large_screenshot": "https://site-images.similarcdn.com/bad.png",
                },
            ]
        )
        fake_pg.run = AsyncMock()

        good_png = make_png(280, 210)

        async def fake_download(url: str, _client):
            if "bad.png" in url:
                raise RuntimeError("sw_screenshot_http_404")
            return good_png, "image/png", 280, 210

        config = SimpleNamespace(
            cloudflare_account_id="acct",
            r2_access_key_id="",
            r2_secret_access_key="",
            r2_bucket="sitesimgs",
            r2_public_base_url="https://img.sigpik.com",
        )

        with patch("workloads.assets.similarweb_screenshots.PostgresClient", return_value=fake_pg), \
             patch("workloads.assets.r2.R2AssetUploader") as r2_cls, \
             patch("workloads.assets.similarweb_screenshots.download_similarweb_screenshot", side_effect=fake_download):
            result = await sync_similarweb_screenshots(
                config,
                limit=10,
                concurrency=2,
                dry_run=True,
                database_url="postgresql://readonly@127.0.0.1:5432/sigpik",
            )

        r2_cls.assert_not_called()
        fake_pg.run.assert_not_called()
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertTrue(result["samples"][0]["r2_key"].startswith("sw/good.ai/"))

    async def test_live_sync_uses_sw_prefix_and_guarded_insert(self):
        fake_pg = MagicMock()
        fake_pg.__aenter__ = AsyncMock(return_value=fake_pg)
        fake_pg.__aexit__ = AsyncMock(return_value=False)
        fake_pg.query = AsyncMock(
            return_value=[
                {
                    "tool_id": 201,
                    "canonical_slug": "alpha",
                    "normalized_domain": "alpha.ai",
                    "seo_index_status": "indexable",
                    "large_screenshot": "https://site-images.similarcdn.com/alpha.png",
                }
            ]
        )
        fake_pg.run = AsyncMock(return_value={"changes": 1})

        fake_uploader = MagicMock()
        fake_uploader.put_object = AsyncMock()

        config = SimpleNamespace(
            cloudflare_account_id="acct",
            r2_access_key_id="k",
            r2_secret_access_key="s",
            r2_bucket="sitesimgs",
            r2_public_base_url="https://img.sigpik.com",
        )
        png = make_png(280, 210)
        with patch("workloads.assets.similarweb_screenshots.PostgresClient", return_value=fake_pg), \
             patch("workloads.assets.r2.R2AssetUploader", return_value=fake_uploader), \
             patch("workloads.assets.similarweb_screenshots.download_similarweb_screenshot", AsyncMock(return_value=(png, "image/png", 280, 210))):
            result = await sync_similarweb_screenshots(
                config,
                limit=10,
                dry_run=False,
                database_url="postgresql://rw@127.0.0.1:5432/sigpik",
            )

        self.assertEqual(result["inserted"], 1)
        put_key = fake_uploader.put_object.call_args.args[0]
        self.assertTrue(put_key.startswith("sw/alpha.ai/"))
        stmt = fake_pg.run.call_args.args[0]
        self.assertIn("WHERE NOT EXISTS", stmt.sql)


if __name__ == "__main__":
    unittest.main()
