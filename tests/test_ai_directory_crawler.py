import json
import pytest
import respx
import httpx

from scripts.crawl_ai_directories import (
    normalize_domain,
    clean_url,
    is_skipped_domain,
    stable_hash,
    candidate_evidence_fingerprint,
    FuturepediaCrawler,
    AITopToolsCrawler,
    DangCrawler,
    run_crawler_pipeline,
)

SAMPLE_FUTUREPEDIA_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.futurepedia.io/tool/cool-ai</loc>
    <lastmod>2026-10-28T02:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://www.futurepedia.io/tool/older-tool</loc>
    <lastmod>2026-10-20T00:00:00.000Z</lastmod>
  </url>
</urlset>
"""

SAMPLE_FUTUREPEDIA_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Cool AI - Futurepedia</title>
  <meta name="description" content="An amazing AI assistant for productivity" />
</head>
<body>
  <h1>Cool AI</h1>
  <a href="https://coolai.example.com/?utm_source=futurepedia&utm_medium=directory">Visit Website</a>
  <a href="https://twitter.com/coolai">Twitter</a>
</body>
</html>
"""

SAMPLE_AITOPTOOLS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://aitoptools.com/tool/awesome-agent/</loc>
    <lastmod>2026-10-28T03:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://aitoptools.com/categories/</loc>
    <lastmod>2026-10-20T00:00:00.000Z</lastmod>
  </url>
</urlset>
"""

SAMPLE_AITOPTOOLS_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Awesome Agent Reviews | AITopTools</title>
  <meta name="description" content="Autonomous AI Agent for developers" />
</head>
<body>
  <h1>Awesome AgentVerified tool</h1>
  <a href="https://awesomeagent.dev/?utm_source=aitoptools">VisitAwesome Agent</a>
</body>
</html>
"""

SAMPLE_DANG_LISTING_HTML = """<!DOCTYPE html>
<html>
<body>
  <a href="/tool/fast-ai-coder">Fast AI</a>
  <a href="/category/dev">Dev</a>
</body>
</html>
"""

SAMPLE_DANG_TOOL_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="description" content="AI code completion in seconds" />
</head>
<body>
  <h1>Fast AI Coder</h1>
  <a href="https://l.dang.ai/abc1234">View tool</a>
</body>
</html>
"""


def test_normalize_domain():
    assert normalize_domain("https://WWW.Example.COM/test?param=1") == "example.com"
    assert normalize_domain("http://sub.domain.org/") == "sub.domain.org"
    assert normalize_domain("app.ai") == "app.ai"
    assert normalize_domain("") is None
    assert normalize_domain("   ") is None


def test_clean_url():
    url = "https://example.com/app?utm_source=test&utm_medium=cpc&id=42&ref=banner"
    cleaned = clean_url(url)
    assert "utm_source" not in cleaned
    assert "utm_medium" not in cleaned
    assert "ref" not in cleaned
    assert "id=42" in cleaned
    assert cleaned.startswith("https://example.com/app?id=42")


def test_is_skipped_domain():
    assert is_skipped_domain("twitter.com") is True
    assert is_skipped_domain("sub.x.com") is True
    assert is_skipped_domain("github.com") is True
    assert is_skipped_domain("futurepedia.io") is True
    assert is_skipped_domain("my-cool-product.ai") is False


def test_stable_hash():
    assert stable_hash("hello world") == "d58b3fa7"


def test_candidate_evidence_fingerprint():
    cand = {
        "confidence_score": 88,
        "description_hint": "An awesome AI tool",
        "normalized_domain": "awesome.ai",
        "official_url": "https://awesome.ai",
        "submitted_name": "Awesome AI",
    }
    fp = candidate_evidence_fingerprint(cand)
    assert isinstance(fp, str)
    assert len(fp) >= 6


@pytest.mark.asyncio
@respx.mock
async def test_futurepedia_crawler():
    respx.get("https://www.futurepedia.io/sitemap_tools.xml").respond(
        status_code=200,
        text=SAMPLE_FUTUREPEDIA_SITEMAP
    )
    respx.get("https://www.futurepedia.io/tool/cool-ai").respond(
        status_code=200,
        text=SAMPLE_FUTUREPEDIA_HTML
    )

    async with httpx.AsyncClient() as client:
        crawler = FuturepediaCrawler(client)
        entries = await crawler.discover_entries(limit=5)
        assert len(entries) == 2
        assert entries[0]["slug"] == "cool-ai"

        tool = await crawler.extract_tool(entries[0])
        assert tool is not None
        assert tool.source == "ai_directory_import"
        assert tool.source_id == "ai_directory:futurepedia:cool-ai"
        assert tool.normalized_domain == "coolai.example.com"
        assert tool.official_url.startswith("https://coolai.example.com")
        assert "utm_source" not in tool.official_url
        assert tool.submitted_name == "Cool AI"
        assert "productivity" in tool.description_hint


@pytest.mark.asyncio
@respx.mock
async def test_aitoptools_crawler():
    respx.get("https://aitoptools.com/sitemap.xml").respond(
        status_code=200,
        text=SAMPLE_AITOPTOOLS_SITEMAP
    )
    respx.get("https://aitoptools.com/tool/awesome-agent/").respond(
        status_code=200,
        text=SAMPLE_AITOPTOOLS_HTML
    )

    async with httpx.AsyncClient() as client:
        crawler = AITopToolsCrawler(client)
        entries = await crawler.discover_entries(limit=5)
        assert len(entries) == 1
        assert entries[0]["slug"] == "awesome-agent"

        tool = await crawler.extract_tool(entries[0])
        assert tool is not None
        assert tool.source_id == "ai_directory:aitoptools:awesome-agent"
        assert tool.normalized_domain == "awesomeagent.dev"
        assert tool.submitted_name == "Awesome Agent"  # "Verified tool" suffix stripped


@pytest.mark.asyncio
@respx.mock
async def test_dang_crawler():
    respx.get("https://dang.ai/new?page=1").respond(
        status_code=200,
        text=SAMPLE_DANG_LISTING_HTML
    )
    respx.get("https://dang.ai/tool/fast-ai-coder").respond(
        status_code=200,
        text=SAMPLE_DANG_TOOL_HTML
    )
    respx.get("https://l.dang.ai/abc1234").respond(
        status_code=302,
        headers={"Location": "https://fastai.io/app?utm_source=dang"}
    )

    async with httpx.AsyncClient() as client:
        crawler = DangCrawler(client)
        entries = await crawler.discover_entries(limit=5)
        assert len(entries) == 1
        assert entries[0]["slug"] == "fast-ai-coder"

        tool = await crawler.extract_tool(entries[0])
        assert tool is not None
        assert tool.source_id == "ai_directory:dang:fast-ai-coder"
        assert tool.normalized_domain == "fastai.io"
        assert tool.official_url.startswith("https://fastai.io/app")
        assert tool.submitted_name == "Fast AI Coder"


@pytest.mark.asyncio
@respx.mock
async def test_run_crawler_pipeline_dry_run():
    respx.get("https://www.futurepedia.io/sitemap_tools.xml").respond(
        status_code=200,
        text=SAMPLE_FUTUREPEDIA_SITEMAP
    )
    respx.get("https://www.futurepedia.io/tool/cool-ai").respond(
        status_code=200,
        text=SAMPLE_FUTUREPEDIA_HTML
    )
    respx.get("https://www.futurepedia.io/tool/older-tool").respond(
        status_code=404
    )

    res = await run_crawler_pipeline(
        sources=["futurepedia"],
        limit_per_source=2,
        concurrency=2,
        db_url=None,
        dry_run=True
    )
    assert res["total_discovered"] == 2
    assert res["total_extracted"] == 1
    assert res["total_new_domains"] == 1
    assert res["total_inserted"] == 0
