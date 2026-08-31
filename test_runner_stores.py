import asyncio
import inspect
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

import runner


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sigpik" / "d1" / "migrations"
TEST_SKIPPED_DATA_MIGRATIONS = {"0026_reject_catalog_fit_mismatches.sql"}


class TrafficProjectionMathTests(unittest.TestCase):
    def test_country_share_clamps_provider_floating_point_noise_at_one(self) -> None:
        self.assertEqual(runner.traffic_share_to_bps(1.0000000000000007), 10000)
        self.assertEqual(runner.estimate_visits_from_bps(12345, 10000), 12345)
        self.assertIsNone(runner.traffic_share_to_bps(1.00001))


class D1RequestObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_preserves_sanitized_response_and_operation(self) -> None:
        class D1Config:
            cloudflare_account_id = "account-id"
            cloudflare_d1_database_id = "database-id"
            cloudflare_api_token = "secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                headers={"cf-ray": "test-ray-SIN"},
                json={
                    "success": False,
                    "errors": [
                        {"code": 7500, "message": "no such table: domain_traffic_country_monthly"}
                    ],
                    "api_token": "should-not-leak",
                },
                request=request,
            )

        d1 = runner.D1Client(D1Config())
        await d1.client.aclose()
        d1.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr), self.assertRaises(runner.D1RequestError) as raised:
                await d1.batch(
                    [("INSERT INTO domain_traffic_country_monthly VALUES (?)", ["US"])],
                    operation="traffic.domain_country_monthly.upsert",
                )
        finally:
            await d1.close()

        message = str(raised.exception)
        self.assertIn("operation=traffic.domain_country_monthly.upsert", message)
        self.assertIn("no such table: domain_traffic_country_monthly", message)
        self.assertIn("request_id=test-ray-SIN", message)
        self.assertNotIn("should-not-leak", message)
        self.assertNotIn("secret-token", message)
        self.assertTrue(runner.is_missing_market_country_schema_error(raised.exception))

        event = json.loads(stderr.getvalue().strip())
        self.assertEqual(event["message"], "d1.request.failed")
        self.assertEqual(event["operation"], "traffic.domain_country_monthly.upsert")
        self.assertEqual(event["http_status"], 400)
        self.assertEqual(event["request_id"], "test-ray-SIN")
        self.assertIn("<redacted>", event["response_body"])
        self.assertEqual(event["statement_count"], 1)
        self.assertEqual(event["sql_verbs"], ["INSERT"])

    async def test_success_false_response_is_not_silently_reduced(self) -> None:
        class D1Config:
            cloudflare_account_id = "account-id"
            cloudflare_d1_database_id = "database-id"
            cloudflare_api_token = "secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": False, "errors": [{"message": "D1 rejected query"}]},
                request=request,
            )

        d1 = runner.D1Client(D1Config())
        await d1.client.aclose()
        d1.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with redirect_stderr(io.StringIO()), self.assertRaises(runner.D1RequestError) as raised:
                await d1.run("UPDATE traffic_tasks SET status = ?", ["failed"])
        finally:
            await d1.close()

        self.assertEqual(raised.exception.reason, "api_unsuccessful")
        self.assertIn("D1 rejected query", str(raised.exception))


class FakeD1:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        before = self.connection.total_changes
        cursor = self.connection.execute(sql, params or [])
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self.connection.commit()
        return {
            "results": rows,
            "meta": {
                "changes": self.connection.total_changes - before,
                "last_row_id": cursor.lastrowid,
            },
        }

    async def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.execute(sql, params, operation=operation)
        return result["results"]

    async def run(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        result = await self.execute(sql, params, operation=operation)
        return result["meta"]

    async def batch(
        self,
        statements: list[tuple[str, list[Any]]],
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        self.connection.execute("BEGIN")
        try:
            for sql, params in statements:
                before = self.connection.total_changes
                cursor = self.connection.execute(sql, params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(
                    {
                        "results": rows,
                        "meta": {
                            "changes": self.connection.total_changes - before,
                            "last_row_id": cursor.lastrowid,
                        },
                    }
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results

    async def insert_snapshot(
        self,
        domain: str,
        task_month: str,
        status: str,
        row: dict[str, Any],
        error: str | None,
        raw_payload: dict[str, Any] | None = None,
    ) -> int | None:
        return await runner.D1Client.insert_snapshot(self, domain, task_month, status, row, error, raw_payload)

    async def upsert_domain_traffic_monthly(
        self,
        domain: str,
        rows: list[dict[str, Any]],
        *,
        captured_at: str | None = None,
    ) -> None:
        await runner.D1Client.upsert_domain_traffic_monthly(
            self,
            domain,
            rows,
            captured_at=captured_at,
        )

    async def upsert_domain_traffic_country_monthly(
        self,
        domain: str,
        rows: list[dict[str, Any]],
        *,
        captured_at: str,
    ) -> None:
        await runner.D1Client.upsert_domain_traffic_country_monthly(
            self,
            domain,
            rows,
            captured_at=captured_at,
        )

    async def upsert_tool_traffic_monthly(self, domain: str, rows: list[dict[str, Any]]) -> None:
        await runner.D1Client.upsert_tool_traffic_monthly(self, domain, rows)

    async def insert_result(self, task: runner.TrafficTask, result: runner.FetchResult) -> None:
        await runner.D1Client.insert_result(self, task, result)


class PageAndNameQualityTests(unittest.TestCase):
    def test_cloudflare_challenge_is_not_a_valid_product_page(self) -> None:
        assessment = runner.classify_page_state(
            "<html><head><title>Just a moment...</title></head>"
            "<body><script>window._cf_chl_opt = {}</script>Checking your browser</body></html>",
            http_status=200,
        )
        self.assertEqual(assessment.state, "anti_bot")

    def test_neutral_example_transport_is_not_a_valid_product_page(self) -> None:
        assessment = runner.classify_page_state(
            "<html><head><title>Example Domain</title></head>"
            "<body>This domain is for use in illustrative examples in documents.</body></html>",
            http_status=200,
        )
        self.assertEqual(assessment.state, "unrelated_page")
        self.assertEqual(
            assessment.reason,
            "anti_bot_signature:neutral_transport:neutral_transport_example_domain",
        )
        self.assertFalse(assessment.is_valid)

    def test_parked_domain_is_not_a_valid_product_page(self) -> None:
        assessment = runner.classify_page_state(
            "<html><head><title>Buy this domain</title></head>"
            "<body>This domain is for sale. Make an offer on this domain today.</body></html>",
            http_status=200,
        )
        self.assertEqual(assessment.state, "parked_domain")
        self.assertFalse(assessment.is_valid)

    def test_access_denied_and_home_are_invalid_names_but_home_assistant_is_valid(self) -> None:
        self.assertEqual(runner.invalid_tool_name_reason("Access Denied"), "access_denied_name")
        self.assertEqual(runner.invalid_tool_name_reason("Home"), "generic_page_name")
        self.assertEqual(runner.invalid_tool_name_reason("Home Assistant"), "")

    def test_json_ld_product_name_wins_over_marketing_page_title(self) -> None:
        html_body = """
        <html><head>
          <title>Free AI Video Generator for Everyone | SeaArt AI</title>
          <script type="application/ld+json">
            {"@type":"SoftwareApplication","name":"SeaArt AI"}
          </script>
        </head><body>Create images and videos with AI.</body></html>
        """
        result = runner.resolve_tool_name(html_body, "seaart.ai")
        self.assertEqual(result.product_name, "SeaArt AI")
        self.assertEqual(result.source, "json_ld_product")
        self.assertEqual(result.review_status, "auto_approved")

    def test_domain_matching_title_segment_is_selected(self) -> None:
        result = runner.resolve_tool_name(
            "<html><head><title>Home - Tractable</title></head><body>AI claims platform</body></html>",
            "tractable.ai",
        )
        self.assertEqual(result.product_name, "Tractable")
        self.assertEqual(result.review_status, "auto_approved")

    def test_title_like_markup_inside_html_comments_is_ignored(self) -> None:
        html_body = """
        <html><head>
          <!-- SEO: per-page <title>, <meta>, JSON-LD and hreflang are emitted by useSeo(). -->
          <title>SYNTX AI: 100+ AI Models in One Place | Telegram &amp; Web</title>
        </head><body>Generate text, images, video and music.</body></html>
        """
        result = runner.resolve_tool_name(
            html_body,
            "syntx.ai",
            model_page_title=(
                ", <meta>, JSON-LD and hreflang are emitted by useSeo(). -->"
                "<title>SYNTX AI: 100+ AI Models in One Place | Telegram & Web"
            ),
        )
        self.assertEqual(runner.read_html_title(html_body), "SYNTX AI: 100+ AI Models in One Place | Telegram & Web")
        self.assertEqual(result.product_name, "SYNTX AI")
        self.assertEqual(result.review_status, "auto_approved")

    def test_html_markup_fragment_is_an_invalid_tool_name(self) -> None:
        self.assertEqual(
            runner.invalid_tool_name_reason(", <meta>, JSON-LD -->"),
            "html_markup_name",
        )

    def test_unproven_name_falls_back_to_domain_and_needs_review(self) -> None:
        result = runner.resolve_tool_name(
            "<html><head><title>Free AI Image Generator Online</title></head>"
            "<body>Create images from text.</body></html>",
            "example-tool.ai",
        )
        self.assertEqual(result.product_name, "Example Tool")
        self.assertEqual(result.review_status, "needs_review")

    def test_deterministic_description_and_features_use_official_page_content(self) -> None:
        task = runner.AssetTask(
            tool_id=1,
            canonical_slug="example-ai",
            normalized_domain="example.ai",
            official_url="https://example.ai/",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test",
        )
        html_body = (
            "<html><body><h2>Automated research</h2>"
            "<p>Example AI researches sources and summarizes findings for teams.</p></body></html>"
        )
        description = runner.deterministic_fallback_description(task, html_body, "Example AI")
        features = runner.deterministic_fallback_key_features("Example AI", description, html_body)
        self.assertIn("researches sources", description)
        self.assertEqual(features[0]["name"], "Automated research")

    def test_explicit_adult_generator_is_nsfw(self) -> None:
        result = runner.assess_content_safety(
            "<html><body>Generate NSFW AI porn images and videos.</body></html>",
            "createporn.com",
            product_name="AI Porn Generator",
            model_label="nsfw",
            model_confidence=99,
        )
        self.assertEqual(result.status, "nsfw")
        self.assertGreaterEqual(result.risk_score, 95)

    def test_ambiguous_companion_product_requires_review(self) -> None:
        result = runner.assess_content_safety(
            "<html><body>Uncensored AI girlfriend roleplay chat.</body></html>",
            "companion.example",
            model_label="uncertain",
            model_confidence=75,
        )
        self.assertEqual(result.status, "needs_review")

    def test_explicit_domain_model_is_safe_not_nsfw(self) -> None:
        result = runner.assess_content_safety(
            "<html><body>Capture your business as an explicit domain model and generate backend code.</body></html>",
            "modelarch.io",
            model_label="safe",
            model_confidence=94,
        )
        self.assertEqual(result.status, "safe")


class HomepageFetchStrategyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def task() -> runner.AssetTask:
        return runner.AssetTask(
            tool_id=42,
            canonical_slug="example",
            normalized_domain="example.com",
            official_url="https://example.com/",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test",
        )

    @staticmethod
    def client() -> runner.CloudflareBrowserRunAssetClient:
        client = object.__new__(runner.CloudflareBrowserRunAssetClient)
        client.endpoint_base = "https://api.cloudflare.com/client/v4/accounts/test/browser-rendering"
        client.headers = {"Authorization": "Bearer test", "Content-Type": "application/json"}
        client.timeout_seconds = 30
        return client

    @staticmethod
    def valid_html(label: str = "Example") -> str:
        return (
            f"<html><head><title>{label}</title></head><body><main>"
            "Example is an AI workspace for research, writing, analysis, and team collaboration. "
            "Customers can organize sources, generate reports, and share completed projects."
            "</main></body></html>"
        )

    @staticmethod
    def async_client_factory(transport: httpx.MockTransport):
        real_async_client = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        return factory

    async def test_static_fetch_follows_same_site_redirect_and_preserves_final_url(self) -> None:
        html_body = self.valid_html()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "example.com":
                return httpx.Response(
                    301,
                    headers={"location": "https://www.example.com/product"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=html_body,
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            runner.httpx,
            "AsyncClient",
            new=self.async_client_factory(transport),
        ):
            final_url, captured_html = await self.client().fetch_homepage_content(self.task())

        self.assertEqual(final_url, "https://www.example.com/product")
        self.assertEqual(captured_html, html_body)

    async def test_static_403_falls_back_to_browser_and_uses_browser_final_url(self) -> None:
        calls: list[str] = []
        html_body = self.valid_html("Example App")

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(403, text="Access denied", request=request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": html_body,
                    "meta": {
                        "finalUrl": "https://www.example.com/app",
                        "status": 200,
                        "title": "Example App",
                    },
                },
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            runner.httpx,
            "AsyncClient",
            new=self.async_client_factory(transport),
        ):
            final_url, captured_html = await self.client().fetch_homepage_content(self.task())

        self.assertEqual(calls, ["GET", "POST"])
        self.assertEqual(final_url, "https://www.example.com/app")
        self.assertEqual(captured_html, html_body)

    async def test_browser_6000_keeps_only_one_six_hour_retry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(403, text="Access denied", request=request)
            return httpx.Response(
                422,
                json={
                    "success": False,
                    "errors": [{"code": 6000, "message": "Unable to render the page"}],
                },
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            runner.httpx,
            "AsyncClient",
            new=self.async_client_factory(transport),
        ):
            with self.assertRaises(runner.AssetPipelineError) as raised:
                await self.client().fetch_homepage_content(self.task())

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.error_code, "browser_run_6000")
        self.assertEqual(raised.exception.max_attempts, 2)
        self.assertEqual(
            raised.exception.retry_after_seconds,
            runner.HOMEPAGE_BROWSER_6000_RETRY_DELAY_SECONDS,
        )

    async def test_browser_5006_and_6002_remain_retryable_as_transient(self) -> None:
        for error_code in (5006, 6002):
            with self.subTest(error_code=error_code):
                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        422,
                        json={
                            "success": False,
                            "errors": [
                                {"code": error_code, "message": "Temporary browser failure"}
                            ],
                        },
                        request=request,
                    )

                transport = httpx.MockTransport(handler)
                with patch.object(
                    runner.httpx,
                    "AsyncClient",
                    new=self.async_client_factory(transport),
                ):
                    with self.assertRaises(runner.AssetPipelineError) as raised:
                        await self.client().call_quick_action_envelope(
                            "content",
                            {"url": "https://example.com/"},
                        )

                self.assertTrue(raised.exception.retryable)
                self.assertEqual(
                    raised.exception.error_code,
                    f"browser_run_{error_code}",
                )
                self.assertIsNone(raised.exception.max_attempts)

    async def test_cross_site_redirect_is_terminal_and_skips_browser(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(
                302,
                headers={"location": "https://unrelated.example.net/"},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            runner.httpx,
            "AsyncClient",
            new=self.async_client_factory(transport),
        ):
            with self.assertRaises(runner.AssetPipelineError) as raised:
                await self.client().fetch_homepage_content(self.task())

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.error_code, "unrelated_homepage_redirect")
        self.assertEqual(calls, ["GET"])

    def test_candidate_urls_do_not_invent_an_http_downgrade(self) -> None:
        self.assertEqual(
            self.client().asset_candidate_urls(self.task()),
            ["https://example.com/"],
        )


class CategoryPromptHelperTests(unittest.TestCase):
    def test_homepage_main_content_excludes_navigation_footer_and_executable_markup(self) -> None:
        html_body = """
        <html><body>
          <nav>Docs Pricing Login</nav>
          <header>Brand shell</header>
          <main>
            <h1>Generate product videos</h1>
            <section><p>Turn a script into a narrated video.</p></section>
            <ul><li>Text to video</li><li>Voice generation</li></ul>
            <script>ignoreSecretInstruction()</script>
            <style>.hidden { display:none }</style>
          </main>
          <footer>Privacy Terms Careers</footer>
        </body></html>
        """

        text = runner.extract_homepage_main_text(html_body, limit=10000)

        self.assertIn("Generate product videos", text)
        self.assertIn("Turn a script into a narrated video.", text)
        self.assertIn("Text to video", text)
        self.assertNotIn("Docs Pricing Login", text)
        self.assertNotIn("Privacy Terms Careers", text)
        self.assertNotIn("ignoreSecretInstruction", text)
        self.assertNotIn("display:none", text)
        self.assertNotIn("Brand shell", text)

    def test_homepage_main_content_falls_back_to_cleaned_body(self) -> None:
        text = runner.extract_homepage_main_text(
            "<body><nav>Menu</nav><h1>AI spreadsheet assistant</h1>"
            "<p>Build formulas from plain language.</p><footer>Terms</footer></body>",
            limit=10000,
        )

        self.assertEqual(
            text,
            "AI spreadsheet assistant\nBuild formulas from plain language.",
        )

    def test_deterministic_category_fallback_prefers_matching_taxonomy_leaf(self) -> None:
        result = runner.deterministic_fallback_category(
            "AI writing assistant for blog content generation and editing",
            [
                runner.CategoryCatalogEntry(slug="writing-text"),
                runner.CategoryCatalogEntry(slug="content-generation", parent_slug="writing-text"),
                runner.CategoryCatalogEntry(slug="coding-development"),
            ],
        )
        self.assertEqual(result.category_l1, "writing-text")
        self.assertEqual(result.category_l2, "content-generation")
        self.assertIn("deterministic_fallback", result.category_raw_output)

    def test_normalize_structured_json_payload_unwraps_openai_chat_content(self) -> None:
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"category_l1":"coding-development","category_l2":"code-generation-understanding"}'
                    },
                }
            ]
        }
        normalized = runner.normalize_structured_json_payload(payload)
        self.assertEqual(normalized["category_l1"], "coding-development")
        self.assertEqual(normalized["category_l2"], "code-generation-understanding")

    def test_normalize_structured_json_payload_reads_reasoning_when_content_null(self) -> None:
        payload = {
            "content": None,
            "reasoning": (
                "After checking boundaries, category_l1 = \"marketing-seo\". "
                'Final object: {"category_l1":"marketing-seo"}'
            ),
        }
        normalized = runner.normalize_structured_json_payload(payload)
        self.assertEqual(normalized["category_l1"], "marketing-seo")

    def test_normalize_structured_json_payload_keeps_flat_schema_objects(self) -> None:
        payload = {"category_l1": "writing-text", "category_l2": "content-generation"}
        self.assertEqual(runner.normalize_structured_json_payload(payload), payload)

    def test_build_category_prompt_includes_definitions_when_present(self) -> None:
        entries = [
            runner.CategoryCatalogEntry(
                slug="code-assistant",
                parent_slug="developer-tools",
                definition="Helps write or review code",
                excludes="Low-code website builders",
                examples="Cursor",
            ),
            runner.CategoryCatalogEntry(slug="image-editing"),
        ]
        prompt = runner.build_category_classification_prompt(entries)
        self.assertIn("code-assistant", prompt)
        self.assertIn("def=Helps write or review code", prompt)
        self.assertIn("excludes=Low-code website builders", prompt)
        self.assertIn("image-editing", prompt)
        self.assertIn("exact slugs", prompt)

    def test_normalize_category_catalog_accepts_string_slugs(self) -> None:
        entries = runner.normalize_category_catalog(["Image Editing", "code-assistant", ""])
        self.assertEqual([entry.slug for entry in entries], ["image-editing", "code-assistant"])

    def test_l1_prompt_contains_only_supplied_top_level_boundaries(self) -> None:
        prompt = runner.build_category_l1_prompt([
            runner.CategoryCatalogEntry(
                slug="writing-text",
                definition="Written text outcomes",
                excludes="Marketing operations",
            ),
            runner.CategoryCatalogEntry(
                slug="coding-development",
                definition="Software development outcomes",
            ),
        ])

        self.assertIn("writing-text | def=Written text outcomes", prompt)
        self.assertIn("excludes=Marketing operations", prompt)
        self.assertIn("coding-development", prompt)
        self.assertNotIn("content-generation", prompt)

    def test_normalize_category_model_id_expands_deepseek_short_names(self) -> None:
        self.assertEqual(
            runner.normalize_category_model_id("deepseek-v4-flash"),
            "deepseek/deepseek-v4-flash",
        )
        self.assertEqual(
            runner.normalize_category_model_id("deepseek/deepseek-v4-flash"),
            "deepseek/deepseek-v4-flash",
        )
        self.assertEqual(
            runner.normalize_category_model_id("workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        )

    def test_build_browser_response_format_uses_json_object_for_deepseek(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"category_l1": {"type": "string"}},
            "required": ["category_l1"],
        }
        deepseek_format = runner.build_browser_response_format(
            schema,
            model="deepseek/deepseek-v4-flash",
            stage="category_l1",
        )
        self.assertEqual(deepseek_format, {"type": "json_object"})
        workers_format = runner.build_browser_response_format(
            schema,
            model="workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            stage="category_l1",
        )
        self.assertEqual(workers_format["type"], "json_schema")
        self.assertEqual(workers_format["json_schema"]["schema"], schema)
        self.assertEqual(workers_format["json_schema"]["name"], "category_l1")

    def test_category_model_auth_uses_openai_key_for_luna(self) -> None:
        config = SimpleNamespace(
            category_openai_api_key="openai-test-key",
            category_api_token="workers-ai-token",
        )

        self.assertEqual(
            runner.category_model_auth_token("openai/gpt-5.6-luna", config),
            "openai-test-key",
        )

    def test_category_custom_ai_excludes_workers_ai_even_when_configured(self) -> None:
        client = SimpleNamespace(
            category_model="deepseek/deepseek-v4-flash",
            category_fallback_model="workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            category_deepseek_api_key="deepseek-test-key",
            category_api_token="workers-ai-token",
            category_deepseek_max_output_tokens=1024,
        )

        configs = runner.CloudflareBrowserRunAssetClient.category_custom_ai(client)

        self.assertEqual(
            [item["model"] for item in configs],
            ["deepseek/deepseek-v4-flash"],
        )

    def test_augment_prompt_for_json_object_lists_required_keys(self) -> None:
        schema = {
            "type": "object",
            "properties": {"category_l1": {"type": "string"}, "category_l2": {"type": "string"}},
            "required": ["category_l1", "category_l2"],
        }
        prompt = runner.augment_prompt_for_response_format(
            "Classify the product.",
            schema,
            {"type": "json_object"},
        )
        self.assertIn("Required keys: category_l1, category_l2", prompt)
        self.assertIn("Classify the product.", prompt)


class AssetExtractionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_classification_is_hierarchical_and_uses_deepseek_v4_flash(self) -> None:
        class RecordingClient(runner.CloudflareBrowserRunAssetClient):
            def __init__(self) -> None:
                self.timeout_seconds = 30
                self.category_api_token = "workers-ai-token"
                self.category_deepseek_api_key = "deepseek-test-key"
                self.category_model = runner.DEFAULT_CATEGORY_CLASSIFICATION_MODEL
                self.category_fallback_model = runner.DEFAULT_CATEGORY_CLASSIFICATION_FALLBACK_MODEL
                self.calls: list[dict[str, Any]] = []

            async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
                self.assert_json_endpoint(endpoint)
                self.calls.append(body)
                response_format = body.get("response_format") or {}
                if response_format.get("type") == "json_object":
                    # DeepSeek path: schema keys are described in the prompt.
                    if "category_l2" in str(body.get("prompt") or "") and "Required keys: category_l2" in str(
                        body.get("prompt") or ""
                    ):
                        return {"category_l2": "content-generation"}
                    return {"category_l1": "writing-text"}
                schema = response_format.get("json_schema") or {}
                if isinstance(schema.get("schema"), dict):
                    properties = schema["schema"].get("properties") or {}
                else:
                    properties = schema.get("properties") or {}
                return (
                    {"category_l1": "writing-text"}
                    if "category_l1" in properties
                    else {"category_l2": "content-generation"}
                )

            @staticmethod
            def assert_json_endpoint(endpoint: str) -> None:
                if endpoint != "json":
                    raise AssertionError(endpoint)

        client = RecordingClient()
        task = runner.AssetTask(1, "example", "example.com", "https://example.com", 1, 5, 1, "lease")
        catalog = [
            runner.CategoryCatalogEntry(slug="writing-text", definition="Written text outcomes"),
            runner.CategoryCatalogEntry(slug="coding-development", definition="Software development"),
            runner.CategoryCatalogEntry(slug="content-generation", parent_slug="writing-text"),
            runner.CategoryCatalogEntry(slug="text-editing", parent_slug="writing-text"),
            runner.CategoryCatalogEntry(slug="code-generation-understanding", parent_slug="coding-development"),
        ]

        result = await client.fetch_homepage_categories(
            task,
            catalog,
            main_content="Generate and edit written content with an AI writing assistant.",
            source_url="https://example.com",
        )

        self.assertEqual((result.category_l1, result.category_l2), ("writing-text", "content-generation"))
        self.assertEqual(len(client.calls), 2)
        # Models are tried one-by-one; first successful attempt uses DeepSeek only.
        self.assertEqual(len(client.calls[0]["custom_ai"]), 1)
        self.assertEqual(
            client.calls[0]["custom_ai"][0]["model"],
            "deepseek/deepseek-v4-flash",
        )
        self.assertEqual(client.calls[0]["custom_ai"][0]["authorization"], "Bearer deepseek-test-key")
        self.assertEqual(client.calls[0]["custom_ai"][0]["thinking"], {"type": "disabled"})
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("url", client.calls[0])
        self.assertIn("Cleaned homepage content", client.calls[0]["html"])
        self.assertIn("CLEANED HOMEPAGE MAIN CONTENT", client.calls[0]["prompt"])
        self.assertIn("Required keys: category_l1", client.calls[0]["prompt"])
        self.assertIn("coding-development", client.calls[0]["prompt"])
        self.assertIn("content-generation", client.calls[1]["prompt"])
        self.assertNotIn("code-generation-understanding", client.calls[1]["prompt"])
        raw = json.loads(result.category_raw_output)
        self.assertEqual(raw["prompt_version"], runner.CATEGORY_CLASSIFICATION_PROMPT_VERSION)
        self.assertEqual(
            raw["model_chain"],
            ["deepseek/deepseek-v4-flash"],
        )

    async def test_category_l2_outside_selected_parent_is_rejected(self) -> None:
        class RecordingClient(runner.CloudflareBrowserRunAssetClient):
            def __init__(self) -> None:
                self.timeout_seconds = 30
                self.category_api_token = "token"
                self.category_deepseek_api_key = "deepseek-test-key"
                self.category_model = runner.DEFAULT_CATEGORY_CLASSIFICATION_MODEL
                self.category_fallback_model = ""
                self.call_count = 0

            async def call_quick_action(self, _endpoint: str, _body: dict[str, Any]) -> Any:
                self.call_count += 1
                return (
                    {"category_l1": "writing-text"}
                    if self.call_count == 1
                    else {"category_l2": "code-generation-understanding"}
                )

        task = runner.AssetTask(1, "example", "example.com", "https://example.com", 1, 5, 1, "lease")
        result = await RecordingClient().fetch_homepage_categories(
            task,
            [
                runner.CategoryCatalogEntry(slug="writing-text"),
                runner.CategoryCatalogEntry(slug="coding-development"),
                runner.CategoryCatalogEntry(slug="content-generation", parent_slug="writing-text"),
                runner.CategoryCatalogEntry(slug="code-generation-understanding", parent_slug="coding-development"),
            ],
            main_content="Write and edit long-form content.",
            source_url="https://example.com",
        )

        self.assertEqual(result.category_l1, "writing-text")
        self.assertEqual(result.category_l2, "")
        self.assertEqual(result.metadata_error, "category_l2_unmatched=code-generation-understanding")
        self.assertEqual(
            json.loads(result.category_raw_output)["l2_error"],
            "category_l2_unmatched=code-generation-understanding",
        )

    async def test_browser_run_json_requests_split_core_features_and_category_contracts(self) -> None:
        class RecordingClient(runner.CloudflareBrowserRunAssetClient):
            def __init__(self) -> None:
                self.timeout_seconds = 30
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
                self.calls.append((endpoint, body))
                schema = body["response_format"]["json_schema"]
                properties = schema.get("schema", schema)["properties"]
                if "description" in properties:
                    return {
                        "product_name": "Example",
                        "page_title": "Example - AI workspace",
                        "description": "Example description",
                        "favicon_href": "",
                        "name_confidence": 95,
                        "name_source": "homepage_brand",
                        "name_evidence": "Header brand",
                        "content_safety_label": "safe",
                        "content_safety_confidence": 96,
                        "content_safety_evidence": "No adult content signals",
                    }
                if "key_features" in properties:
                    return {"key_features": [
                        {"name": "Feature one", "description": "Description one"},
                    ]}
                return {
                    "category_l1": "image-processing",
                    "category_l2": "image-editing",
                }

        client = RecordingClient()
        task = runner.AssetTask(
            tool_id=1,
            canonical_slug="example",
            normalized_domain="example.com",
            official_url="https://example.com",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test-lease",
        )

        core = await client.fetch_homepage_core_metadata(task)
        features = await client.fetch_homepage_key_features(task)
        categories = await client.fetch_homepage_categories(
            task,
            ["image-processing", "image-editing"],
            main_content="Edit and transform images with AI.",
            source_url="https://example.com",
        )

        self.assertEqual(core.description, "Example description")
        self.assertEqual(len(features.key_features or []), 1)
        self.assertEqual(categories.category_l2, "image-editing")
        self.assertEqual(len(client.calls), 3)
        schemas = [
            body["response_format"]["json_schema"].get(
                "schema", body["response_format"]["json_schema"]
            )
            for endpoint, body in client.calls
            if endpoint == "json"
        ]
        self.assertEqual(
            [set(schema["properties"]) for schema in schemas],
            [
                {
                    "product_name",
                    "page_title",
                    "description",
                    "favicon_href",
                    "name_confidence",
                    "name_source",
                    "name_evidence",
                    "content_safety_label",
                    "content_safety_confidence",
                    "content_safety_evidence",
                },
                {"key_features"},
                {"category_l1", "category_l2"},
            ],
        )
        self.assertEqual(schemas[1]["properties"]["key_features"]["minItems"], 1)
        self.assertEqual(schemas[1]["properties"]["key_features"]["maxItems"], 6)

    async def test_browser_run_json_generation_400_remains_retryable(self) -> None:
        class FakeResponse:
            status_code = 400
            text = json.dumps(
                {
                    "success": False,
                    "errors": [{"message": "Unable to form JSON based on webpage text"}],
                }
            )

        class FakeAsyncClient:
            def __init__(self, **_: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def post(self, *_: Any, **__: Any) -> FakeResponse:
                return FakeResponse()

        client = runner.CloudflareBrowserRunAssetClient.__new__(runner.CloudflareBrowserRunAssetClient)
        client.endpoint_base = "https://api.example.test"
        client.headers = {}
        client.timeout_seconds = 30

        with patch.object(runner.httpx, "AsyncClient", FakeAsyncClient):
            with self.assertRaises(runner.AssetPipelineError) as raised:
                await client.call_quick_action("json", {"url": "https://example.com"})

        self.assertTrue(raised.exception.retryable)


class BrowserStructuredTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleaned_text_tries_each_model_once_without_url_navigation(self) -> None:
        class EmptyResultClient(runner.CloudflareBrowserRunAssetClient):
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
                self.assert_json_endpoint(endpoint)
                self.calls.append(body)
                return {}

            @staticmethod
            def assert_json_endpoint(endpoint: str) -> None:
                if endpoint != "json":
                    raise AssertionError(f"unexpected endpoint: {endpoint}")

        client = EmptyResultClient()
        models = [
            {
                "model": "deepseek/deepseek-v4-flash",
                "authorization": "Bearer deepseek-test-key",
            },
            {
                "model": "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "authorization": "Bearer workers-test-key",
            },
        ]

        with self.assertRaises(runner.AssetPipelineError):
            await client.fetch_structured_text_data(
                source_url="https://www.cursor.com/",
                stage="shadow_profile_main_content",
                prompt="CLEANED HOMEPAGE MAIN CONTENT:\nGenerate videos from text.",
                json_schema={
                    "type": "object",
                    "properties": {"primary_job": {"type": "string"}},
                    "required": ["primary_job"],
                },
                custom_ai=models,
            )

        self.assertEqual(
            [call["custom_ai"][0]["model"] for call in client.calls],
            [item["model"] for item in models],
        )
        self.assertTrue(all("url" not in call for call in client.calls))
        self.assertTrue(all("html" in call for call in client.calls))
        self.assertTrue(
            all("example.com" not in str(call.get("html") or "") for call in client.calls)
        )
        self.assertTrue(
            all(
                call.get("html", "").startswith(
                    '<main data-classification-transport="prompt-only">'
                )
                for call in client.calls
            )
        )


class BrowserStructuredPayloadValidationTests(unittest.TestCase):
    def test_empty_required_array_can_be_valid_when_explicitly_allowed(self) -> None:
        schema = {
            "type": "object",
            "properties": {"capability_slugs": {"type": "array"}},
            "required": ["capability_slugs"],
        }

        self.assertFalse(
            runner.CloudflareBrowserRunAssetClient.structured_payload_has_required_fields(
                {"capability_slugs": []}, schema
            )
        )
        self.assertTrue(
            runner.CloudflareBrowserRunAssetClient.structured_payload_has_required_fields(
                {"capability_slugs": []},
                schema,
                allow_empty_required_arrays=True,
            )
        )

    def test_allow_empty_array_still_requires_the_declared_key(self) -> None:
        schema = {
            "type": "object",
            "properties": {"capability_slugs": {"type": "array"}},
            "required": ["capability_slugs"],
        }

        self.assertFalse(
            runner.CloudflareBrowserRunAssetClient.structured_payload_has_required_fields(
                {},
                schema,
                allow_empty_required_arrays=True,
            )
        )

    def test_capability_stage_can_explicitly_treat_empty_object_as_no_matches(self) -> None:
        schema = {
            "type": "object",
            "properties": {"capability_slugs": {"type": "array"}},
            "required": ["capability_slugs"],
        }

        self.assertTrue(
            runner.CloudflareBrowserRunAssetClient.structured_payload_has_required_fields(
                {},
                schema,
                allow_empty_required_arrays=True,
                empty_object_means_empty_required_arrays=True,
            )
        )


class WorkerControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_taxonomy_worker_wires_auto_non_product_recheck_kill_switch(self) -> None:
        config = type(
            "TaxonomyConfig",
            (),
            {
                "taxonomy_auto_enabled": True,
                "taxonomy_recheck_auto_non_product": True,
                "taxonomy_capabilities_enabled": True,
                "taxonomy_capability_backfill_enabled": True,
                "taxonomy_capability_candidate_limit": 96,
                "taxonomy_limit": 50,
                "taxonomy_concurrency": 3,
                "taxonomy_auto_accept_confidence": 0.5,
            },
        )()
        classifier = AsyncMock(return_value={"selected": 0})

        class FakeD1Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def run_operation(_config, _d1, workload, operation):
            self.assertEqual(workload, "taxonomy")
            return await operation()

        with (
            patch.object(runner, "D1Client", return_value=FakeD1Context()),
            patch.object(runner, "run_with_telemetry", side_effect=run_operation),
            patch("taxonomy_shadow.run_shadow_taxonomy", new=classifier),
        ):
            await runner.run_taxonomy_once(config)

        classifier.assert_awaited_once()
        self.assertTrue(
            classifier.await_args.kwargs["include_auto_non_product_recheck"]
        )
        self.assertTrue(classifier.await_args.kwargs["include_capabilities"])
        self.assertTrue(
            classifier.await_args.kwargs["include_capability_backfill"]
        )
        self.assertEqual(
            classifier.await_args.kwargs["capability_candidate_limit"], 96
        )

    def test_taxonomy_full_batch_continues_without_poll_delay(self) -> None:
        config = type(
            "TaxonomyConfig",
            (),
            {
                "taxonomy_limit": 50,
                "taxonomy_interval_seconds": 300,
                "taxonomy_provider_backoff_seconds": 21600,
            },
        )()

        self.assertEqual(
            runner.taxonomy_next_delay_seconds(config, {"selected": 50}),
            0,
        )
        self.assertEqual(
            runner.taxonomy_next_delay_seconds(config, {"selected": 49}),
            300,
        )
        self.assertEqual(
            runner.taxonomy_next_delay_seconds(
                config,
                {"selected": 50, "provider_blocked": 1},
            ),
            21600,
        )
        self.assertEqual(
            runner.taxonomy_next_delay_seconds(
                config,
                {"selected": 50, "auto_non_product_recheck_selected": 50},
            ),
            300,
        )
        self.assertEqual(
            runner.taxonomy_next_delay_seconds(
                config,
                {"selected": 50, "capability_backfill_selected": 50},
            ),
            300,
        )
        self.assertEqual(
            runner.taxonomy_next_delay_seconds(
                config,
                {"selected": 50, "capability_only_selected": 50},
            ),
            300,
        )

    def test_taxonomy_idle_detection_ignores_scan_only_bookkeeping(self) -> None:
        self.assertFalse(
            runner.taxonomy_batch_has_activity(
                {
                    "selected": 0,
                    "failed": 0,
                    "anomaly_scanned": 500,
                    "anomaly_candidates": 0,
                }
            )
        )
        self.assertTrue(
            runner.taxonomy_batch_has_activity(
                {"selected": 0, "anomaly_candidates": 1}
            )
        )
        self.assertTrue(
            runner.taxonomy_batch_has_activity(
                {"selected": 0, "anomaly_scan_failed": 1}
            )
        )
        self.assertTrue(
            runner.taxonomy_batch_has_activity(
                {"selected": 0, "model_retries_resumed": 1}
            )
        )

    def test_taxonomy_idle_heartbeat_is_rate_limited(self) -> None:
        self.assertTrue(runner.taxonomy_idle_heartbeat_due(None, 100.0, 3600))
        self.assertFalse(runner.taxonomy_idle_heartbeat_due(100.0, 3699.0, 3600))
        self.assertTrue(runner.taxonomy_idle_heartbeat_due(100.0, 3700.0, 3600))

    async def test_disabled_pricing_never_opens_d1_or_provider_work(self) -> None:
        config = type("PricingConfig", (), {"pricing_monitor_enabled": False})()

        with patch.object(runner, "D1Client", side_effect=AssertionError("D1 must stay idle")):
            counts = await runner.run_pricing_once(config)

        self.assertEqual(counts, {"disabled": 1})


class DomainStateClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_ahrefs_request_pacer_evenly_spaces_request_starts(self) -> None:
        pacer = runner.AsyncRequestPacer(60)
        sleep = AsyncMock()
        with (
            patch.object(runner.time, "monotonic", side_effect=[100.0, 100.0]),
            patch.object(runner.asyncio, "sleep", sleep),
        ):
            await pacer.wait()
            await pacer.wait()

        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 1.0)

    async def test_ahrefs_domain_rating_request_uses_bearer_token(self) -> None:
        observed: dict[str, Any] = {}
        request_pacer = AsyncMock()

        class FakeResponse:
            status_code = 200
            is_success = True
            text = ""

            @staticmethod
            def json() -> dict[str, Any]:
                return {"domain_rating": {"domain_rating": 42}}

        class FakeAsyncClient:
            def __init__(self, **_: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def get(self, endpoint: Any, *, headers: dict[str, str]) -> FakeResponse:
                observed["endpoint"] = str(endpoint)
                observed["headers"] = headers
                return FakeResponse()

        with patch.object(runner.httpx, "AsyncClient", FakeAsyncClient):
            result = await runner.DomainStateClient(
                "test-ahrefs-token",
                request_pacer=request_pacer,
            ).fetch_ahrefs_domain_rating("example.com")

        self.assertEqual(result.status, "done")
        self.assertEqual(result.domain_rating, 42)
        self.assertIn("target=example.com", observed["endpoint"])
        self.assertEqual(observed["headers"]["Authorization"], "Bearer test-ahrefs-token")
        request_pacer.wait.assert_awaited_once()

    async def test_ahrefs_429_exposes_retry_after_for_task_backoff(self) -> None:
        class FakeResponse:
            status_code = 429
            is_success = False
            text = "rate limited"
            headers = {"retry-after": "12"}

        class FakeAsyncClient:
            def __init__(self, **_: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def get(self, *_: Any, **__: Any) -> FakeResponse:
                return FakeResponse()

        with patch.object(runner.httpx, "AsyncClient", FakeAsyncClient):
            result = await runner.DomainStateClient(
                "test-ahrefs-token",
                request_pacer=AsyncMock(),
            ).fetch_ahrefs_domain_rating("example.com")

        self.assertEqual(result.status, "failed")
        self.assertIn("429", result.error or "")
        self.assertEqual(result.retry_after_seconds, 12)

    async def test_ahrefs_domain_rating_requires_api_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "AHREF_API_KEY"):
            await runner.DomainStateClient("").fetch_ahrefs_domain_rating("example.com")

    async def test_monthly_dr_refresh_skips_previously_checked_rdap(self) -> None:
        client = runner.DomainStateClient("test-ahrefs-token")
        client.fetch_ahrefs_domain_rating = AsyncMock(
            return_value=runner.DomainStateResult("done", 51.0, None)
        )
        client.fetch_domain_created_at = AsyncMock(
            return_value=runner.DomainStateResult("done", None, "2020-01-02T00:00:00Z")
        )

        result = await client.fetch("example.com", fetch_domain_rating=True, fetch_rdap=False)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.domain_rating, 51.0)
        self.assertIsNone(result.rdap_status)
        client.fetch_ahrefs_domain_rating.assert_awaited_once_with("example.com")
        client.fetch_domain_created_at.assert_not_awaited()

    async def test_one_time_rdap_failure_is_a_completed_observation(self) -> None:
        client = runner.DomainStateClient("test-ahrefs-token")
        client.fetch_ahrefs_domain_rating = AsyncMock()
        client.fetch_domain_created_at = AsyncMock(
            return_value=runner.DomainStateResult("failed", None, None, "rdap_timeout")
        )

        result = await client.fetch("example.com", fetch_domain_rating=False, fetch_rdap=True)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.rdap_status, "failed")
        self.assertEqual(result.rdap_error, "rdap_timeout")
        client.fetch_ahrefs_domain_rating.assert_not_awaited()
        client.fetch_domain_created_at.assert_awaited_once_with("example.com")

    async def test_ahrefs_exception_does_not_discard_one_time_rdap_result(self) -> None:
        client = runner.DomainStateClient("test-ahrefs-token")
        client.fetch_ahrefs_domain_rating = AsyncMock(side_effect=RuntimeError("ahrefs_down"))
        client.fetch_domain_created_at = AsyncMock(
            return_value=runner.DomainStateResult("done", None, "2020-01-02T00:00:00Z")
        )

        result = await client.fetch("example.com")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "ahrefs_down")
        self.assertEqual(result.rdap_status, "done")
        self.assertEqual(result.domain_created_at, "2020-01-02T00:00:00Z")

    async def test_dr_retry_does_not_repeat_rdap_within_the_same_task(self) -> None:
        task = runner.DomainStateTask(
            normalized_domain="example.com",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="lease-token",
            fetch_domain_rating=True,
            fetch_rdap=True,
        )
        client = AsyncMock()
        client.fetch.side_effect = [
            runner.DomainStateResult(
                "failed",
                None,
                "2020-01-02T00:00:00Z",
                "ahrefs_down",
                rdap_status="done",
            ),
            runner.DomainStateResult("done", 55.0, None),
        ]
        store = AsyncMock()
        store.renew_lease.return_value = True
        store.complete_task.return_value = True

        with patch.object(runner.asyncio, "sleep", AsyncMock()):
            status = await runner.process_domain_state(task, client, store, max_retries=1)

        self.assertEqual(status, "done")
        self.assertEqual(client.fetch.await_count, 2)
        self.assertTrue(client.fetch.await_args_list[0].kwargs["fetch_rdap"])
        self.assertFalse(client.fetch.await_args_list[1].kwargs["fetch_rdap"])
        completed_result = store.complete_task.await_args.args[1]
        self.assertEqual(completed_result.rdap_status, "done")
        self.assertEqual(completed_result.domain_created_at, "2020-01-02T00:00:00Z")

    async def test_dr_retry_honors_provider_retry_after(self) -> None:
        task = runner.DomainStateTask(
            normalized_domain="rate-limited.example",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="lease-token",
            fetch_domain_rating=True,
            fetch_rdap=False,
        )
        client = AsyncMock()
        client.fetch.side_effect = [
            runner.DomainStateResult(
                "failed",
                None,
                None,
                "Ahrefs HTTP 429",
                retry_after_seconds=12,
            ),
            runner.DomainStateResult("done", 55.0, None),
        ]
        store = AsyncMock()
        store.renew_lease.return_value = True
        store.complete_task.return_value = True
        sleep = AsyncMock()

        with (
            patch.object(runner.random, "uniform", return_value=2.0),
            patch.object(runner.asyncio, "sleep", sleep),
        ):
            status = await runner.process_domain_state(task, client, store, max_retries=1)

        self.assertEqual(status, "done")
        sleep.assert_awaited_once_with(12.0)


class DomainStateMigrationContractTests(unittest.TestCase):
    def test_legacy_domain_crawls_are_marked_checked_without_another_rdap_request(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name in TEST_SKIPPED_DATA_MIGRATIONS or migration.name.startswith("0035_"):
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                """
                INSERT INTO domain_states (
                  normalized_domain, source, domain_rating, last_crawled_at, domain_created_at
                ) VALUES
                  ('known.example', 'ahrefs', 50, '2026-07-01T00:00:00Z', '2020-01-02T00:00:00Z'),
                  ('missing.example', 'ahrefs', 20, '2026-07-01T00:00:00Z', NULL)
                """
            )
            connection.executemany(
                "INSERT INTO domain_state_tasks (normalized_domain, source, status) VALUES (?, 'ahrefs', 'done')",
                [("known.example",), ("missing.example",)],
            )

            migration = next(MIGRATIONS_DIR.glob("0035_*.sql"))
            connection.executescript(migration.read_text(encoding="utf-8"))

            rows = {
                row["normalized_domain"]: row
                for row in connection.execute(
                    "SELECT normalized_domain, rdap_status, rdap_checked_at, rdap_last_error "
                    "FROM domain_states"
                ).fetchall()
            }
            self.assertEqual(rows["known.example"]["rdap_status"], "done")
            self.assertEqual(rows["missing.example"]["rdap_status"], "no_data")
            self.assertEqual(rows["missing.example"]["rdap_last_error"], "legacy_rdap_no_result")
            self.assertTrue(rows["known.example"]["rdap_checked_at"])
            self.assertTrue(rows["missing.example"]["rdap_checked_at"])
            fetch_flags = connection.execute(
                "SELECT sum(fetch_rdap) AS fetch_rdap FROM domain_state_tasks"
            ).fetchone()
            self.assertEqual(fetch_flags["fetch_rdap"], 0)
        finally:
            connection.close()


class RunnerStoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration.name in TEST_SKIPPED_DATA_MIGRATIONS:
                continue
            self.connection.executescript(migration.read_text(encoding="utf-8"))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.d1 = FakeD1(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def add_tool(self, suffix: str, *, status: str = "pending_enrich") -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO tools (
              canonical_slug, official_url, normalized_domain, status,
              content_safety_status, content_safety_score, content_safety_confidence
            )
            VALUES (?, ?, ?, ?, 'safe', 0, 100)
            """,
            [suffix, f"https://{suffix}.example", f"{suffix}.example", status],
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def seed_complete_enrichment(self, tool_id: int, suffix: str) -> None:
        category = self.connection.execute(
            "SELECT id FROM categories WHERE status = 'active' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(category)
        self.connection.execute(
            "UPDATE tools SET primary_category_id = ? WHERE id = ?",
            [category["id"], tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, short_description,
              feature_highlights, translation_status, published_at
            ) VALUES (?, 'en', ?, ?, 'Complete description', '["Feature one"]', 'published', ?)
            """,
            [tool_id, suffix, suffix, runner.utc_now_iso()],
        )
        self.connection.commit()

    def seed_publishable_tool(self, tool_id: int, suffix: str) -> None:
        self.seed_complete_enrichment(tool_id, suffix)
        now = runner.utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO tool_assets (
              tool_id, asset_kind, storage_bucket, storage_object_path, is_current
            ) VALUES (?, 'screenshot', 'sitesimgs', ?, 1)
            """,
            [tool_id, f"{suffix}/screenshot.png"],
        )
        self.connection.execute(
            """
            INSERT INTO tool_sources (
              tool_id, source_type, source_url, is_primary
            ) VALUES (?, 'official_site', ?, 1)
            """,
            [tool_id, f"https://{suffix}.example"],
        )
        self.connection.execute(
            """
            INSERT INTO tool_enrichment_states (
              tool_id, readiness, blocking_json, warnings_json, evaluated_at, updated_at
            ) VALUES (?, 'ready', '[]', '[]', ?, ?)
            """,
            [tool_id, now, now],
        )
        self.connection.commit()

    def task_row(self, table: str, where: str, params: list[Any]) -> sqlite3.Row:
        row = self.connection.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        self.assertIsNotNone(row)
        return row

    def assert_active_lease(self, row: sqlite3.Row, owner: str) -> None:
        self.assertEqual(row["lease_owner"], owner)
        self.assertTrue(row["lease_token"])
        self.assertTrue(row["lease_expires_at"])

    def assert_completed_lease(self, row: sqlite3.Row, status: str) -> None:
        self.assertEqual(row["status"], status)
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_token"])
        self.assertIsNone(row["lease_expires_at"])
        self.assertTrue(row["last_completed_at"])

    async def test_asset_queue_claim_lease_complete(self) -> None:
        tool_id = self.add_tool("asset-flow")
        store = runner.D1AssetStore(self.d1)

        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        tasks = await store.claim_due_tasks(10, "asset-worker")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.tool_id, tool_id)
        self.assertTrue(task.lease_token)
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "processing")
        self.assert_active_lease(row, "asset-worker")
        self.assertEqual(await store.claim_due_tasks(10, "other-worker"), [])

        await store.complete_task(task, "done")
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assert_completed_lease(row, "done")

    async def test_antibot_preflight_blocks_every_asset_write_and_schedules_retry(self) -> None:
        tool_id = self.add_tool("asset-antibot")
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        class AntiBotClient:
            async def preflight_homepage(self, _: runner.AssetTask) -> runner.PageQualityAssessment:
                return runner.PageQualityAssessment(
                    "anti_bot",
                    "challenge_title",
                    "Just a moment...",
                )

        status = await runner.process_asset_task(
            task,
            AntiBotClient(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            store,
            "https://img.example.test",
            2,
        )

        self.assertEqual(status, "failed")
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["page_state"], "anti_bot")
        self.assertIn("page_invalid:anti_bot", row["last_error"])
        self.assertIsNotNone(row["next_retry_at"])
        self.assertIsNone(row["dead_letter_at"])
        localization_count = self.connection.execute(
            "SELECT count(*) FROM tool_localizations WHERE tool_id = ?",
            [tool_id],
        ).fetchone()[0]
        asset_count = self.connection.execute(
            "SELECT count(*) FROM tool_assets WHERE tool_id = ?",
            [tool_id],
        ).fetchone()[0]
        self.assertEqual(localization_count, 0)
        self.assertEqual(asset_count, 0)

    async def test_nsfw_detection_blocks_asset_writes_and_requires_review(self) -> None:
        tool_id = self.add_tool("asset-nsfw")
        self.connection.execute(
            "UPDATE tools SET content_safety_status = 'unknown' WHERE id = ?",
            [tool_id],
        )
        self.connection.commit()
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        class NsfwClient:
            async def preflight_homepage(self, _: runner.AssetTask) -> runner.PageQualityAssessment:
                return runner.PageQualityAssessment("valid_product_page", "usable_html", "Adult generator")

            async def fetch_homepage_core_metadata(self, current: runner.AssetTask) -> runner.AssetFetchResult:
                return runner.AssetFetchResult(
                    final_url=current.official_url,
                    title="Explicit Generator",
                    description="Generate adult images",
                    content_safety_status="nsfw",
                    content_safety_score=99,
                    content_safety_confidence=98,
                    content_safety_reason="explicit_adult_product_signal",
                    content_safety_evidence=["strong:nsfw"],
                    content_safety_source="deterministic_rules",
                )

        uploader = AsyncMock()
        status = await runner.process_asset_task(
            task,
            NsfwClient(),  # type: ignore[arg-type]
            uploader,
            store,
            "https://img.example.test",
            0,
        )

        self.assertEqual(status, "failed")
        tool = self.connection.execute(
            "SELECT status, content_safety_status FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["status"], "pending_enrich")
        self.assertEqual(tool["content_safety_status"], "nsfw")
        event = self.connection.execute(
            "SELECT decision, reason FROM tool_content_safety_events WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(event["decision"], "nsfw")
        self.assertEqual(event["reason"], "explicit_adult_product_signal")
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM tool_assets WHERE tool_id = ?", [tool_id]).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM tool_localizations WHERE tool_id = ?", [tool_id]).fetchone()[0],
            0,
        )
        uploader.put_object.assert_not_awaited()

    async def test_asset_queue_ignores_taxonomy_only_gap(self) -> None:
        tool_id = self.add_tool("asset-category-gap")
        self.connection.executemany(
            """
            INSERT INTO tool_assets (tool_id, asset_kind, storage_bucket, storage_object_path, is_current)
            VALUES (?, ?, 'sitesimgs', ?, 1)
            """,
            [
                [tool_id, "screenshot", "asset-category-gap/screenshot.png"],
                [tool_id, "favicon", "asset-category-gap/favicon.png"],
            ],
        )
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, short_description,
              feature_highlights, translation_status, published_at
            ) VALUES (?, 'en', 'asset-category-gap', 'Category Gap', 'Complete description',
                      '["Feature one"]', 'published', ?)
            """,
            [tool_id, runner.utc_now_iso()],
        )
        self.connection.commit()

        store = runner.D1AssetStore(self.d1)
        queued = await store.queue_missing_asset_tasks(10)

        self.assertEqual(queued, 0)
        self.assertEqual(await store.missing_asset_requirements(tool_id), [])
        row = self.connection.execute(
            "SELECT * FROM asset_tasks WHERE tool_id = ? AND source = ?",
            [tool_id, runner.ASSET_SOURCE],
        ).fetchone()
        self.assertIsNone(row)

    async def test_asset_processing_retries_only_requirements_that_remain_missing(self) -> None:
        tool_id = self.add_tool("asset-metadata-only")
        self.connection.executemany(
            """
            INSERT INTO tool_assets (tool_id, asset_kind, storage_bucket, storage_object_path, is_current)
            VALUES (?, ?, 'sitesimgs', ?, 1)
            """,
            [
                [tool_id, "screenshot", "asset-metadata-only/screenshot.png"],
                [tool_id, "favicon", "asset-metadata-only/favicon.png"],
            ],
        )
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, short_description,
              feature_highlights, translation_status, published_at
            ) VALUES (?, 'en', 'asset-metadata-only', 'Metadata Only', 'Existing description',
                      '[]', 'published', ?)
            """,
            [tool_id, runner.utc_now_iso()],
        )
        category = self.connection.execute(
            """
            SELECT child.canonical_slug AS child_slug, parent.canonical_slug AS parent_slug
            FROM categories child
            JOIN categories parent ON parent.id = child.parent_category_id
            WHERE child.status = 'active' AND parent.status = 'active'
            ORDER BY child.id
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(category)
        self.connection.commit()

        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        class StageRecordingClient:
            def __init__(self) -> None:
                self.feature_calls = 0
                self.category_calls = 0

            async def fetch_homepage_core_metadata(self, _: runner.AssetTask) -> runner.AssetFetchResult:
                raise AssertionError("core metadata must not be fetched")

            async def capture_homepage_screenshot(self, _: runner.AssetTask) -> runner.AssetFetchResult:
                raise AssertionError("screenshot must not be fetched")

            async def fetch_homepage_key_features(self, current_task: runner.AssetTask) -> runner.AssetFetchResult:
                self.feature_calls += 1
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    key_features=[{"name": "Feature one", "description": "Does one thing"}],
                )

            async def fetch_homepage_categories(
                self,
                current_task: runner.AssetTask,
                _: list[str],
            ) -> runner.AssetFetchResult:
                self.category_calls += 1
                if self.category_calls == 1:
                    raise runner.AssetPipelineError("temporary category failure", retryable=True)
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    category_l1=category["parent_slug"],
                    category_l2=category["child_slug"],
                )

        class RecordingUploader:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def put_object(self, key: str, *_: Any) -> None:
                self.calls.append(key)

        browser_client = StageRecordingClient()
        uploader = RecordingUploader()
        with patch.object(runner.asyncio, "sleep", new=AsyncMock()):
            status = await runner.process_asset_task(
                task,
                browser_client,
                uploader,
                store,
                "https://img.example.test",
                1,
            )

        self.assertEqual(status, "done")
        self.assertEqual(browser_client.feature_calls, 1)
        self.assertEqual(browser_client.category_calls, 0)
        self.assertEqual(uploader.calls, [])
        self.assertEqual(await store.missing_asset_requirements(tool_id), [])
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assert_completed_lease(row, "done")

    async def test_asset_fallbacks_advance_a_qualified_tool_to_pending_review(self) -> None:
        tool_id = self.add_tool("auto-publish-ready")
        now = runner.utc_now_iso()
        category_id = self.connection.execute(
            "SELECT id FROM categories WHERE status = 'active' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        self.connection.execute(
            "INSERT INTO tool_categories (tool_id, category_id, source) VALUES (?, ?, 'auto')",
            [tool_id, category_id],
        )
        self.connection.execute(
            "UPDATE tools SET primary_category_id = ? WHERE id = ?",
            [category_id, tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_sources (
              tool_id, source_type, source_label, source_url, first_seen_at, last_seen_at
            ) VALUES (?, 'official_site', 'Discovery official website',
                      'https://auto-publish-ready.example/', ?, ?)
            """,
            [tool_id, now, now],
        )
        self.connection.commit()
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        class EmptyModelClient:
            async def fetch_homepage_core_metadata(self, current_task: runner.AssetTask) -> runner.AssetFetchResult:
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    html=(
                        "<html><head><title>Auto Publish Ready</title></head>"
                        "<body><h2>Team automation</h2></body></html>"
                    ),
                    title="Auto Publish Ready",
                    description="An AI assistant that automates team research workflows.",
                    name_source="page_title_segment",
                    name_confidence=72,
                    name_review_status="needs_review",
                    content_safety_status="safe",
                    content_safety_confidence=95,
                    content_safety_reason="no_explicit_content_signals",
                )

            async def fetch_homepage_key_features(self, current_task: runner.AssetTask) -> runner.AssetFetchResult:
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    key_features=[],
                    metadata_error="features_empty",
                )

            async def fetch_homepage_categories(
                self,
                current_task: runner.AssetTask,
                _: list[str],
            ) -> runner.AssetFetchResult:
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    metadata_error="category_empty",
                )

            async def capture_homepage_screenshot(self, current_task: runner.AssetTask) -> runner.AssetFetchResult:
                return runner.AssetFetchResult(final_url=current_task.official_url, screenshot=b"png")

        class RecordingUploader:
            async def put_object(self, *_: Any) -> None:
                return None

        favicon = runner.FaviconAsset(body=b"icon", key="auto-publish-ready/favicon.ico", mime_type="image/x-icon")
        with patch.object(runner, "fetch_favicon_asset", new=AsyncMock(return_value=favicon)):
            status = await runner.process_asset_task(
                task,
                EmptyModelClient(),
                RecordingUploader(),
                store,
                "https://img.example.test",
                0,
            )

        self.assertEqual(status, "done")
        readiness = await runner.D1EnrichmentStore(self.d1).evaluate_tool(tool_id)
        self.assertEqual(readiness, "ready")
        tool = self.connection.execute("SELECT status FROM tools WHERE id = ?", [tool_id]).fetchone()
        self.assertEqual(tool["status"], "pending_review")

    async def test_successful_screenshot_is_not_recaptured_while_favicon_retries(self) -> None:
        tool_id = self.add_tool("asset-stage-once")
        self.seed_complete_enrichment(tool_id, "asset-stage-once")
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        class CaptureOnceClient:
            def __init__(self) -> None:
                self.screenshot_calls = 0

            async def capture_homepage_screenshot(
                self,
                current_task: runner.AssetTask,
            ) -> runner.AssetFetchResult:
                self.screenshot_calls += 1
                return runner.AssetFetchResult(
                    final_url=current_task.official_url,
                    screenshot=b"screenshot",
                )

        browser_client = CaptureOnceClient()
        uploader = AsyncMock()
        favicon_fetch = AsyncMock(
            side_effect=[
                None,
                runner.FaviconAsset(b"favicon", "asset-stage-once/favicon.ico", "image/x-icon"),
            ]
        )
        with (
            patch.object(runner, "fetch_favicon_asset", new=favicon_fetch),
            patch.object(runner.asyncio, "sleep", new=AsyncMock()),
        ):
            status = await runner.process_asset_task(
                task,
                browser_client,  # type: ignore[arg-type]
                uploader,
                store,
                "https://img.example.test",
                1,
            )

        self.assertEqual(status, "done")
        self.assertEqual(browser_client.screenshot_calls, 1)
        self.assertEqual(favicon_fetch.await_count, 2)
        self.assertEqual(uploader.put_object.await_count, 2)
        self.assertEqual(await store.missing_asset_requirements(tool_id), [])

    async def test_missing_favicon_stays_in_bounded_failure_state(self) -> None:
        tool_id = self.add_tool("asset-favicon-failure")
        self.seed_complete_enrichment(tool_id, "asset-favicon-failure")
        self.connection.execute(
            """
            INSERT INTO tool_assets (
              tool_id, asset_kind, storage_bucket, storage_object_path, is_current
            ) VALUES (?, 'screenshot', 'sitesimgs', 'asset-favicon-failure/screenshot.png', 1)
            """,
            [tool_id],
        )
        self.connection.commit()

        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]
        uploader = AsyncMock()
        with patch.object(runner, "fetch_favicon_asset", new=AsyncMock(return_value=None)):
            status = await runner.process_asset_task(
                task,
                object(),  # type: ignore[arg-type]
                uploader,
                store,
                "https://img.example.test",
                0,
            )

        self.assertEqual(status, "failed")
        self.assertEqual(await store.missing_asset_requirements(tool_id), ["favicon"])
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["next_retry_at"])
        self.assertIsNone(row["dead_letter_at"])
        uploader.put_object.assert_not_awaited()

    async def test_asset_category_materialization_writes_parent_and_child(self) -> None:
        tool_id = self.add_tool("asset-category-materialization")
        category = self.connection.execute(
            """
            SELECT child.id AS child_id, child.canonical_slug AS child_slug,
                   parent.id AS parent_id, parent.canonical_slug AS parent_slug
            FROM categories child
            JOIN categories parent ON parent.id = child.parent_category_id
            WHERE child.status = 'active' AND parent.status = 'active'
            ORDER BY child.id
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(category)
        task = runner.AssetTask(
            tool_id=tool_id,
            canonical_slug="asset-category-materialization",
            normalized_domain="asset-category-materialization.example",
            official_url="https://asset-category-materialization.example",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test-lease",
        )
        result = runner.AssetFetchResult(
            final_url=task.official_url,
            screenshot=b"",
            category_l1=category["parent_slug"],
            category_l2=category["child_slug"],
        )

        await runner.D1AssetStore(self.d1).save_tool_categories(task, result)

        assigned_ids = {
            row["category_id"]
            for row in self.connection.execute(
                "SELECT category_id FROM tool_categories WHERE tool_id = ?",
                [tool_id],
            ).fetchall()
        }
        self.assertEqual(assigned_ids, {category["parent_id"], category["child_id"]})
        tool = self.connection.execute(
            "SELECT primary_category_id FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["primary_category_id"], category["parent_id"])

    def _seed_legacy_categories(self, tool_id: int, parent_id: int, child_id: int | None = None) -> None:
        self.connection.execute(
            "UPDATE tools SET primary_category_id = ? WHERE id = ?",
            [parent_id, tool_id],
        )
        self.connection.execute(
            "INSERT INTO tool_categories (tool_id, category_id, source) VALUES (?, ?, 'auto')",
            [tool_id, parent_id],
        )
        if child_id is not None:
            self.connection.execute(
                "INSERT INTO tool_categories (tool_id, category_id, source) VALUES (?, ?, 'auto')",
                [tool_id, child_id],
            )
        self.connection.commit()

    def _active_parent_child(self) -> sqlite3.Row:
        category = self.connection.execute(
            """
            SELECT child.id AS child_id, child.canonical_slug AS child_slug,
                   parent.id AS parent_id, parent.canonical_slug AS parent_slug
            FROM categories child
            JOIN categories parent ON parent.id = child.parent_category_id
            WHERE child.status = 'active' AND parent.status = 'active'
            ORDER BY child.id
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(category)
        return category

    def _second_parent_child(self, exclude_parent_id: int) -> sqlite3.Row:
        category = self.connection.execute(
            """
            SELECT child.id AS child_id, child.canonical_slug AS child_slug,
                   parent.id AS parent_id, parent.canonical_slug AS parent_slug
            FROM categories child
            JOIN categories parent ON parent.id = child.parent_category_id
            WHERE child.status = 'active'
              AND parent.status = 'active'
              AND parent.id <> ?
            ORDER BY child.id
            LIMIT 1
            """,
            [exclude_parent_id],
        ).fetchone()
        self.assertIsNotNone(category)
        return category

    async def test_published_legacy_category_candidate_filters(self) -> None:
        category = self._active_parent_child()
        published_id = self.add_tool("pub-legacy", status="published")
        rejected_id = self.add_tool("rej-legacy", status="rejected")
        manual_id = self.add_tool("pub-manual", status="published")
        done_id = self.add_tool("pub-done", status="published")
        pending_id = self.add_tool("pend-legacy", status="pending_enrich")

        self._seed_legacy_categories(published_id, category["parent_id"], category["child_id"])
        self._seed_legacy_categories(rejected_id, category["parent_id"], category["child_id"])
        self._seed_legacy_categories(manual_id, category["parent_id"], category["child_id"])
        self.connection.execute(
            "UPDATE tool_categories SET source = 'manual' WHERE tool_id = ?",
            [manual_id],
        )
        self._seed_legacy_categories(done_id, category["parent_id"], category["child_id"])
        raw_done = json.dumps(
            {
                "backfill": runner.PUBLISHED_CATEGORY_BACKFILL_VERSION,
                "prompt_version": runner.CATEGORY_CLASSIFICATION_PROMPT_VERSION,
                "mode": "hierarchical",
            }
        )
        self.connection.execute(
            """
            UPDATE tools
            SET category_classification_status = 'auto_ok',
                category_classification_raw = ?
            WHERE id = ?
            """,
            [raw_done, done_id],
        )
        self._seed_legacy_categories(pending_id, category["parent_id"], category["child_id"])
        self.connection.commit()

        store = runner.D1AssetStore(self.d1)
        tasks = await store.published_legacy_category_tasks(50)
        tool_ids = {task.tool_id for task in tasks}
        self.assertIn(published_id, tool_ids)
        self.assertNotIn(rejected_id, tool_ids)
        self.assertNotIn(manual_id, tool_ids)
        self.assertNotIn(done_id, tool_ids)
        self.assertNotIn(pending_id, tool_ids)

    async def test_published_category_backfill_replaces_atomically_and_is_resumable(self) -> None:
        old = self._active_parent_child()
        new = self._second_parent_child(old["parent_id"])
        tool_id = self.add_tool("pub-backfill-apply", status="published")
        self._seed_legacy_categories(tool_id, old["parent_id"], old["child_id"])

        task = runner.AssetTask(
            tool_id=tool_id,
            canonical_slug="pub-backfill-apply",
            normalized_domain="pub-backfill-apply.example",
            official_url="https://pub-backfill-apply.example",
            attempts=0,
            max_attempts=1,
            generation=0,
            lease_token="published-category-backfill",
        )
        result = runner.AssetFetchResult(
            final_url=task.official_url,
            category_l1=new["parent_slug"],
            category_l2=new["child_slug"],
            category_raw_output=json.dumps(
                {
                    "prompt_version": runner.CATEGORY_CLASSIFICATION_PROMPT_VERSION,
                    "mode": "hierarchical",
                    "taxonomy_version": "test-tax",
                    "model_chain": [runner.DEFAULT_CATEGORY_CLASSIFICATION_MODEL],
                }
            ),
        )

        store = runner.D1AssetStore(self.d1)
        dry = await store.apply_published_category_backfill(task, result, dry_run=True)
        self.assertFalse(dry["applied"])
        still_old = self.connection.execute(
            "SELECT primary_category_id FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(still_old["primary_category_id"], old["parent_id"])

        summary = await store.apply_published_category_backfill(task, result, dry_run=False)
        self.assertTrue(summary["applied"])

        tool = self.connection.execute(
            """
            SELECT primary_category_id, category_classification_status, category_classification_raw
            FROM tools WHERE id = ?
            """,
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["primary_category_id"], new["parent_id"])
        self.assertEqual(tool["category_classification_status"], "auto_ok")
        self.assertTrue(runner.raw_has_published_category_backfill(tool["category_classification_raw"]))

        assigned = {
            row["category_id"]
            for row in self.connection.execute(
                "SELECT category_id FROM tool_categories WHERE tool_id = ?",
                [tool_id],
            ).fetchall()
        }
        self.assertEqual(assigned, {new["parent_id"], new["child_id"]})

        change = self.connection.execute(
            "SELECT change_type, old_value, new_value FROM tool_change_log WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(change["change_type"], "category_backfill")
        self.assertIn(str(old["parent_id"]), change["old_value"])
        self.assertIn(new["parent_slug"], change["new_value"])

        event = self.connection.execute(
            "SELECT outcome, category_l1_slug, category_l2_slug FROM tool_category_classification_events WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(event["outcome"], "auto_ok")
        self.assertEqual(event["category_l1_slug"], new["parent_slug"])
        self.assertEqual(event["category_l2_slug"], new["child_slug"])

        remaining = await store.published_legacy_category_tasks(50)
        self.assertNotIn(tool_id, {item.tool_id for item in remaining})

    async def test_published_category_backfill_failure_keeps_live_categories(self) -> None:
        category = self._active_parent_child()
        tool_id = self.add_tool("pub-backfill-fail", status="published")
        self._seed_legacy_categories(tool_id, category["parent_id"], category["child_id"])
        task = runner.AssetTask(
            tool_id=tool_id,
            canonical_slug="pub-backfill-fail",
            normalized_domain="pub-backfill-fail.example",
            official_url="https://pub-backfill-fail.example",
            attempts=0,
            max_attempts=1,
            generation=0,
            lease_token="published-category-backfill",
        )
        store = runner.D1AssetStore(self.d1)
        await store.record_published_category_backfill_failure(
            task,
            error="category_l1_empty",
            raw_output=json.dumps({"error": "category_l1_empty"}),
        )

        tool = self.connection.execute(
            "SELECT primary_category_id, category_classification_last_error FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["primary_category_id"], category["parent_id"])
        self.assertEqual(tool["category_classification_last_error"], "category_l1_empty")
        assigned = {
            row["category_id"]
            for row in self.connection.execute(
                "SELECT category_id FROM tool_categories WHERE tool_id = ?",
                [tool_id],
            ).fetchall()
        }
        self.assertEqual(assigned, {category["parent_id"], category["child_id"]})
        outcomes = [
            row["outcome"]
            for row in self.connection.execute(
                "SELECT outcome FROM tool_category_classification_events WHERE tool_id = ?",
                [tool_id],
            ).fetchall()
        ]
        self.assertEqual(outcomes, ["auto_failed"])

    def test_published_category_backfill_success_helper(self) -> None:
        ok = runner.AssetFetchResult(final_url="https://x", category_l1="writing-text", category_l2="")
        self.assertTrue(runner.published_category_backfill_success(ok))
        bad_l1 = runner.AssetFetchResult(final_url="https://x", category_l1="", metadata_error="category_l1_empty")
        self.assertFalse(runner.published_category_backfill_success(bad_l1))
        bad_l2 = runner.AssetFetchResult(
            final_url="https://x",
            category_l1="writing-text",
            metadata_error="category_l2_unmatched=not-a-child",
        )
        self.assertFalse(runner.published_category_backfill_success(bad_l2))

    async def test_category_failures_remain_eligible_for_automatic_recovery(self) -> None:
        tool_id = self.add_tool("asset-category-auto-recovery")
        store = runner.D1AssetStore(self.d1)

        for attempt in range(runner.CATEGORY_CLASSIFICATION_MAX_ATTEMPTS):
            state = await store.record_category_classification_failure(
                tool_id,
                f"classification failed {attempt + 1}",
                raw_output=json.dumps({"attempt": attempt + 1}),
            )

        self.assertEqual(state["status"], "auto_failed")
        self.assertEqual(state["attempts"], runner.CATEGORY_CLASSIFICATION_MAX_ATTEMPTS)
        self.assertFalse(await store.category_is_waived(tool_id))

        readiness = await runner.D1EnrichmentStore(self.d1).evaluate_tool(tool_id)
        self.assertEqual(readiness, "blocked")
        enrichment = self.task_row("tool_enrichment_states", "tool_id = ?", [tool_id])
        self.assertIn("category", json.loads(enrichment["blocking_json"]))
        self.assertNotIn("category_needs_manual", json.loads(enrichment["warnings_json"]))

        outcomes = [
            row["outcome"]
            for row in self.connection.execute(
                "select outcome from tool_category_classification_events where tool_id = ? order by id",
                [tool_id],
            ).fetchall()
        ]
        self.assertEqual(outcomes, ["auto_failed", "auto_failed", "auto_failed"])

    async def test_asset_localization_uses_clean_public_slug_and_numbers_real_collisions(self) -> None:
        existing_tool_id = self.add_tool("existing-hocoos")
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, feature_highlights,
              translation_status, published_at
            ) VALUES (?, 'en', 'hocoos', 'Existing Hocoos', '[]', 'published', ?)
            """,
            [existing_tool_id, runner.utc_now_iso()],
        )
        tool_id = self.add_tool("hocoos-e549821f")
        self.connection.execute(
            "UPDATE tools SET normalized_domain = 'hocoos.com', official_url = 'https://hocoos.com/' WHERE id = ?",
            [tool_id],
        )
        self.connection.commit()
        task = runner.AssetTask(
            tool_id=tool_id,
            canonical_slug="hocoos-e549821f",
            normalized_domain="hocoos.com",
            official_url="https://hocoos.com/",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test-lease",
        )
        result = runner.AssetFetchResult(
            final_url=task.official_url,
            screenshot=b"",
            title="Hocoos AI Website Builder",
            description="Build a website with AI.",
        )

        await runner.D1AssetStore(self.d1).save_tool_localization(task, result)

        localization = self.connection.execute(
            "SELECT localized_slug, name FROM tool_localizations WHERE tool_id = ? AND locale_code = 'en'",
            [tool_id],
        ).fetchone()
        self.assertEqual(localization["localized_slug"], "hocoos-2")
        self.assertEqual(localization["name"], "Hocoos AI Website Builder")

    async def test_low_confidence_name_uses_publish_review_fallback(self) -> None:
        tool_id = self.add_tool("uncertain-name")
        task = runner.AssetTask(
            tool_id=tool_id,
            canonical_slug="uncertain-name",
            normalized_domain="uncertain-name.example",
            official_url="https://uncertain-name.example/",
            attempts=1,
            max_attempts=5,
            generation=1,
            lease_token="test-lease",
        )
        result = runner.AssetFetchResult(
            final_url=task.official_url,
            title="100+ AI Models in One Place",
            description="An AI product whose exact brand needs confirmation.",
            name_source="domain_fallback",
            name_confidence=45,
            name_evidence="hostname=uncertain-name.example",
            name_review_status="needs_review",
        )

        await runner.D1AssetStore(self.d1).save_tool_localization(task, result)
        localization = self.connection.execute(
            "SELECT name, name_confidence, name_review_status FROM tool_localizations WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(localization["name"], "Uncertain Name")
        self.assertEqual(localization["name_confidence"], runner.AUTO_APPROVE_TOOL_NAME_CONFIDENCE)
        self.assertEqual(localization["name_review_status"], "auto_approved")

        readiness = await runner.D1EnrichmentStore(self.d1).evaluate_tool(tool_id)
        self.assertEqual(readiness, "blocked")
        enrichment = self.task_row("tool_enrichment_states", "tool_id = ?", [tool_id])
        self.assertNotIn("name_quality", json.loads(enrichment["blocking_json"]))

        self.connection.execute(
            """
            UPDATE tool_localizations
            SET name = ', <meta>, JSON-LD and hreflang/canonical links are emitted by useSeo() into each rendered',
                name_review_status = 'legacy_unreviewed'
            WHERE tool_id = ? AND locale_code = 'en'
            """,
            [tool_id],
        )
        self.connection.commit()
        await runner.D1EnrichmentStore(self.d1).evaluate_tool(tool_id)
        enrichment = self.task_row("tool_enrichment_states", "tool_id = ?", [tool_id])
        self.assertIn("name_quality", json.loads(enrichment["blocking_json"]))

    async def test_public_slug_data_migration_is_global_collision_safe_and_idempotent(self) -> None:
        existing_runway_id = self.add_tool("runway", status="published")
        pending_delve_id = self.add_tool("delve-11111111", status="pending_review")
        published_delve_id = self.add_tool("delve-22222222", status="published")
        runway_candidate_id = self.add_tool("runway-33333333", status="pending_review")
        unique_id = self.add_tool("unique-brand-44444444", status="published")
        for tool_id, slug, name in (
            (existing_runway_id, "runway", "Runway"),
            (pending_delve_id, "delve-11111111", "Delve Compliance"),
            (published_delve_id, "delve-22222222", "Delve AI"),
            (runway_candidate_id, "runway-33333333", "Runway Candidate"),
            (unique_id, "unique-brand-44444444", "Unique Brand"),
        ):
            self.connection.execute(
                """
                INSERT INTO tool_localizations (
                  tool_id, locale_code, localized_slug, name, feature_highlights,
                  translation_status, published_at
                ) VALUES (?, 'en', ?, ?, '[]', 'published', ?)
                """,
                [tool_id, slug, name, runner.utc_now_iso()],
            )
        self.connection.commit()
        migration_sql = (MIGRATIONS_DIR / "0020_clean_public_tool_slugs.sql").read_text(encoding="utf-8")

        self.connection.executescript(migration_sql)
        self.connection.executescript(migration_sql)

        slugs = {
            row["tool_id"]: row["localized_slug"]
            for row in self.connection.execute(
                "SELECT tool_id, localized_slug FROM tool_localizations WHERE locale_code = 'en'"
            ).fetchall()
        }
        self.assertEqual(slugs[existing_runway_id], "runway")
        self.assertEqual(slugs[published_delve_id], "delve")
        self.assertEqual(slugs[pending_delve_id], "delve-2")
        self.assertEqual(slugs[runway_candidate_id], "runway-2")
        self.assertEqual(slugs[unique_id], "unique-brand")
        canonical_slug = self.connection.execute(
            "SELECT canonical_slug FROM tools WHERE id = ?",
            [unique_id],
        ).fetchone()["canonical_slug"]
        self.assertEqual(canonical_slug, "unique-brand-44444444")

    async def test_non_retryable_asset_failure_is_dead_lettered_immediately(self) -> None:
        tool_id = self.add_tool("asset-contract-error")
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        task = (await store.claim_due_tasks(10, "asset-worker"))[0]

        completed = await store.complete_task(
            task,
            "failed",
            "browser_run_json_api_error: invalid schema",
            retryable=False,
        )

        self.assertTrue(completed)
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["next_retry_at"])
        self.assertTrue(row["dead_letter_at"])

    async def test_asset_failed_task_is_not_reset_or_revived_by_normal_queue(self) -> None:
        tool_id = self.add_tool("asset-failed")
        store = runner.D1AssetStore(self.d1)
        self.assertNotIn("force", inspect.signature(store.queue_missing_asset_tasks).parameters)
        self.connection.execute(
            """
            INSERT INTO asset_tasks (
              tool_id, normalized_domain, source, status, attempts, max_attempts,
              next_retry_at, last_error
            )
            VALUES (?, 'asset-failed.example', ?, 'failed', 2, 5, '2000-01-01T00:00:00Z', 'keep me')
            """,
            [tool_id, runner.ASSET_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_missing_asset_tasks(10), 0)

        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["last_error"], "keep me")

    async def test_incomplete_asset_dead_letter_is_revived_after_cooldown(self) -> None:
        tool_id = self.add_tool("asset-auto-revive")
        store = runner.D1AssetStore(self.d1)
        self.connection.execute(
            "UPDATE tools SET category_classification_status = 'needs_manual', category_classification_attempts = 3 WHERE id = ?",
            [tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO asset_tasks (
              tool_id, normalized_domain, source, status, attempts, max_attempts,
              generation, last_error, dead_letter_at
            )
            VALUES (?, 'asset-auto-revive.example', ?, 'failed', 5, 5, 1,
                    'asset_enrichment_incomplete: missing=key_features,category',
                    '2000-01-01T00:00:00Z')
            """,
            [tool_id, runner.ASSET_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.revive_incomplete_dead_letter_tasks(10), 1)
        task = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["generation"], 2)
        self.assertIsNone(task["dead_letter_at"])
        tool = self.connection.execute(
            "SELECT category_classification_status, category_classification_attempts FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["category_classification_status"], "needs_manual")
        self.assertEqual(tool["category_classification_attempts"], 3)

    async def test_content_safety_dead_letter_is_not_automatically_revived(self) -> None:
        tool_id = self.add_tool("asset-unsafe")
        store = runner.D1AssetStore(self.d1)
        self.connection.execute(
            """
            INSERT INTO asset_tasks (
              tool_id, normalized_domain, source, status, attempts, max_attempts,
              generation, last_error, dead_letter_at
            )
            VALUES (?, 'asset-unsafe.example', ?, 'failed', 1, 5, 1,
                    'content_safety_blocked:nsfw:strong_signal',
                    '2000-01-01T00:00:00Z')
            """,
            [tool_id, runner.ASSET_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.revive_incomplete_dead_letter_tasks(10), 0)
        task = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertIsNotNone(task["dead_letter_at"])

    async def test_domain_failed_task_is_not_reset_or_revived_by_normal_queue(self) -> None:
        self.add_tool("domain-failed")
        store = runner.D1DomainStateStore(self.d1)
        self.assertNotIn("force", inspect.signature(store.queue_due_tasks).parameters)
        self.connection.execute(
            """
            INSERT INTO domain_state_tasks (
              normalized_domain, source, status, attempts, max_attempts,
              next_retry_at, last_error
            )
            VALUES ('domain-failed.example', ?, 'failed', 3, 5, '2000-01-01T00:00:00Z', 'keep me too')
            """,
            [runner.DOMAIN_STATE_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_due_tasks(10, 30), 0)

        row = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            ["domain-failed.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["last_error"], "keep me too")

    async def test_terminal_domain_failures_do_not_starve_stale_completed_domains(self) -> None:
        store = runner.D1DomainStateStore(self.d1)
        for index in range(3):
            suffix = f"domain-terminal-{index}"
            self.add_tool(suffix)
            self.connection.execute(
                """
                INSERT INTO domain_state_tasks (
                  normalized_domain, source, status, attempts, max_attempts,
                  next_retry_at, last_error
                ) VALUES (?, ?, 'failed', 5, 5, NULL, 'terminal failure')
                """,
                [f"{suffix}.example", runner.DOMAIN_STATE_SOURCE],
            )

        self.add_tool("domain-stale-completed")
        self.connection.execute(
            """
            INSERT INTO domain_states (
              normalized_domain, source, domain_rating, last_crawled_at,
              rdap_status, rdap_checked_at
            ) VALUES (?, ?, 42, '2020-01-01T00:00:00Z', 'done', '2020-01-01T00:00:00Z')
            """,
            ["domain-stale-completed.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.connection.execute(
            """
            INSERT INTO domain_state_tasks (
              normalized_domain, source, status, attempts, generation,
              fetch_domain_rating, fetch_rdap, last_completed_at
            ) VALUES (?, ?, 'done', 1, 1, 1, 0, '2020-01-01T00:00:00Z')
            """,
            ["domain-stale-completed.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_due_tasks(1, 30), 1)

        stale = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            ["domain-stale-completed.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(stale["status"], "queued")
        self.assertEqual(stale["generation"], 2)
        self.assertEqual(stale["attempts"], 0)

        terminal = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            ["domain-terminal-0.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["attempts"], 5)

    async def test_configured_ahrefs_key_can_recover_only_missing_key_dead_letters(self) -> None:
        store = runner.D1DomainStateStore(self.d1)
        for suffix, error in (
            ("domain-missing-key", "AHREF_API_KEY is required for Ahrefs Domain Rating requests"),
            ("domain-real-failure", "ahrefs_http_500"),
        ):
            self.add_tool(suffix)
            self.connection.execute(
                """
                INSERT INTO domain_state_tasks (
                  normalized_domain, source, status, attempts, max_attempts,
                  generation, last_error, dead_letter_at
                ) VALUES (?, ?, 'failed', 5, 5, 1, ?, '2026-01-01T00:00:00Z')
                """,
                [f"{suffix}.example", runner.DOMAIN_STATE_SOURCE, error],
            )
        self.connection.commit()

        self.assertEqual(await store.requeue_missing_credential_tasks(10), 1)

        recovered = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            ["domain-missing-key.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["attempts"], 0)
        self.assertEqual(recovered["generation"], 2)
        self.assertIsNone(recovered["last_error"])
        self.assertIsNone(recovered["dead_letter_at"])

        untouched = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            ["domain-real-failure.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(untouched["status"], "failed")
        self.assertEqual(untouched["attempts"], 5)
        self.assertEqual(untouched["last_error"], "ahrefs_http_500")
        self.assertIsNotNone(untouched["dead_letter_at"])

    async def test_stale_asset_completion_cannot_overwrite_new_generation_and_token(self) -> None:
        tool_id = self.add_tool("asset-stale-complete")
        store = runner.D1AssetStore(self.d1)
        self.assertEqual(await store.queue_missing_asset_tasks(10), 1)
        old_task = (await store.claim_due_tasks(10, "old-worker"))[0]
        new_generation = old_task.generation + 1
        self.connection.execute(
            """
            UPDATE asset_tasks
            SET generation = ?, lease_owner = 'new-worker', lease_token = 'new-token',
                lease_expires_at = '2099-01-01T00:00:00Z', status = 'processing'
            WHERE tool_id = ? AND source = ?
            """,
            [new_generation, tool_id, runner.ASSET_SOURCE],
        )
        self.connection.commit()

        completed = await store.complete_task(old_task, "done")

        self.assertFalse(completed)
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["generation"], new_generation)
        self.assertEqual(row["lease_owner"], "new-worker")
        self.assertEqual(row["lease_token"], "new-token")
        self.assertIsNone(row["last_completed_at"])

    async def test_traffic_queue_claim_lease_complete(self) -> None:
        tool_id = self.add_tool("traffic-flow")
        traffic_month = "2026-06"
        store = runner.D1TaskStore(self.d1)

        self.assertEqual(await store.queue_missing_traffic_tasks(10, traffic_month), 1)
        tasks = await store.claim_due_tasks(10, "traffic-worker")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        row = self.task_row(
            "traffic_tasks",
            "normalized_domain = ? AND source = ? AND traffic_month = ?",
            [task.normalized_domain, runner.TRAFFIC_SOURCE, traffic_month],
        )
        self.assertEqual(row["status"], "processing")
        self.assert_active_lease(row, "traffic-worker")
        self.assertEqual(await store.claim_due_tasks(10, "other-worker"), [])

        result = runner.FetchResult(
            status="done",
            monthly_rows=[{"traffic_month": traffic_month, "visits": 1234}],
        )
        await self.d1.insert_result(task, result)
        await store.complete_task(task, result)
        row = self.task_row(
            "traffic_tasks",
            "normalized_domain = ? AND source = ? AND traffic_month = ?",
            [task.normalized_domain, runner.TRAFFIC_SOURCE, traffic_month],
        )
        self.assert_completed_lease(row, "done")
        monthly = self.connection.execute(
            "SELECT visits FROM domain_traffic_monthly WHERE normalized_domain = ? AND traffic_month = ?",
            [task.normalized_domain, traffic_month],
        ).fetchone()
        self.assertEqual(monthly["visits"], 1234)

    async def test_ai_directory_domains_join_the_monthly_traffic_queue(self) -> None:
        self.connection.execute(
            """
            INSERT INTO ai_directory_sites (
              normalized_domain, canonical_url, submission_url, submission_mode, status
            )
            VALUES ('directory.example', 'https://directory.example/', 'https://directory.example/submit', 'free', 'active')
            """
        )
        self.connection.commit()
        store = runner.D1TaskStore(self.d1)

        self.assertEqual(await store.queue_missing_traffic_tasks(10, "2026-06-01"), 1)
        row = self.task_row(
            "traffic_tasks",
            "normalized_domain = ? AND source = ? AND traffic_month = ?",
            ["directory.example", runner.TRAFFIC_SOURCE, "2026-06-01"],
        )
        self.assertEqual(row["status"], "queued")

    async def test_similarweb_keywords_and_raw_payload_are_preserved(self) -> None:
        tool_id = self.add_tool("traffic-keywords", status="published")
        traffic_month = "2026-06-01"
        store = runner.D1TaskStore(self.d1)
        self.assertEqual(await store.queue_missing_traffic_tasks(10, traffic_month), 1)
        task = (await store.claim_due_tasks(10, "traffic-worker"))[0]
        raw_payload = {
            "SiteName": "traffic-keywords.example",
            "SnapshotDate": traffic_month,
            "EstimatedMonthlyVisits": {traffic_month: 4321},
            "TopKeywords": [
                {"Name": "keyword one", "Volume": 1200, "EstimatedValue": 900, "Cpc": 1.25},
                {"Name": "keyword two", "Volume": None, "EstimatedValue": 40, "Cpc": None},
            ],
            "TrafficSources": {
                "Direct": 0.4,
                "SearchOrganic": 0.35,
                "SearchPaid": 0.05,
                "GenAi": 0.02,
            },
            "AiTrafficDetails": {
                "TotalVisits": 321,
                "ReferralTraffic": 0.074,
                "Traffic": {
                    "Distribution": {
                        "Boundary": "2026-01-01",
                        "Chatbots": [
                            {"Name": "ChatGPT", "Value": 0.72},
                            {"Name": "Perplexity", "Value": 0.28},
                        ],
                    },
                    "Split": [{"Name": "ChatGPT", "Rank": 1}],
                },
                "TopPrompts": {"Status": 200, "Prompts": [{"Text": "keyword one"}]},
            },
        }
        rows = runner.parse_monthly_rows(raw_payload, task.normalized_domain, traffic_month)
        self.assertEqual(rows[0]["top_keywords"][0]["name"], "keyword one")
        self.assertEqual(rows[0]["top_keywords"][0]["volume"], 1200)
        self.assertEqual(rows[0]["gen_ai_traffic_share"], 0.02)
        self.assertEqual(rows[0]["ai_traffic"]["total_visits"], 321)

        result = runner.FetchResult(
            status="done",
            monthly_rows=rows,
            observed_latest_month=traffic_month,
            raw_payload=raw_payload,
        )
        await self.d1.insert_result(task, result)

        monthly = self.connection.execute(
            "SELECT metrics_json, gen_ai_traffic_share, ai_visits, ai_referral_share "
            "FROM domain_traffic_monthly WHERE normalized_domain = ? AND traffic_month = ?",
            [task.normalized_domain, traffic_month],
        ).fetchone()
        self.assertEqual(monthly["gen_ai_traffic_share"], 0.02)
        self.assertEqual(monthly["ai_visits"], 321)
        self.assertEqual(monthly["ai_referral_share"], 0.074)
        self.assertEqual(
            self.connection.execute(
                "SELECT json_array_length(?, '$.top_search_keywords')",
                [monthly["metrics_json"]],
            ).fetchone()[0],
            2,
        )
        self.assertIn('"ChatGPT"', monthly["metrics_json"])
        snapshot = self.connection.execute(
            "SELECT raw_payload FROM domain_traffic_snapshots WHERE normalized_domain = ? ORDER BY id DESC LIMIT 1",
            [task.normalized_domain],
        ).fetchone()
        self.assertIn('"TopKeywords"', snapshot["raw_payload"])

        await self.d1.upsert_domain_traffic_monthly(
            task.normalized_domain,
            [{"traffic_month": traffic_month, "visits": 5000, "website": task.normalized_domain}],
        )
        preserved = self.connection.execute(
            "SELECT visits, json_array_length(metrics_json, '$.top_search_keywords') AS keyword_count "
            "FROM domain_traffic_monthly WHERE normalized_domain = ? AND traffic_month = ?",
            [task.normalized_domain, traffic_month],
        ).fetchone()
        self.assertEqual(preserved["visits"], 5000)
        self.assertEqual(preserved["keyword_count"], 2)

    async def test_domain_traffic_monthly_dual_writes_and_reconciles_top_countries(self) -> None:
        domain = "traffic-countries.example"
        traffic_month = "2026-06-01"
        await self.d1.upsert_domain_traffic_monthly(
            domain,
            [
                {
                    "traffic_month": traffic_month,
                    "visits": 10000,
                    "top_country_1": "us",
                    "top_country_1_traffic_share": 0.33335,
                    "top_country_2": "GB",
                    "top_country_2_traffic_share": 0.1,
                }
            ],
            captured_at="2026-07-01T00:00:00Z",
        )

        rows = self.connection.execute(
            "SELECT country_code, country_position, traffic_share_bps, estimated_visits "
            "FROM domain_traffic_country_monthly "
            "WHERE normalized_domain = ? AND traffic_month = ? ORDER BY country_position",
            [domain, traffic_month],
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("US", 1, 3334, 3334), ("GB", 2, 1000, 1000)],
        )

        await self.d1.upsert_domain_traffic_monthly(
            domain,
            [
                {
                    "traffic_month": traffic_month,
                    "visits": 12000,
                    "top_country_1": "ca",
                    "top_country_1_traffic_share": 0.25,
                }
            ],
            captured_at="2026-07-02T00:00:00Z",
        )
        reconciled = self.connection.execute(
            "SELECT country_code, country_position, traffic_share_bps, estimated_visits "
            "FROM domain_traffic_country_monthly "
            "WHERE normalized_domain = ? AND traffic_month = ?",
            [domain, traffic_month],
        ).fetchall()
        self.assertEqual([tuple(row) for row in reconciled], [("CA", 1, 2500, 3000)])

        await self.d1.upsert_domain_traffic_monthly(
            domain,
            [{"traffic_month": traffic_month, "visits": 16000}],
            captured_at="2026-07-03T00:00:00Z",
        )
        refreshed = self.connection.execute(
            "SELECT country_code, traffic_share_bps, estimated_visits "
            "FROM domain_traffic_country_monthly "
            "WHERE normalized_domain = ? AND traffic_month = ?",
            [domain, traffic_month],
        ).fetchall()
        self.assertEqual([tuple(row) for row in refreshed], [("CA", 2500, 4000)])

    async def test_country_dual_write_safely_degrades_only_when_0041_table_is_missing(self) -> None:
        self.connection.execute("DROP TABLE domain_traffic_country_monthly")
        self.connection.commit()

        with patch.object(runner, "log_info") as mocked_log:
            await self.d1.upsert_domain_traffic_monthly(
                "traffic-pre-migration.example",
                [
                    {
                        "traffic_month": "2026-06-01",
                        "visits": 777,
                        "top_country_1": "US",
                        "top_country_1_traffic_share": 0.5,
                    }
                ],
            )

        monthly = self.connection.execute(
            "SELECT visits FROM domain_traffic_monthly "
            "WHERE normalized_domain = ? AND source = ? AND traffic_month = ?",
            ["traffic-pre-migration.example", runner.TRAFFIC_SOURCE, "2026-06-01"],
        ).fetchone()
        self.assertEqual(monthly["visits"], 777)
        self.assertIs(self.d1._market_country_schema_available, False)
        mocked_log.assert_called_once()
        self.assertEqual(
            mocked_log.call_args.args[0],
            "d1.domain_traffic_country_monthly.schema_unavailable",
        )

    async def test_country_dual_write_does_not_hide_other_schema_errors(self) -> None:
        self.connection.execute("DROP TABLE domain_traffic_country_monthly")
        self.connection.execute("CREATE TABLE domain_traffic_country_monthly (unexpected TEXT)")
        self.connection.commit()

        with self.assertRaises(sqlite3.OperationalError):
            await self.d1.upsert_domain_traffic_monthly(
                "traffic-bad-schema.example",
                [
                    {
                        "traffic_month": "2026-06-01",
                        "visits": 900,
                        "top_country_1": "US",
                        "top_country_1_traffic_share": 0.5,
                    }
                ],
            )

    async def test_market_foundation_migration_is_schema_only(self) -> None:
        migration_sql = (
            MIGRATIONS_DIR / "0041_market_explorer_foundation.sql"
        ).read_text(encoding="utf-8")
        executable_sql = "\n".join(
            line.split("--", 1)[0] for line in migration_sql.splitlines()
        )
        statements = [
            " ".join(statement.split()).upper()
            for statement in executable_sql.split(";")
            if statement.strip()
        ]

        self.assertTrue(statements)
        self.assertTrue(
            all(
                statement == "END"
                or statement.startswith(
                    (
                        "PRAGMA ",
                        "CREATE TABLE ",
                        "CREATE INDEX ",
                        "CREATE UNIQUE INDEX ",
                        "CREATE TRIGGER ",
                    )
                )
                for statement in statements
            )
        )
        self.assertNotIn("JSON_EACH(", executable_sql.upper())
        self.assertNotIn("ROW_NUMBER() OVER", executable_sql.upper())
        self.assertNotIn("INSERT INTO DOMAIN_TRAFFIC_COUNTRY_MONTHLY", executable_sql.upper())

    async def test_traffic_projection_backfill_pages_resumes_and_replays_countries(self) -> None:
        snapshots = [
            {
                "domain": "raw-countries.example",
                "visits": 1000,
                "country_1": None,
                "share_1": None,
                "country_2": None,
                "share_2": None,
                "raw_payload": {
                    "SiteName": "raw-countries.example",
                    "SnapshotDate": "2026-06-01",
                    "EstimatedMonthlyVisits": {"2026-06-01": 1000},
                    "TopCountryShares": [
                        {"CountryCode": "US", "Value": 0.6},
                        {"CountryCode": "GB", "Value": 0.2},
                    ],
                },
            },
            {
                "domain": "legacy-columns.example",
                "visits": 800,
                "country_1": "CA",
                "share_1": 0.5,
                "country_2": "MX",
                "share_2": 0.125,
                "raw_payload": {},
            },
            {
                "domain": "resume-countries.example",
                "visits": 500,
                "country_1": None,
                "share_1": None,
                "country_2": None,
                "share_2": None,
                "raw_payload": {
                    "SiteName": "resume-countries.example",
                    "SnapshotDate": "2026-06-01",
                    "EstimatedMonthlyVisits": {"2026-06-01": 500},
                    "TopCountryShares": [{"CountryCode": "JP", "Value": 0.4}],
                },
            },
        ]
        snapshot_ids: list[int] = []
        for index, snapshot in enumerate(snapshots, start=1):
            cursor = self.connection.execute(
                """
                INSERT INTO domain_traffic_snapshots (
                  normalized_domain, source, website, traffic_month, status, visits,
                  top_country_1, top_country_1_traffic_share,
                  top_country_2, top_country_2_traffic_share,
                  fetched_at, raw_payload
                )
                VALUES (?, ?, ?, '2026-06-01', 'done', ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    snapshot["domain"],
                    runner.TRAFFIC_SOURCE,
                    snapshot["domain"],
                    snapshot["visits"],
                    snapshot["country_1"],
                    snapshot["share_1"],
                    snapshot["country_2"],
                    snapshot["share_2"],
                    f"2026-07-0{index}T00:00:00Z",
                    json.dumps(snapshot["raw_payload"]),
                ],
            )
            snapshot_ids.append(int(cursor.lastrowid))
        self.connection.commit()

        with patch.object(runner, "log_info"):
            first = await runner.backfill_domain_traffic_monthly_from_d1(
                self.d1,
                limit=2,
                page_size=1,
                write_batch_size=4,
            )

        self.assertEqual(first["snapshots_scanned"], 2)
        self.assertEqual(first["pages_completed"], 2)
        self.assertEqual(first["last_snapshot_id"], snapshot_ids[1])
        self.assertEqual(first["monthly_rows_upserted"], 2)
        self.assertEqual(first["country_rows_upserted"], 4)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM domain_traffic_monthly WHERE normalized_domain = ?",
                [snapshots[2]["domain"]],
            ).fetchone()
        )

        with patch.object(runner, "log_info"):
            resumed = await runner.backfill_domain_traffic_monthly_from_d1(
                self.d1,
                after_snapshot_id=first["last_snapshot_id"],
                page_size=1,
                write_batch_size=4,
            )

        self.assertEqual(resumed["snapshots_scanned"], 1)
        self.assertEqual(resumed["pages_completed"], 1)
        self.assertEqual(resumed["last_snapshot_id"], snapshot_ids[2])
        country_rows = self.connection.execute(
            """
            SELECT normalized_domain, country_code, country_position,
                   traffic_share_bps, estimated_visits, source_snapshot_id
            FROM domain_traffic_country_monthly
            ORDER BY normalized_domain, country_position
            """
        ).fetchall()
        expected_rows = [
            ("legacy-columns.example", "CA", 1, 5000, 400, snapshot_ids[1]),
            ("legacy-columns.example", "MX", 2, 1250, 100, snapshot_ids[1]),
            ("raw-countries.example", "US", 1, 6000, 600, snapshot_ids[0]),
            ("raw-countries.example", "GB", 2, 2000, 200, snapshot_ids[0]),
            ("resume-countries.example", "JP", 1, 4000, 200, snapshot_ids[2]),
        ]
        self.assertEqual([tuple(row) for row in country_rows], expected_rows)
        legacy_monthly = self.connection.execute(
            "SELECT visits FROM domain_traffic_monthly WHERE normalized_domain = ?",
            [snapshots[1]["domain"]],
        ).fetchone()
        self.assertEqual(legacy_monthly["visits"], 800)

        with patch.object(runner, "log_info"):
            replayed = await runner.backfill_domain_traffic_monthly_from_d1(
                self.d1,
                page_size=2,
                write_batch_size=5,
            )
        replayed_country_rows = self.connection.execute(
            """
            SELECT normalized_domain, country_code, country_position,
                   traffic_share_bps, estimated_visits, source_snapshot_id
            FROM domain_traffic_country_monthly
            ORDER BY normalized_domain, country_position
            """
        ).fetchall()
        self.assertEqual(replayed["snapshots_scanned"], 3)
        self.assertEqual([tuple(row) for row in replayed_country_rows], expected_rows)

    async def test_traffic_projection_backfill_never_splits_a_snapshot_atomic_group(self) -> None:
        snapshot_specs = [
            (
                "atomic-first.example",
                1000,
                [{"CountryCode": "US", "Value": 0.5}],
            ),
            (
                "atomic-second.example",
                2000,
                [
                    {"CountryCode": "CA", "Value": 0.4},
                    {"CountryCode": "GB", "Value": 0.2},
                ],
            ),
        ]
        for index, (domain, visits, countries) in enumerate(snapshot_specs, start=1):
            self.connection.execute(
                """
                INSERT INTO domain_traffic_snapshots (
                  normalized_domain, source, website, traffic_month, status,
                  visits, fetched_at, raw_payload
                )
                VALUES (?, ?, ?, '2026-06-01', 'done', ?, ?, ?)
                """,
                [
                    domain,
                    runner.TRAFFIC_SOURCE,
                    domain,
                    visits,
                    f"2026-07-0{index}T00:00:00Z",
                    json.dumps(
                        {
                            "SiteName": domain,
                            "SnapshotDate": "2026-06-01",
                            "EstimatedMonthlyVisits": {"2026-06-01": visits},
                            "TopCountryShares": countries,
                        }
                    ),
                ],
            )
        self.connection.execute(
            """
            INSERT INTO domain_traffic_country_monthly (
              normalized_domain, source, traffic_month, country_code,
              country_position, traffic_share_bps, estimated_visits, captured_at
            )
            VALUES (
              'atomic-second.example', 'similarweb', '2026-06-01',
              'DE', 1, 9000, 1800, '2026-06-30T00:00:00Z'
            )
            """
        )
        self.connection.commit()

        class FailingSecondBatchD1(FakeD1):
            def __init__(self, connection: sqlite3.Connection):
                super().__init__(connection)
                self.batches: list[list[tuple[str, list[Any]]]] = []

            async def batch(
                self,
                statements: list[tuple[str, list[Any]]],
            ) -> list[dict[str, Any]]:
                self.batches.append(list(statements))
                if len(self.batches) != 2:
                    return await super().batch(statements)

                failing_statements: list[tuple[str, list[Any]]] = []
                failure_injected = False
                for statement in statements:
                    failing_statements.append(statement)
                    if (
                        not failure_injected
                        and statement[0] == runner.DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL
                    ):
                        failing_statements.append(
                            ("INSERT INTO forced_backfill_failure DEFAULT VALUES", [])
                        )
                        failure_injected = True
                if not failure_injected:
                    failing_statements.insert(
                        0,
                        ("INSERT INTO forced_backfill_failure DEFAULT VALUES", []),
                    )
                return await super().batch(failing_statements)

        failing_d1 = FailingSecondBatchD1(self.connection)
        with self.assertRaisesRegex(sqlite3.OperationalError, "forced_backfill_failure"):
            await runner.backfill_domain_traffic_monthly_from_d1(
                failing_d1,
                page_size=2,
                write_batch_size=5,
            )

        self.assertEqual([len(batch) for batch in failing_d1.batches], [3, 4])
        self.assertEqual(
            [{params[0] for _, params in batch} for batch in failing_d1.batches],
            [{"atomic-first.example"}, {"atomic-second.example"}],
        )
        self.assertEqual(
            sum(
                sql == runner.DOMAIN_TRAFFIC_COUNTRY_DELETE_SQL
                for sql, _ in failing_d1.batches[1]
            ),
            1,
        )
        self.assertEqual(
            sum(
                sql == runner.DOMAIN_TRAFFIC_COUNTRY_UPSERT_SQL
                for sql, _ in failing_d1.batches[1]
            ),
            2,
        )

        first_country = self.connection.execute(
            "SELECT country_code FROM domain_traffic_country_monthly "
            "WHERE normalized_domain = 'atomic-first.example'"
        ).fetchone()
        self.assertEqual(first_country["country_code"], "US")
        second_countries = self.connection.execute(
            "SELECT country_code FROM domain_traffic_country_monthly "
            "WHERE normalized_domain = 'atomic-second.example' ORDER BY country_code"
        ).fetchall()
        self.assertEqual([row["country_code"] for row in second_countries], ["DE"])
        second_monthly = self.connection.execute(
            "SELECT 1 FROM domain_traffic_monthly "
            "WHERE normalized_domain = 'atomic-second.example'"
        ).fetchone()
        self.assertIsNone(second_monthly)

    async def test_market_snapshot_builder_materializes_metrics_and_never_retires_on_failure(self) -> None:
        tool_id = self.add_tool("market-snapshot", status="published")
        duplicate_tool_id = self.add_tool("market-snapshot-duplicate", status="published")
        self.connection.execute(
            "UPDATE tools SET verification_status = 'verified', staleness_status = 'fresh' WHERE id = ?",
            [tool_id],
        )
        self.connection.execute(
            "UPDATE tools SET normalized_domain = 'market-snapshot.example', "
            "verification_status = 'verified', staleness_status = 'fresh' WHERE id = ?",
            [duplicate_tool_id],
        )
        self.connection.executemany(
            """
            INSERT INTO traffic_month_release_checks (
              source, traffic_month, status, probe_domain, observed_latest_month
            )
            VALUES (?, ?, ?, 'probe.example', ?)
            """,
            [
                (runner.TRAFFIC_SOURCE, "2026-05-01", "available", "2026-05-01"),
                (runner.TRAFFIC_SOURCE, "2026-06-01", "available", "2026-06-01"),
                (runner.TRAFFIC_SOURCE, "2026-07-01", "unavailable", "2026-06-01"),
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO domain_rating_history (
              normalized_domain, source, observed_date, domain_rating, observed_at
            )
            VALUES ('market-snapshot.example', ?, ?, ?, ?)
            """,
            [
                (runner.DOMAIN_STATE_SOURCE, "2026-06-01", 40, "2026-06-01T00:00:00Z"),
                (runner.DOMAIN_STATE_SOURCE, "2026-06-25", 45, "2026-06-25T00:00:00Z"),
                (runner.DOMAIN_STATE_SOURCE, "2026-07-01", 46, "2026-07-01T00:00:00Z"),
            ],
        )
        self.connection.commit()

        await self.d1.upsert_domain_traffic_monthly(
            "market-snapshot.example",
            [
                {
                    "traffic_month": "2026-05-01",
                    "visits": 1000,
                    "ai_traffic": {"total_visits": 100},
                    "gen_ai_traffic_share": 0.1,
                },
                {
                    "traffic_month": "2026-06-01",
                    "visits": 2000,
                    "ai_traffic": {"total_visits": 300},
                    "gen_ai_traffic_share": 0.15,
                    "search_organic_traffic_share": 0.25,
                    "search_paid_traffic_share": 0.05,
                    "top_country_1": "US",
                    "top_country_1_traffic_share": 0.6,
                    "top_country_2": "GB",
                    "top_country_2_traffic_share": 0.2,
                },
            ],
        )
        # An unavailable month cannot become the default serving input.
        await self.d1.upsert_domain_traffic_monthly(
            "market-snapshot.example",
            [
                {
                    "traffic_month": "2026-07-01",
                    "visits": 2500,
                    "top_country_1": "US",
                    "top_country_1_traffic_share": 0.7,
                }
            ],
        )

        preview = await runner.preview_market_snapshot_from_d1(self.d1)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(preview["traffic_month"], "2026-06-01")
        self.assertEqual(preview["baseline_month"], "2026-05-01")
        self.assertEqual(preview["coverage"]["total_tools"], 1)
        self.assertEqual(preview["coverage"]["country_tools"], 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM market_snapshot_versions").fetchone()[0],
            0,
        )

        result = await runner.build_market_snapshot_from_d1(self.d1, activate=True)
        self.assertEqual(result["status"], "active")
        active_snapshot_id = result["snapshot_id"]
        self.assertEqual(result["coverage"]["total_tools"], 1)
        self.assertEqual(result["coverage"]["country_tools"], 1)
        self.assertEqual(result["coverage"]["country_ai_rows"], 2)
        self.assertEqual(result["coverage"]["country_ai_tools"], 1)
        coverage_metadata = json.loads(
            self.connection.execute(
                "SELECT coverage_json FROM market_snapshot_versions WHERE id = ?",
                [active_snapshot_id],
            ).fetchone()["coverage_json"]
        )
        self.assertEqual(coverage_metadata["share_unit"], "basis_points")
        self.assertEqual(coverage_metadata["country_scope"], "provider_top5_observed_only")
        self.assertEqual(coverage_metadata["country_absence"], "unknown")
        self.assertEqual(coverage_metadata["country_ai_visits_provenance"], "modeled_not_observed")
        self.assertEqual(
            coverage_metadata["country_ai_visits_formula"],
            "estimated_visits * gen_ai_share_bps / 10000",
        )
        self.assertEqual(coverage_metadata["country_ai_visits_scope"], "provider_top5_lower_bound")

        snapshot = self.connection.execute(
            "SELECT * FROM tool_market_snapshots WHERE snapshot_id = ? AND tool_id = ?",
            [active_snapshot_id, tool_id],
        ).fetchone()
        self.assertIsNotNone(snapshot)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM tool_market_snapshots WHERE snapshot_id = ? AND tool_id = ?",
                [active_snapshot_id, duplicate_tool_id],
            ).fetchone()
        )
        self.assertEqual(snapshot["visits"], 2000)
        self.assertEqual(snapshot["previous_visits"], 1000)
        self.assertEqual(snapshot["visits_change"], 1000)
        self.assertAlmostEqual(snapshot["visits_growth_rate"], 1.0)
        self.assertEqual(snapshot["search_share_bps"], 3000)
        self.assertEqual(snapshot["organic_search_share_bps"], 2500)
        self.assertEqual(snapshot["paid_search_share_bps"], 500)
        self.assertEqual(snapshot["search_visits"], 600)
        self.assertEqual(snapshot["paid_search_visits"], 100)
        self.assertEqual(snapshot["ai_visits"], 300)
        self.assertEqual(snapshot["previous_ai_visits"], 100)
        self.assertEqual(snapshot["ai_visits_change"], 200)
        self.assertAlmostEqual(snapshot["ai_visits_growth_rate"], 2.0)
        self.assertEqual(snapshot["gen_ai_share_bps"], 1500)
        self.assertEqual(snapshot["domain_rating"], 46)
        self.assertEqual(snapshot["previous_domain_rating"], 40)
        self.assertEqual(snapshot["domain_rating_change"], 6)
        self.assertAlmostEqual(snapshot["domain_rating_velocity_30d"], 6.0)

        countries = self.connection.execute(
            "SELECT country_code, traffic_share_bps, estimated_visits, estimated_ai_visits "
            "FROM tool_country_market_snapshots WHERE snapshot_id = ? ORDER BY country_position",
            [active_snapshot_id],
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in countries],
            [("US", 6000, 1200, 180), ("GB", 2000, 400, 60)],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO tool_market_snapshots (snapshot_id, tool_id, normalized_domain) "
                "VALUES (?, ?, 'market-snapshot.example')",
                [active_snapshot_id, duplicate_tool_id],
            )
        self.connection.rollback()

        # Explicit builds are allowed for investigation, but a candidate that
        # regresses a premium metric must not replace the active serving snapshot.
        with self.assertRaisesRegex(RuntimeError, "failed search_tools absolute coverage gate"):
            await runner.build_market_snapshot_from_d1(
                self.d1,
                "2026-07-01",
                activate=True,
            )
        active = self.connection.execute(
            "SELECT id FROM market_snapshot_versions WHERE status = 'active'"
        ).fetchone()
        self.assertEqual(active["id"], active_snapshot_id)
        failed_activation_candidate = self.connection.execute(
            "SELECT id, status FROM market_snapshot_versions WHERE traffic_month = '2026-07-01'"
        ).fetchone()
        self.assertEqual(failed_activation_candidate["status"], "candidate")
        unknown_ai_estimate = self.connection.execute(
            "SELECT estimated_ai_visits FROM tool_country_market_snapshots "
            "WHERE snapshot_id = ? LIMIT 1",
            [failed_activation_candidate["id"]],
        ).fetchone()
        self.assertIsNone(unknown_ai_estimate["estimated_ai_visits"])

    async def test_market_facet_rollups_preserve_known_zero_unknown_and_grains(self) -> None:
        category_id = int(
            self.connection.execute(
                "INSERT INTO categories (canonical_slug) VALUES ('market-rollup-category')"
            ).lastrowid
        )
        tool_ids = [
            self.add_tool(f"market-rollup-{index}", status="published")
            for index in range(2)
        ]
        self.connection.execute(
            "UPDATE tools SET primary_category_id = ?, verification_status = 'verified', "
            "staleness_status = 'fresh' WHERE id IN (?, ?)",
            [category_id, *tool_ids],
        )
        eligibility_revision = await runner.ensure_market_catalog_eligibility_revision(self.d1)
        snapshot_id = int(
            self.connection.execute(
                """
                INSERT INTO market_snapshot_versions (
                  status, traffic_source, traffic_month, baseline_month,
                  catalog_eligibility_revision, coverage_json, built_at
                ) VALUES (
                  'candidate', ?, '2026-07-01', '2026-06-01', ?, '{}',
                  '2026-08-01T00:00:00Z'
                )
                """,
                [runner.TRAFFIC_SOURCE, eligibility_revision],
            ).lastrowid
        )
        for index, tool_id in enumerate(tool_ids):
            domain = f"market-rollup-{index}.example"
            self.connection.execute(
                """
                INSERT INTO tool_market_snapshots (
                  snapshot_id, tool_id, normalized_domain, primary_category_id, visits
                ) VALUES (?, ?, ?, ?, 1000)
                """,
                [snapshot_id, tool_id, domain, category_id],
            )
        country_rows = [
            (tool_ids[0], "market-rollup-0.example", "US", 1, 0, 0),
            (tool_ids[1], "market-rollup-1.example", "US", 1, None, None),
            (tool_ids[0], "market-rollup-0.example", "CA", 2, 0, 0),
            (tool_ids[1], "market-rollup-1.example", "GB", 2, None, None),
        ]
        self.connection.executemany(
            """
            INSERT INTO tool_country_market_snapshots (
              snapshot_id, tool_id, normalized_domain, country_code,
              country_position, traffic_share_bps, estimated_visits,
              estimated_ai_visits
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            [
                [snapshot_id, tool_id, domain, country, position, visits, ai_visits]
                for tool_id, domain, country, position, visits, ai_visits in country_rows
            ],
        )
        self.connection.commit()

        await runner.build_market_snapshot_facet_rollups_from_d1(
            self.d1,
            snapshot_id,
            eligibility_revision,
            "2026-08-01T00:00:00Z",
        )

        rows = self.connection.execute(
            """
            SELECT primary_category_id, country_code, tool_count,
                   country_estimated_visits, country_estimated_ai_visits,
                   country_estimated_visits_unknown_count,
                   country_estimated_ai_visits_unknown_count
            FROM market_snapshot_facet_rollups
            WHERE snapshot_id = ?
            ORDER BY primary_category_id, country_code
            """,
            [snapshot_id],
        ).fetchall()
        indexed = {
            (row["primary_category_id"], row["country_code"]): tuple(row)[2:]
            for row in rows
        }
        self.assertEqual(indexed[(0, "")], (2, None, None, 0, 0))
        self.assertEqual(indexed[(category_id, "")], (2, None, None, 0, 0))
        self.assertEqual(indexed[(0, "US")], (2, 0, 0, 1, 1))
        self.assertEqual(indexed[(category_id, "US")], (2, 0, 0, 1, 1))
        self.assertEqual(indexed[(0, "CA")], (1, 0, 0, 0, 0))
        self.assertEqual(indexed[(0, "GB")], (1, None, None, 1, 1))
        self.assertNotIn((0, "JP"), indexed)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO market_snapshot_facet_rollups (
                  snapshot_id, country_code, tool_count,
                  country_estimated_visits_unknown_count,
                  country_estimated_ai_visits_unknown_count
                ) VALUES (?, 'JP', 1, 0, 0)
                """,
                [snapshot_id],
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO market_snapshot_facet_rollups (
                  snapshot_id, country_code, tool_count,
                  country_estimated_visits, country_estimated_ai_visits
                ) VALUES (?, 'JP', 1, 10, 11)
                """,
                [snapshot_id],
            )
        self.connection.rollback()

    async def test_market_entitlement_seed_has_stable_plan_matrix(self) -> None:
        # The plan skeleton is safe to apply again during local/bootstrap workflows.
        self.connection.executescript(
            (MIGRATIONS_DIR / "0042_market_access_entitlements.sql").read_text(encoding="utf-8")
        )
        plans = self.connection.execute(
            "SELECT code FROM access_plans ORDER BY sort_order"
        ).fetchall()
        self.assertEqual([row["code"] for row in plans], ["free", "pro", "enterprise"])
        matrix = self.connection.execute(
            "SELECT plan_code, feature_key, is_enabled, limit_json "
            "FROM access_plan_features"
        ).fetchall()
        self.assertEqual(len(matrix), 42)
        indexed = {(row["plan_code"], row["feature_key"]): row for row in matrix}
        self.assertEqual(indexed[("free", "filter.country")]["is_enabled"], 1)
        self.assertEqual(indexed[("free", "sort.search_share")]["is_enabled"], 0)
        self.assertEqual(indexed[("free", "sort.paid_share")]["is_enabled"], 0)
        self.assertEqual(indexed[("pro", "sort.search_share")]["is_enabled"], 1)
        self.assertEqual(indexed[("pro", "sort.paid_share")]["is_enabled"], 1)
        self.assertEqual(indexed[("free", "sort.ai_visits")]["is_enabled"], 0)
        self.assertEqual(indexed[("pro", "sort.ai_visits")]["is_enabled"], 1)
        self.assertEqual(indexed[("free", "sort.dr_velocity_30d")]["is_enabled"], 0)
        self.assertEqual(indexed[("pro", "sort.dr_velocity_30d")]["is_enabled"], 1)
        self.assertEqual(indexed[("enterprise", "sort.country_ai_estimated_visits")]["is_enabled"], 1)
        self.assertEqual(indexed[("pro", "sort.country_ai_estimated_visits")]["is_enabled"], 1)
        self.assertEqual(indexed[("free", "sort.country_ai_estimated_visits")]["is_enabled"], 0)

    async def test_existing_market_candidate_can_be_activated_without_rebuilding(self) -> None:
        tool_id = self.add_tool("activate-existing-market", status="published")
        self.connection.execute(
            "UPDATE tools SET verification_status = 'verified', staleness_status = 'fresh' WHERE id = ?",
            [tool_id],
        )
        version_ids: list[int] = []
        for status, traffic_month in (("active", "2026-06-01"), ("candidate", "2026-07-01")):
            cursor = self.connection.execute(
                """
                INSERT INTO market_snapshot_versions (
                  status, traffic_source, traffic_month, baseline_month,
                  coverage_json, built_at, activated_at
                )
                VALUES (?, ?, ?, '2026-05-01', '{}', '2026-08-01T00:00:00Z', ?)
                """,
                [
                    status,
                    runner.TRAFFIC_SOURCE,
                    traffic_month,
                    "2026-08-01T00:00:00Z" if status == "active" else None,
                ],
            )
            version_ids.append(int(cursor.lastrowid))

        for snapshot_id in version_ids:
            self.connection.execute(
                """
                INSERT INTO tool_market_snapshots (
                  snapshot_id, tool_id, normalized_domain, visits,
                  search_share_bps, paid_search_share_bps,
                  ai_visits, gen_ai_share_bps, domain_rating,
                  domain_rating_change, domain_rating_velocity_30d
                )
                VALUES (
                  ?, ?, 'activate-existing-market.example', 1000, 4000, 1000,
                  100, 1000, 70, 2, 2
                )
                """,
                [snapshot_id, tool_id],
            )
            self.connection.execute(
                """
                INSERT INTO tool_country_market_snapshots (
                  snapshot_id, tool_id, normalized_domain, country_code,
                  country_position, traffic_share_bps, estimated_visits,
                  estimated_ai_visits
                )
                VALUES (
                  ?, ?, 'activate-existing-market.example', 'US', 1, 5000, 500, 50
                )
                """,
                [snapshot_id, tool_id],
            )
        self.connection.commit()
        eligibility_revision = await runner.ensure_market_catalog_eligibility_revision(self.d1)
        for snapshot_id in version_ids:
            await runner.build_market_snapshot_facet_rollups_from_d1(
                self.d1,
                snapshot_id,
                eligibility_revision,
                "2026-08-01T00:00:00Z",
            )

        activation = await runner.activate_market_snapshot_from_d1(
            self.d1,
            version_ids[1],
        )

        self.assertEqual(activation["snapshot_id"], version_ids[1])
        self.assertEqual(activation["previous_snapshot_id"], version_ids[0])
        statuses = self.connection.execute(
            "SELECT id, status FROM market_snapshot_versions ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["status"]) for row in statuses],
            [(version_ids[0], "retired"), (version_ids[1], "active")],
        )
        revision_before = self.connection.execute(
            "SELECT revision FROM market_catalog_eligibility_revision WHERE id = 1"
        ).fetchone()["revision"]
        self.add_tool("not-in-active-market-snapshot", status="published")
        revision_after_insert = self.connection.execute(
            "SELECT revision FROM market_catalog_eligibility_revision WHERE id = 1"
        ).fetchone()["revision"]
        self.assertEqual(revision_after_insert, revision_before)

        candidate_id = int(
            self.connection.execute(
                """
                INSERT INTO market_snapshot_versions (
                  status, traffic_source, traffic_month, baseline_month,
                  catalog_eligibility_revision, coverage_json, built_at
                ) VALUES (
                  'candidate', ?, '2026-08-01', '2026-07-01', ?, '{}',
                  '2026-08-02T00:00:00Z'
                )
                """,
                [runner.TRAFFIC_SOURCE, revision_before],
            ).lastrowid
        )
        self.connection.execute(
            """
            INSERT INTO tool_market_snapshots (
              snapshot_id, tool_id, normalized_domain, visits,
              search_share_bps, paid_search_share_bps,
              ai_visits, gen_ai_share_bps, domain_rating,
              domain_rating_change, domain_rating_velocity_30d
            ) VALUES (
              ?, ?, 'activate-existing-market.example', 1000, 4000, 1000,
              100, 1000, 70, 2, 2
            )
            """,
            [candidate_id, tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_country_market_snapshots (
              snapshot_id, tool_id, normalized_domain, country_code,
              country_position, traffic_share_bps, estimated_visits,
              estimated_ai_visits
            ) VALUES (
              ?, ?, 'activate-existing-market.example', 'US', 1, 5000, 500, 50
            )
            """,
            [candidate_id, tool_id],
        )
        self.connection.commit()
        await runner.build_market_snapshot_facet_rollups_from_d1(
            self.d1,
            candidate_id,
            revision_before,
            "2026-08-02T00:00:00Z",
        )

        self.connection.execute(
            "UPDATE tools SET status = 'archived' WHERE id = ?",
            [tool_id],
        )
        self.connection.commit()
        revision_after_active_change = self.connection.execute(
            "SELECT revision FROM market_catalog_eligibility_revision WHERE id = 1"
        ).fetchone()["revision"]
        self.assertEqual(revision_after_active_change, revision_before + 1)
        with self.assertRaisesRegex(
            RuntimeError,
            "current_catalog_eligibility_revision=.*expected=",
        ):
            await runner.activate_market_snapshot_from_d1(self.d1, candidate_id)
        statuses_after_rejected_activation = self.connection.execute(
            "SELECT id, status FROM market_snapshot_versions WHERE id IN (?, ?) ORDER BY id",
            [version_ids[1], candidate_id],
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["status"]) for row in statuses_after_rejected_activation],
            [(version_ids[1], "active"), (candidate_id, "candidate")],
        )

    async def test_market_candidate_without_dr_coverage_cannot_activate(self) -> None:
        tool_id = self.add_tool("market-candidate-without-dr", status="published")
        self.connection.execute(
            "UPDATE tools SET verification_status = 'verified', staleness_status = 'fresh' WHERE id = ?",
            [tool_id],
        )
        cursor = self.connection.execute(
            """
            INSERT INTO market_snapshot_versions (
              status, traffic_source, traffic_month, baseline_month,
              coverage_json, built_at
            )
            VALUES (
              'candidate', ?, '2026-07-01', '2026-06-01', '{}',
              '2026-08-01T00:00:00Z'
            )
            """,
            [runner.TRAFFIC_SOURCE],
        )
        snapshot_id = int(cursor.lastrowid)
        self.connection.execute(
            """
            INSERT INTO tool_market_snapshots (
              snapshot_id, tool_id, normalized_domain, visits,
              search_share_bps, paid_search_share_bps,
              ai_visits, gen_ai_share_bps
            )
            VALUES (
              ?, ?, 'market-candidate-without-dr.example', 1000,
              4000, 1000, 100, 1000
            )
            """,
            [snapshot_id, tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_country_market_snapshots (
              snapshot_id, tool_id, normalized_domain, country_code,
              country_position, traffic_share_bps, estimated_visits,
              estimated_ai_visits
            )
            VALUES (
              ?, ?, 'market-candidate-without-dr.example', 'US', 1,
              5000, 500, 50
            )
            """,
            [snapshot_id, tool_id],
        )
        self.connection.commit()
        eligibility_revision = await runner.ensure_market_catalog_eligibility_revision(self.d1)
        await runner.build_market_snapshot_facet_rollups_from_d1(
            self.d1,
            snapshot_id,
            eligibility_revision,
            "2026-08-01T00:00:00Z",
        )

        with self.assertRaisesRegex(RuntimeError, "dr_tools absolute coverage gate"):
            await runner.activate_market_snapshot_from_d1(self.d1, snapshot_id)

        status = self.connection.execute(
            "SELECT status FROM market_snapshot_versions WHERE id = ?",
            [snapshot_id],
        ).fetchone()["status"]
        self.assertEqual(status, "candidate")

    async def test_market_candidate_cannot_regress_relative_dr_coverage(self) -> None:
        tool_ids = [
            self.add_tool(f"market-relative-dr-{index:02d}", status="published")
            for index in range(20)
        ]
        self.connection.execute(
            "UPDATE tools SET verification_status = 'verified', staleness_status = 'fresh' "
            f"WHERE id IN ({','.join('?' for _ in tool_ids)})",
            tool_ids,
        )
        version_ids: list[int] = []
        for status, traffic_month in (("active", "2026-06-01"), ("candidate", "2026-07-01")):
            cursor = self.connection.execute(
                """
                INSERT INTO market_snapshot_versions (
                  status, traffic_source, traffic_month, baseline_month,
                  coverage_json, built_at, activated_at
                )
                VALUES (?, ?, ?, '2026-05-01', '{}', '2026-08-01T00:00:00Z', ?)
                """,
                [
                    status,
                    runner.TRAFFIC_SOURCE,
                    traffic_month,
                    "2026-08-01T00:00:00Z" if status == "active" else None,
                ],
            )
            version_ids.append(int(cursor.lastrowid))

        for snapshot_index, snapshot_id in enumerate(version_ids):
            for tool_index, tool_id in enumerate(tool_ids):
                has_dr = snapshot_index == 0 or tool_index < 18
                domain = f"market-relative-dr-{tool_index:02d}.example"
                self.connection.execute(
                    """
                    INSERT INTO tool_market_snapshots (
                      snapshot_id, tool_id, normalized_domain, visits,
                      search_share_bps, paid_search_share_bps,
                      ai_visits, gen_ai_share_bps, domain_rating,
                      domain_rating_change, domain_rating_velocity_30d
                    )
                    VALUES (?, ?, ?, 1000, 4000, 1000, 100, 1000, ?, ?, ?)
                    """,
                    [
                        snapshot_id,
                        tool_id,
                        domain,
                        70 if has_dr else None,
                        2 if has_dr else None,
                        2 if has_dr else None,
                    ],
                )
                self.connection.execute(
                    """
                    INSERT INTO tool_country_market_snapshots (
                      snapshot_id, tool_id, normalized_domain, country_code,
                      country_position, traffic_share_bps, estimated_visits,
                      estimated_ai_visits
                    )
                    VALUES (?, ?, ?, 'US', 1, 5000, 500, 50)
                    """,
                    [snapshot_id, tool_id, domain],
                )
        self.connection.commit()
        eligibility_revision = await runner.ensure_market_catalog_eligibility_revision(self.d1)
        for snapshot_id in version_ids:
            await runner.build_market_snapshot_facet_rollups_from_d1(
                self.d1,
                snapshot_id,
                eligibility_revision,
                "2026-08-01T00:00:00Z",
            )

        with self.assertRaisesRegex(RuntimeError, "failed dr_tools coverage gate"):
            await runner.activate_market_snapshot_from_d1(self.d1, version_ids[1])

        statuses = self.connection.execute(
            "SELECT id, status FROM market_snapshot_versions ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["status"]) for row in statuses],
            [(version_ids[0], "active"), (version_ids[1], "candidate")],
        )

    async def test_activate_market_snapshot_id_cli_is_a_separate_mode(self) -> None:
        with patch.object(
            runner.sys,
            "argv",
            ["runner.py", "--activate-market-snapshot-id", "42"],
        ):
            args = runner.parse_args()
        self.assertEqual(args.activate_market_snapshot_id, 42)
        self.assertFalse(args.build_market_snapshot)

        with patch.object(
            runner.sys,
            "argv",
            [
                "runner.py",
                "--build-market-snapshot",
                "--activate-market-snapshot-id",
                "42",
            ],
        ):
            with self.assertRaises(SystemExit):
                runner.parse_args()

        help_output = io.StringIO()
        with (
            patch.object(runner.sys, "argv", ["runner.py", "--help"]),
            redirect_stdout(help_output),
            self.assertRaises(SystemExit) as help_exit,
        ):
            runner.parse_args()
        self.assertEqual(help_exit.exception.code, 0)
        self.assertIn(
            "WRITE: apply coverage gates, then atomically activate",
            help_output.getvalue(),
        )

    async def test_split_worker_cli_modes_are_exclusive_and_profiled(self) -> None:
        periodic_args = runner.parse_args(["--periodic-facts", "--loop"])
        self.assertTrue(periodic_args.periodic_facts)
        self.assertEqual(
            runner.runtime_profile_for_args(periodic_args),
            ("periodic-facts-worker", ("traffic", "domain_state")),
        )

        assets_args = runner.parse_args(["--assets", "--loop"])
        self.assertTrue(assets_args.assets)
        self.assertEqual(
            runner.runtime_profile_for_args(assets_args),
            ("assets-worker", ("assets", "enrichment", "catalog_publish")),
        )

        taxonomy_args = runner.parse_args(["--taxonomy", "--loop"])
        self.assertTrue(taxonomy_args.taxonomy)
        self.assertEqual(
            runner.runtime_profile_for_args(taxonomy_args),
            ("taxonomy-worker", ("taxonomy",)),
        )

        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            runner.parse_args(["--backfill-published-categories", "--dry-run"])

        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            runner.parse_args(["--periodic-facts", "--assets", "--loop"])

    async def test_periodic_facts_once_keeps_workload_results_separate(self) -> None:
        config = type(
            "PeriodicFactsConfig",
            (),
            {"limit": 500, "domain_state_limit": 50},
        )()
        with (
            patch.object(
                runner,
                "run_once",
                new=AsyncMock(return_value={"claimed": 20, "done": 19, "failed": 1}),
            ) as run_traffic,
            patch.object(
                runner,
                "run_domain_state_once",
                new=AsyncMock(return_value={"claimed": 2, "done": 2, "failed": 0}),
            ) as run_domain,
        ):
            counts = await runner.run_periodic_facts_once(config, traffic_limit=100)

        run_traffic.assert_awaited_once_with(config, 100)
        run_domain.assert_awaited_once_with(config, 50)
        self.assertEqual(counts["traffic_claimed"], 20)
        self.assertEqual(counts["traffic_failed"], 1)
        self.assertEqual(counts["domain_claimed"], 2)
        self.assertEqual(counts["domain_failed"], 0)

    def test_domain_state_batches_back_off_after_draining_available_work(self) -> None:
        self.assertEqual(
            runner.domain_state_next_delay_seconds(
                {"claimed": 50},
                batch_limit=50,
                idle_interval_seconds=1,
            ),
            1,
        )
        self.assertEqual(
            runner.domain_state_next_delay_seconds(
                {"claimed": 49},
                batch_limit=50,
                idle_interval_seconds=1,
            ),
            10,
        )
        self.assertEqual(
            runner.domain_state_next_delay_seconds(
                {"claimed": 0, "queued": 0},
                batch_limit=50,
                idle_interval_seconds=1,
            ),
            60,
        )

    async def test_telemetry_health_checks_only_the_latest_run_per_workload(self) -> None:
        class TelemetryConfig:
            runner_instance_id = "telemetry-latest-workload-health"
            runner_version = "test-version"

        telemetry = runner.RunnerTelemetry(self.d1, TelemetryConfig())
        traffic_failure = await telemetry.start("traffic")
        await telemetry.finish(traffic_failure, {"failed": 1})
        domain_success = await telemetry.start("domain_state")
        await telemetry.finish(domain_success, {"done": 1})

        degraded = self.connection.execute(
            "SELECT status FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()
        self.assertEqual(degraded["status"], "degraded")

        traffic_success = await telemetry.start("traffic")
        await telemetry.finish(traffic_success, {"done": 1})
        healthy = self.connection.execute(
            "SELECT status FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()
        self.assertEqual(healthy["status"], "healthy")

    async def test_activate_market_snapshot_id_cli_does_not_require_brightdata(self) -> None:
        config = type("ActivationConfig", (), {"poll_interval_seconds": 60})()
        with (
            patch.object(
                runner.sys,
                "argv",
                ["runner.py", "--activate-market-snapshot-id", "42"],
            ),
            patch.object(runner, "load_config", return_value=config) as load_config,
            patch.object(
                runner,
                "activate_market_snapshot",
                new=AsyncMock(return_value={"snapshot_id": 42}),
            ) as activate_snapshot,
            patch.object(runner, "log_info"),
        ):
            await asyncio.to_thread(runner.main)

        load_config.assert_called_once_with(require_brightdata=False)
        activate_snapshot.assert_awaited_once_with(config, 42)

    async def test_done_traffic_task_without_materialization_starts_new_generation(self) -> None:
        self.add_tool("traffic-missing-materialization", status="published")
        traffic_month = "2026-06-01"
        store = runner.D1TaskStore(self.d1)
        self.connection.execute(
            """
            INSERT INTO traffic_tasks (
              normalized_domain, source, traffic_month, status, attempts,
              generation, last_started_at, last_fetched_at, last_completed_at
            )
            VALUES (?, ?, ?, 'done', 3, 4, ?, ?, ?)
            """,
            [
                "traffic-missing-materialization.example",
                runner.TRAFFIC_SOURCE,
                traffic_month,
                runner.utc_now_iso(),
                runner.utc_now_iso(),
                runner.utc_now_iso(),
            ],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_missing_traffic_tasks(10, traffic_month), 1)
        row = self.task_row(
            "traffic_tasks",
            "normalized_domain = ? AND source = ? AND traffic_month = ?",
            ["traffic-missing-materialization.example", runner.TRAFFIC_SOURCE, traffic_month],
        )
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["generation"], 5)
        self.assertIsNone(row["lease_token"])
        self.assertIsNone(row["last_started_at"])
        self.assertIsNone(row["last_fetched_at"])
        self.assertIn("materialization is missing", row["last_error"])

    async def test_terminal_traffic_without_data_is_not_requeued(self) -> None:
        self.add_tool("traffic-terminal-no-data", status="published")
        traffic_month = "2026-06-01"
        store = runner.D1TaskStore(self.d1)
        self.connection.execute(
            """
            INSERT INTO traffic_tasks (normalized_domain, source, traffic_month, status)
            VALUES (?, ?, ?, 'no_data')
            """,
            ["traffic-terminal-no-data.example", runner.TRAFFIC_SOURCE, traffic_month],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_missing_traffic_tasks(10, traffic_month), 0)
        row = self.task_row(
            "traffic_tasks",
            "normalized_domain = ? AND source = ? AND traffic_month = ?",
            ["traffic-terminal-no-data.example", runner.TRAFFIC_SOURCE, traffic_month],
        )
        self.assertEqual(row["status"], "no_data")

    async def test_traffic_queue_skips_existing_claimable_tasks(self) -> None:
        self.add_tool("traffic-already-failed", status="published")
        self.add_tool("traffic-already-queued", status="published")
        traffic_month = "2026-06-01"
        self.connection.executemany(
            """
            INSERT INTO traffic_tasks (
              normalized_domain, source, traffic_month, status, attempts,
              max_attempts, next_retry_at, last_queued_at
            )
            VALUES (?, ?, ?, ?, ?, 5, ?, ?)
            """,
            [
                (
                    "traffic-already-failed.example",
                    runner.TRAFFIC_SOURCE,
                    traffic_month,
                    "failed",
                    1,
                    "2000-01-01T00:00:00Z",
                    "2000-01-01T00:00:00Z",
                ),
                (
                    "traffic-already-queued.example",
                    runner.TRAFFIC_SOURCE,
                    traffic_month,
                    "queued",
                    0,
                    None,
                    "2000-01-01T00:00:00Z",
                ),
            ],
        )
        self.connection.commit()
        store = runner.D1TaskStore(self.d1)

        with patch.object(self.d1, "run", wraps=self.d1.run) as run:
            queued = await store.queue_missing_traffic_tasks(10, traffic_month)

        self.assertEqual(queued, 0)
        self.assertEqual(run.await_count, 0)
        claimed = await store.claim_due_tasks(10, "traffic-worker")
        self.assertEqual(
            {task.normalized_domain for task in claimed},
            {"traffic-already-failed.example", "traffic-already-queued.example"},
        )

    async def test_release_gate_requires_the_exact_requested_month(self) -> None:
        self.assertFalse(
            runner.requested_month_has_traffic_data(
                [{"traffic_month": "2026-05-01", "visits": 1234}],
                "2026-06-01",
            )
        )
        self.assertTrue(
            runner.requested_month_has_traffic_data(
                [{"traffic_month": "2026-06-01", "visits": 0}],
                "2026-06-01",
            )
        )

    async def test_release_gate_persists_unavailable_probe_until_next_check(self) -> None:
        class ProbeClient:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch(self, domain: str, traffic_month: str) -> runner.FetchResult:
                self.calls += 1
                return runner.FetchResult(
                    status="no_data",
                    monthly_rows=[{"traffic_month": "2026-05-01", "visits": 1234}],
                    error="requested_month_unavailable:latest=2026-05-01",
                    observed_latest_month="2026-05-01",
                )

        client = ProbeClient()
        store = runner.D1TrafficReleaseStore(self.d1)
        first = await store.check_or_probe("2026-06-01", "chatgpt.com", 3600, client)
        second = await store.check_or_probe("2026-06-01", "chatgpt.com", 3600, client)

        self.assertFalse(first.available)
        self.assertTrue(first.probe_attempted)
        self.assertEqual(first.observed_latest_month, "2026-05-01")
        self.assertFalse(second.available)
        self.assertFalse(second.probe_attempted)
        self.assertEqual(client.calls, 1)
        gate = self.connection.execute(
            """
            SELECT status, probe_domain, observed_latest_month, attempts, next_check_at, available_at
            FROM traffic_month_release_checks
            WHERE source = ? AND traffic_month = ?
            """,
            [runner.TRAFFIC_SOURCE, "2026-06-01"],
        ).fetchone()
        self.assertEqual(gate["status"], "unavailable")
        self.assertEqual(gate["probe_domain"], "chatgpt.com")
        self.assertEqual(gate["observed_latest_month"], "2026-05-01")
        self.assertEqual(gate["attempts"], 1)
        self.assertTrue(gate["next_check_at"])
        self.assertIsNone(gate["available_at"])

    async def test_release_gate_opens_after_probe_contains_target_month(self) -> None:
        class ProbeClient:
            async def fetch(self, domain: str, traffic_month: str) -> runner.FetchResult:
                return runner.FetchResult(
                    status="done",
                    monthly_rows=[{"traffic_month": traffic_month, "visits": 1234}],
                    observed_latest_month=traffic_month,
                )

        gate = await runner.D1TrafficReleaseStore(self.d1).check_or_probe(
            "2026-06-01",
            "chatgpt.com",
            3600,
            ProbeClient(),
        )

        self.assertTrue(gate.available)
        self.assertEqual(gate.status, "available")
        row = self.connection.execute(
            """
            SELECT status, next_check_at, available_at, last_error
            FROM traffic_month_release_checks
            WHERE source = ? AND traffic_month = ?
            """,
            [runner.TRAFFIC_SOURCE, "2026-06-01"],
        ).fetchone()
        self.assertEqual(row["status"], "available")
        self.assertIsNone(row["next_check_at"])
        self.assertTrue(row["available_at"])
        self.assertIsNone(row["last_error"])

    async def test_run_once_does_not_enqueue_before_release_gate_opens(self) -> None:
        self.add_tool("traffic-gated", status="published")

        class ProbeClient:
            async def fetch(self, domain: str, traffic_month: str) -> runner.FetchResult:
                return runner.FetchResult(
                    status="no_data",
                    monthly_rows=[{"traffic_month": "2026-05-01", "visits": 1234}],
                    observed_latest_month="2026-05-01",
                    error="requested_month_unavailable:latest=2026-05-01",
                )

        class Config:
            limit = 20
            concurrency = 1
            traffic_release_probe_domain = "chatgpt.com"
            traffic_release_probe_interval_seconds = 3600
            traffic_release_queue_limit = 5000
            runner_instance_id = "release-gate-test"
            max_retries = 0

        with patch.object(runner, "SimilarWebClient", return_value=ProbeClient()), patch.object(
            runner,
            "previous_traffic_month",
            return_value="2026-06-01",
        ), patch.object(runner, "traffic_release_probe_window_open", return_value=True):
            counts = await runner._run_once(Config(), self.d1, 20)

        self.assertEqual(counts["release_available"], 0)
        self.assertEqual(counts["traffic_queued"], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM traffic_tasks").fetchone()[0],
            0,
        )

    async def test_run_once_queues_full_catalog_after_release_gate_opens(self) -> None:
        self.add_tool("traffic-release-one", status="published")
        self.add_tool("traffic-release-two", status="published")
        self.add_tool("traffic-release-three", status="published")

        class ProbeClient:
            async def fetch(self, domain: str, traffic_month: str) -> runner.FetchResult:
                return runner.FetchResult(
                    status="done",
                    monthly_rows=[{"traffic_month": traffic_month, "visits": 1234}],
                    observed_latest_month=traffic_month,
                )

        class Config:
            limit = 1
            concurrency = 1
            traffic_release_probe_domain = "chatgpt.com"
            traffic_release_probe_interval_seconds = 3600
            traffic_release_queue_limit = 5000
            runner_instance_id = "release-gate-test"
            max_retries = 0

        with patch.object(runner, "SimilarWebClient", return_value=ProbeClient()), patch.object(
            runner,
            "previous_traffic_month",
            return_value="2026-06-01",
        ), patch.object(runner, "traffic_release_probe_window_open", return_value=True), patch.object(
            runner.D1TaskStore,
            "claim_due_tasks",
            return_value=[],
        ):
            counts = await runner._run_once(Config(), self.d1, 1)

        self.assertEqual(counts["release_available"], 1)
        self.assertEqual(counts["traffic_queued"], 3)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM traffic_tasks WHERE traffic_month = '2026-06-01' AND status = 'queued'"
            ).fetchone()[0],
            3,
        )

    async def test_domain_queue_claim_lease_complete(self) -> None:
        self.add_tool("domain-flow")
        store = runner.D1DomainStateStore(self.d1)

        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        tasks = await store.claim_due_tasks(10, "domain-worker")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        row = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(row["status"], "processing")
        self.assert_active_lease(row, "domain-worker")
        self.assertEqual(await store.claim_due_tasks(10, "other-worker"), [])

        await store.complete_task(
            task,
            runner.DomainStateResult(
                status="done",
                domain_rating=42.0,
                domain_created_at="2020-01-02T00:00:00Z",
                rdap_status="done",
            ),
        )
        row = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        )
        self.assert_completed_lease(row, "done")
        state = self.connection.execute(
            "SELECT domain_rating, domain_created_at, rdap_status, rdap_checked_at "
            "FROM domain_states WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchone()
        self.assertEqual(state["domain_rating"], 42.0)
        self.assertEqual(state["domain_created_at"], "2020-01-02T00:00:00Z")
        self.assertEqual(state["rdap_status"], "done")
        self.assertTrue(state["rdap_checked_at"])
        history = self.connection.execute(
            "SELECT domain_rating, observed_date FROM domain_rating_history "
            "WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["domain_rating"], 42.0)
        self.assertEqual(len(history[0]["observed_date"]), 10)

    async def test_completed_rdap_is_not_queued_again_with_monthly_dr(self) -> None:
        self.add_tool("domain-rdap-once")
        store = runner.D1DomainStateStore(self.d1)
        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        first_task = (await store.claim_due_tasks(10, "domain-worker"))[0]
        self.assertTrue(first_task.fetch_domain_rating)
        self.assertTrue(first_task.fetch_rdap)
        await store.complete_task(
            first_task,
            runner.DomainStateResult(
                status="done",
                domain_rating=42.0,
                domain_created_at="2020-01-02T00:00:00Z",
                rdap_status="done",
            ),
        )

        self.assertEqual(await store.queue_due_tasks(10, 30), 0)
        self.connection.execute(
            "UPDATE domain_states SET last_crawled_at = '2020-01-01T00:00:00Z' "
            "WHERE normalized_domain = ? AND source = ?",
            [first_task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        monthly_task = (await store.claim_due_tasks(10, "domain-worker"))[0]
        self.assertTrue(monthly_task.fetch_domain_rating)
        self.assertFalse(monthly_task.fetch_rdap)

    async def test_failed_rdap_is_marked_once_and_not_requeued(self) -> None:
        self.add_tool("domain-rdap-failed")
        store = runner.D1DomainStateStore(self.d1)
        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        task = (await store.claim_due_tasks(10, "domain-worker"))[0]
        await store.complete_task(
            task,
            runner.DomainStateResult(
                status="done",
                domain_rating=21.0,
                domain_created_at=None,
                rdap_status="failed",
                rdap_error="rdap_timeout",
            ),
        )

        state = self.connection.execute(
            "SELECT rdap_status, rdap_checked_at, rdap_last_error FROM domain_states "
            "WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchone()
        self.assertEqual(state["rdap_status"], "failed")
        self.assertTrue(state["rdap_checked_at"])
        self.assertEqual(state["rdap_last_error"], "rdap_timeout")
        self.assertEqual(await store.queue_due_tasks(10, 30), 0)

    async def test_pending_rdap_can_run_without_refreshing_fresh_dr(self) -> None:
        self.add_tool("domain-rdap-only")
        self.connection.execute(
            """
            INSERT INTO domain_states (
              normalized_domain, source, domain_rating, last_crawled_at
            ) VALUES (?, ?, 64, '2099-01-01T00:00:00Z')
            """,
            ["domain-rdap-only.example", runner.DOMAIN_STATE_SOURCE],
        )
        self.connection.commit()
        store = runner.D1DomainStateStore(self.d1)

        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        task = (await store.claim_due_tasks(10, "domain-worker"))[0]
        self.assertFalse(task.fetch_domain_rating)
        self.assertTrue(task.fetch_rdap)
        await store.complete_task(
            task,
            runner.DomainStateResult(
                status="done",
                domain_rating=None,
                domain_created_at=None,
                rdap_status="no_data",
                rdap_error="created_at_not_found",
            ),
        )

        state = self.connection.execute(
            "SELECT domain_rating, last_crawled_at, rdap_status FROM domain_states "
            "WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchone()
        self.assertEqual(state["domain_rating"], 64)
        self.assertEqual(state["last_crawled_at"], "2099-01-01T00:00:00Z")
        self.assertEqual(state["rdap_status"], "no_data")
        self.assertEqual(await store.queue_due_tasks(10, 30), 0)

    async def test_failed_dr_persists_rdap_and_disables_it_for_task_retry(self) -> None:
        self.add_tool("domain-dr-retry")
        store = runner.D1DomainStateStore(self.d1)
        self.assertEqual(await store.queue_due_tasks(10, 30), 1)
        task = (await store.claim_due_tasks(10, "domain-worker"))[0]
        await store.complete_task(
            task,
            runner.DomainStateResult(
                status="failed",
                domain_rating=None,
                domain_created_at="2020-01-02T00:00:00Z",
                error="ahrefs_down",
                rdap_status="done",
            ),
        )

        state = self.connection.execute(
            "SELECT domain_created_at, rdap_status FROM domain_states "
            "WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchone()
        retry = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        )
        self.assertEqual(state["domain_created_at"], "2020-01-02T00:00:00Z")
        self.assertEqual(state["rdap_status"], "done")
        self.assertEqual(retry["status"], "failed")
        self.assertEqual(retry["fetch_rdap"], 0)

    async def test_similarweb_three_month_windows_accumulate_long_term_history(self) -> None:
        tool_id = self.add_tool("traffic-history", status="published")
        first_payload = {
            "EstimatedMonthlyVisits": {
                "2026-01-01": 100,
                "2026-02-01": 200,
                "2026-03-01": 300,
            }
        }
        second_payload = {
            "EstimatedMonthlyVisits": {
                "2026-02-01": 220,
                "2026-03-01": 330,
                "2026-04-01": 400,
            }
        }

        await self.d1.upsert_domain_traffic_monthly(
            "traffic-history.example",
            runner.parse_monthly_rows(first_payload, "traffic-history.example", "2026-03-01"),
        )
        await self.d1.upsert_domain_traffic_monthly(
            "traffic-history.example",
            runner.parse_monthly_rows(second_payload, "traffic-history.example", "2026-04-01"),
        )

        rows = self.connection.execute(
            "SELECT traffic_month, visits FROM domain_traffic_monthly "
            "WHERE normalized_domain = ? ORDER BY traffic_month",
            ["traffic-history.example"],
        ).fetchall()
        self.assertEqual(
            [(row["traffic_month"], row["visits"]) for row in rows],
            [
                ("2026-01-01", 100),
                ("2026-02-01", 220),
                ("2026-03-01", 330),
                ("2026-04-01", 400),
            ],
        )

    async def test_pricing_queue_claim_lease_complete(self) -> None:
        tool_id = self.add_tool("pricing-flow")
        store = runner.D1PricingStore(self.d1)
        await store.insert_pricing_source(tool_id, "https://pricing-flow.example/pricing", "manual", 100)

        self.assertEqual(await store.queue_due_tasks(10), 1)
        tasks = await store.claim_due_tasks(10, lease_owner="pricing-worker")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        row = self.task_row("pricing_tasks", "id = ?", [task.task_id])
        self.assertEqual(row["status"], "running")
        self.assert_active_lease(row, "pricing-worker")
        self.assertEqual(await store.claim_due_tasks(10, lease_owner="other-worker"), [])

        result = runner.PricingFetchResult(
            url=task.source_url,
            final_url=task.source_url,
            status=200,
            content_type="text/html",
            html="<html><body>Free plan</body></html>",
        )
        await store.finish_task(task, "succeeded", None, result)
        row = self.task_row("pricing_tasks", "id = ?", [task.task_id])
        self.assert_completed_lease(row, "succeeded")
        source = self.connection.execute(
            "SELECT last_success_at FROM pricing_sources WHERE id = ?",
            [task.pricing_source_id],
        ).fetchone()
        self.assertTrue(source["last_success_at"])

    async def test_pricing_shadow_replays_manual_review_once_after_normal_work(self) -> None:
        replay_tool_id = self.add_tool("pricing-shadow-replay")
        normal_tool_id = self.add_tool("pricing-normal-priority")
        store = runner.D1PricingStore(self.d1)
        replay_url = "https://pricing-shadow-replay.example/pricing"
        normal_url = "https://pricing-normal-priority.example/pricing"
        await store.insert_pricing_source(replay_tool_id, replay_url, "manual", 100)
        await store.insert_pricing_source(normal_tool_id, normal_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 2)

        task_rows = self.connection.execute(
            "SELECT id, tool_id FROM pricing_tasks ORDER BY id"
        ).fetchall()
        replay_task_id = next(row["id"] for row in task_rows if row["tool_id"] == replay_tool_id)
        normal_task_id = next(row["id"] for row in task_rows if row["tool_id"] == normal_tool_id)
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', attempts = 1, finished_at = ? WHERE id = ?",
            [runner.utc_now_iso(), replay_task_id],
        )
        self.connection.commit()

        normal_claimed = await store.claim_due_tasks(
            2,
            lease_owner="pricing-shadow-normal-worker",
            replay_manual_review=False,
        )
        self.assertEqual([task.task_id for task in normal_claimed], [normal_task_id])
        self.assertFalse(normal_claimed[0].is_manual_review_replay)
        replay_claimed = await store.claim_due_tasks(
            1,
            lease_owner="pricing-shadow-replay-worker",
            replay_manual_review=True,
        )
        self.assertEqual([task.task_id for task in replay_claimed], [replay_task_id])
        self.assertTrue(replay_claimed[0].is_manual_review_replay)
        self.assertEqual(replay_claimed[0].attempts, 2)
        running_replay_row = self.connection.execute(
            "SELECT last_error FROM pricing_tasks WHERE id = ?",
            [replay_task_id],
        ).fetchone()
        self.assertEqual(
            running_replay_row["last_error"],
            runner.PRICING_CLAIMS_V2_REPLAY_RUNNING_MARKER,
        )
        self.assertEqual(
            await store.claim_due_tasks(
                2,
                lease_owner="pricing-shadow-race-worker",
                replay_manual_review=True,
            ),
            [],
        )

        replay_task = replay_claimed[0]
        html_body = """
        <section id="pricing" aria-label="Pricing plans">
          <h1>Pricing</h1><p>This product is free forever.</p>
        </section>
        """
        result = runner.PricingFetchResult(
            url=replay_url,
            final_url=replay_url,
            status=200,
            content_type="text/html",
            html=html_body,
        )
        bundle = runner.build_pricing_snapshot_bundle(html_body)
        snapshot_id = await store.insert_snapshot(replay_task, result, bundle)
        self.assertGreater(snapshot_id, 0)
        await store.insert_pricing_claims_shadow(replay_task, snapshot_id, bundle)
        self.assertTrue(
            await store.finish_task(
                replay_task,
                "manual_review",
                runner.pricing_claims_v2_replay_error(
                    "Python extraction pending manual approval",
                    len(bundle.raw_claims),
                ),
                result,
            )
        )
        replay_row = self.connection.execute(
            "SELECT last_error FROM pricing_tasks WHERE id = ?",
            [replay_task_id],
        ).fetchone()
        source_row = self.connection.execute(
            "SELECT last_error FROM pricing_sources WHERE id = ?",
            [replay_task.pricing_source_id],
        ).fetchone()
        self.assertTrue(replay_row["last_error"].startswith("pricing_claims_v2_replayed:claims="))
        self.assertEqual(source_row["last_error"], "Python extraction pending manual approval")
        replayed_again = await store.claim_due_tasks(
            10,
            lease_owner="pricing-shadow-replay-worker-two",
            replay_manual_review=True,
        )
        self.assertFalse(any(task.task_id == replay_task_id for task in replayed_again))

    async def test_pricing_manual_review_replay_identity_survives_expired_lease(self) -> None:
        tool_id = self.add_tool("pricing-shadow-replay-expired")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-shadow-replay-expired.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', attempts = 1, finished_at = ? WHERE tool_id = ?",
            [runner.utc_now_iso(), tool_id],
        )
        self.connection.commit()

        replay_task = (
            await store.claim_due_tasks(
                1,
                lease_owner="pricing-shadow-replay-expired-one",
                replay_manual_review=True,
            )
        )[0]
        self.connection.execute(
            "UPDATE pricing_tasks SET lease_expires_at = ? WHERE id = ?",
            [runner.iso_delta(hours=-1), replay_task.task_id],
        )
        self.connection.commit()

        recovered = (
            await store.claim_due_tasks(
                1,
                lease_owner="pricing-shadow-replay-expired-two",
            )
        )[0]
        self.assertEqual(recovered.task_id, replay_task.task_id)
        self.assertTrue(recovered.is_manual_review_replay)
        recovered_row = self.connection.execute(
            "SELECT last_error FROM pricing_tasks WHERE id = ?",
            [recovered.task_id],
        ).fetchone()
        self.assertEqual(
            recovered_row["last_error"],
            runner.PRICING_CLAIMS_V2_REPLAY_RUNNING_MARKER,
        )

    async def test_pricing_manual_review_replay_requires_shadow_opt_in(self) -> None:
        tool_id = self.add_tool("pricing-shadow-disabled")
        store = runner.D1PricingStore(self.d1)
        await store.insert_pricing_source(
            tool_id,
            "https://pricing-shadow-disabled.example/pricing",
            "manual",
            100,
        )
        self.assertEqual(await store.queue_due_tasks(10), 1)
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', attempts = 1, finished_at = ? WHERE tool_id = ?",
            [runner.utc_now_iso(), tool_id],
        )
        self.connection.commit()

        self.assertEqual(
            await store.claim_due_tasks(
                10,
                lease_owner="pricing-shadow-disabled-worker",
                replay_manual_review=False,
            ),
            [],
        )

    async def test_pricing_shadow_replays_only_latest_task_for_active_eligible_source(self) -> None:
        tool_id = self.add_tool("pricing-shadow-latest-only", status="published")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-shadow-latest-only.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        first_task_id = self.connection.execute(
            "SELECT id FROM pricing_tasks WHERE tool_id = ?",
            [tool_id],
        ).fetchone()["id"]
        now = runner.utc_now_iso()
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', attempts = 1, finished_at = ? WHERE id = ?",
            [now, first_task_id],
        )
        second_task_id = self.connection.execute(
            """
            INSERT INTO pricing_tasks (
              pricing_source_id, tool_id, status, run_after, attempts, max_attempts,
              finished_at, last_error
            )
            SELECT pricing_source_id, tool_id, 'manual_review', ?, 1, 3, ?, 'newer review'
            FROM pricing_tasks WHERE id = ?
            """,
            [now, now, first_task_id],
        ).lastrowid
        self.connection.commit()

        replayed = await store.claim_due_tasks(
            10,
            lease_owner="pricing-shadow-latest-only-worker",
            replay_manual_review=True,
        )
        self.assertEqual([task.task_id for task in replayed], [second_task_id])

        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL WHERE id = ?",
            [second_task_id],
        )
        self.connection.execute(
            "UPDATE pricing_sources SET is_active = 0 WHERE tool_id = ?",
            [tool_id],
        )
        self.connection.commit()
        self.assertEqual(
            await store.claim_due_tasks(
                10,
                lease_owner="pricing-shadow-inactive-source-worker",
                replay_manual_review=True,
            ),
            [],
        )

    async def test_pricing_zero_claim_v2_snapshot_still_checkpoints_replay(self) -> None:
        tool_id = self.add_tool("pricing-shadow-zero-claim")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-shadow-zero-claim.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'manual_review', attempts = 1, finished_at = ? WHERE tool_id = ?",
            [runner.utc_now_iso(), tool_id],
        )
        self.connection.commit()
        replay_task = (
            await store.claim_due_tasks(
                10,
                lease_owner="pricing-shadow-zero-claim-worker",
                replay_manual_review=True,
            )
        )[0]

        html_body = "<section id='pricing'><h1>Pricing</h1><p>Compare product capabilities.</p></section>"
        bundle = runner.build_pricing_snapshot_bundle(html_body)
        self.assertEqual(bundle.raw_claims, ())
        result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html=html_body,
        )
        snapshot_id = await store.insert_snapshot(replay_task, result, bundle)
        counts = await store.insert_pricing_claims_shadow(replay_task, snapshot_id, bundle)
        self.assertEqual(counts["claims_inserted"], 0)
        self.assertTrue(
            await store.finish_task(
                replay_task,
                "manual_review",
                runner.pricing_claims_v2_replay_error("no deterministic claims", 0),
                result,
            )
        )

        self.assertEqual(
            await store.claim_due_tasks(
                10,
                lease_owner="pricing-shadow-zero-claim-worker-two",
                replay_manual_review=True,
            ),
            [],
        )

    async def test_pricing_incomplete_v2_snapshot_retries_full_shadow_extraction(self) -> None:
        tool_id = self.add_tool("pricing-shadow-incomplete")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-shadow-incomplete.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        first_task = (await store.claim_due_tasks(10, lease_owner="pricing-shadow-incomplete-one"))[0]
        html_body = """
        <section id="pricing" aria-label="Pricing plans">
          <h1>Pricing</h1><p>Pro costs USD 29 per month.</p>
        </section>
        """
        bundle = runner.build_pricing_snapshot_bundle(html_body)
        result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html=html_body,
        )
        incomplete_snapshot_id = await store.insert_snapshot(first_task, result, bundle)
        self.assertGreater(incomplete_snapshot_id, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) AS total FROM pricing_snapshot_artifacts WHERE snapshot_id = ?",
                [incomplete_snapshot_id],
            ).fetchone()["total"],
            0,
        )
        self.assertTrue(
            await store.finish_task(
                first_task,
                "manual_review",
                "shadow persistence interrupted",
                result,
            )
        )

        replay_task = (
            await store.claim_due_tasks(
                10,
                lease_owner="pricing-shadow-incomplete-two",
                replay_manual_review=True,
            )
        )[0]

        class StubUploader:
            async def put_object(self, key: str, body: bytes, content_type: str) -> None:
                return None

        retry_plan = await store.prepare_pricing_claims_shadow(
            replay_task,
            bundle,
            StubUploader(),
        )
        self.assertTrue(retry_plan.region_changed)
        self.assertTrue(retry_plan.run_extraction)

    async def test_pricing_claims_shadow_deduplicates_artifacts_and_continues_claims(self) -> None:
        tool_id = self.add_tool("pricing-claims-shadow")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-claims-shadow.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        task = (await store.claim_due_tasks(10, lease_owner="claims-shadow-worker"))[0]
        html_body = """
        <html><body><section id="pricing" aria-label="Pricing plans">
          <h1>Pricing plans</h1><article><h2>Pro</h2><p>$29 per month</p></article>
        </section></body></html>
        """
        result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html=html_body,
        )
        bundle = runner.build_pricing_snapshot_bundle(html_body)

        class StubUploader:
            def __init__(self) -> None:
                self.keys: list[str] = []

            async def put_object(self, key: str, body: bytes, content_type: str) -> None:
                self.keys.append(key)

        uploader = StubUploader()
        first_plan = await store.prepare_pricing_claims_shadow(task, bundle, uploader)
        self.assertTrue(first_plan.region_changed)
        self.assertEqual(len(uploader.keys), 4)
        first_snapshot_id = await store.insert_snapshot(task, result, bundle)
        first_counts = await store.insert_pricing_claims_shadow(
            task, first_snapshot_id, bundle, first_plan
        )
        self.assertEqual(first_counts["claims_inserted"], 4)
        self.assertEqual(first_counts["claims_continued"], 0)

        second_plan = await store.prepare_pricing_claims_shadow(task, bundle, uploader)
        self.assertFalse(second_plan.region_changed)
        self.assertFalse(second_plan.run_extraction)
        self.assertEqual(len(uploader.keys), 4)
        second_snapshot_id = await store.insert_snapshot(task, result, bundle)
        second_counts = await store.insert_pricing_claims_shadow(
            task, second_snapshot_id, bundle, second_plan
        )
        self.assertEqual(second_counts["claims_inserted"], 0)
        self.assertEqual(second_counts["claims_continued"], 4)

        snapshot = self.connection.execute(
            "SELECT html_object_key, text_object_key, dom_map_object_key, pricing_region_hash "
            "FROM pricing_snapshots WHERE id = ?",
            [second_snapshot_id],
        ).fetchone()
        self.assertTrue(snapshot["html_object_key"].startswith("pricing/artifacts/html/"))
        self.assertTrue(snapshot["text_object_key"].startswith("pricing/artifacts/text/"))
        self.assertTrue(snapshot["dom_map_object_key"].startswith("pricing/artifacts/dom_map/"))
        self.assertEqual(snapshot["pricing_region_hash"], bundle.region.region_hash)
        active_claims = self.connection.execute(
            "SELECT claim_type, first_seen_snapshot_id, last_seen_snapshot_id, consecutive_seen_count, "
            "normalization_status, validation_status, decision_status, normalized_value_json, "
            "normalization_errors_json, normalizer_version, validator_version "
            "FROM pricing_claims WHERE lifecycle_status = 'active' ORDER BY claim_type"
        ).fetchall()
        self.assertEqual(len(active_claims), 4)
        self.assertTrue(all(row["first_seen_snapshot_id"] == first_snapshot_id for row in active_claims))
        self.assertTrue(all(row["last_seen_snapshot_id"] == second_snapshot_id for row in active_claims))
        self.assertTrue(all(row["consecutive_seen_count"] == 2 for row in active_claims))
        by_type = {row["claim_type"]: row for row in active_claims}
        self.assertEqual(by_type["has_paid_pricing"]["normalization_status"], "not_applicable")
        self.assertEqual(by_type["has_paid_pricing"]["validation_status"], "entailed")
        self.assertEqual(by_type["has_paid_pricing"]["decision_status"], "auto_verified")
        self.assertEqual(by_type["starting_paid_price"]["normalization_status"], "failed")
        self.assertEqual(by_type["starting_paid_price"]["validation_status"], "entailed")
        self.assertEqual(by_type["starting_paid_price"]["decision_status"], "unresolved")
        self.assertEqual(by_type["starting_paid_price"]["normalized_value_json"], '{"amount":"29"}')
        self.assertEqual(
            by_type["starting_paid_price"]["normalization_errors_json"],
            '["ambiguous_currency_symbol"]',
        )
        self.assertTrue(all(row["normalizer_version"] == "pricing-normalizer-v1" for row in active_claims))
        self.assertTrue(all(row["validator_version"] == "pricing-validator-v1" for row in active_claims))
        evidence_count = self.connection.execute(
            "SELECT count(*) AS total FROM pricing_claim_evidence"
        ).fetchone()["total"]
        self.assertEqual(evidence_count, 8)
        second_retention = self.connection.execute(
            "SELECT DISTINCT retention_class FROM pricing_snapshot_artifacts WHERE snapshot_id = ?",
            [second_snapshot_id],
        ).fetchall()
        self.assertEqual([row["retention_class"] for row in second_retention], ["diagnostic"])

        changed_html = html_body.replace("$29", "$39")
        changed_result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html=changed_html,
        )
        changed_bundle = runner.build_pricing_snapshot_bundle(changed_html)
        changed_plan = await store.prepare_pricing_claims_shadow(task, changed_bundle, uploader)
        self.assertTrue(changed_plan.region_changed)
        changed_snapshot_id = await store.insert_snapshot(task, changed_result, changed_bundle)
        changed_counts = await store.insert_pricing_claims_shadow(
            task, changed_snapshot_id, changed_bundle, changed_plan
        )
        self.assertEqual(changed_counts["claims_inserted"], 1)
        self.assertEqual(changed_counts["claims_continued"], 3)
        self.assertEqual(changed_counts["claims_superseded"], 1)
        price_claims = self.connection.execute(
            "SELECT lifecycle_status, raw_value_json FROM pricing_claims "
            "WHERE claim_type = 'starting_paid_price' ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [row["lifecycle_status"] for row in price_claims],
            ["superseded", "active"],
        )
        self.assertIn('"amount_raw":"39"', price_claims[-1]["raw_value_json"])
        change_events = self.connection.execute(
            "SELECT event_type FROM pricing_claim_events ORDER BY id"
        ).fetchall()
        self.assertEqual([row["event_type"] for row in change_events], ["change_detected"])

    async def test_missing_pricing_source_discovery_builds_unleased_probe_task(self) -> None:
        tool_id = self.add_tool("pricing-source-discovery", status="published")
        store = runner.D1PricingStore(self.d1)

        class StubPricingClient:
            async def choose_pricing_page(self, task: runner.PricingTask) -> runner.PricingFetchResult:
                self.task = task
                return runner.PricingFetchResult(
                    url=task.source_url,
                    final_url="https://pricing-source-discovery.example/pricing",
                    status=200,
                    content_type="text/html",
                    html="<html><body>Pricing plans start at $10 per month.</body></html>",
                )

        client = StubPricingClient()
        created = await runner.discover_missing_pricing_sources(store, client, 1)

        self.assertEqual(created, 1)
        self.assertEqual(client.task.tool_id, tool_id)
        self.assertEqual(client.task.generation, 1)
        self.assertEqual(client.task.lease_token, "")
        source = self.connection.execute(
            "SELECT url, is_active FROM pricing_sources WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(source["url"], "https://pricing-source-discovery.example/pricing")
        self.assertEqual(source["is_active"], 1)

    async def test_failed_pricing_source_discovery_retries_with_backoff_and_exhausts(self) -> None:
        tool_id = self.add_tool("pricing-source-unreachable", status="published")
        store = runner.D1PricingStore(self.d1)

        class UnreachablePricingClient:
            async def choose_pricing_page(self, task: runner.PricingTask) -> runner.PricingFetchResult:
                return runner.PricingFetchResult(
                    url=task.source_url,
                    final_url=task.source_url,
                    status=0,
                    content_type="",
                    html="",
                    error="connection failed",
                    page_status="not_found",
                )

        created = await runner.discover_missing_pricing_sources(store, UnreachablePricingClient(), 1)

        self.assertEqual(created, 0)
        source = self.connection.execute(
            "SELECT is_active, source_confidence, last_error, discovery_status, discovery_attempts, next_discovery_at "
            "FROM pricing_sources WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(source["is_active"], 0)
        self.assertEqual(source["source_confidence"], 0)
        self.assertEqual(source["last_error"], "connection failed")
        self.assertEqual(source["discovery_status"], "retryable")
        self.assertEqual(source["discovery_attempts"], 1)
        self.assertTrue(source["next_discovery_at"])
        self.assertEqual(await store.missing_source_candidates(1), [])

        self.connection.execute(
            "UPDATE pricing_sources SET next_discovery_at = '2000-01-01T00:00:00Z' WHERE tool_id = ?",
            [tool_id],
        )
        self.connection.commit()
        self.assertEqual(len(await store.missing_source_candidates(1)), 1)

        for _ in range(4):
            await store.mark_pricing_source_discovery_skipped(
                tool_id,
                "https://pricing-source-unreachable.example",
                "connection failed",
                retryable=True,
            )
        source = self.connection.execute(
            "SELECT discovery_status, discovery_attempts, next_discovery_at FROM pricing_sources WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(source["discovery_status"], "exhausted")
        self.assertEqual(source["discovery_attempts"], 5)
        self.assertIsNone(source["next_discovery_at"])
        self.assertEqual(await store.missing_source_candidates(1), [])

    async def test_dead_letter_pricing_task_is_not_recreated(self) -> None:
        tool_id = self.add_tool("pricing-dead-letter", status="published")
        store = runner.D1PricingStore(self.d1)
        await store.insert_pricing_source(tool_id, "https://pricing-dead-letter.example/pricing", "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        self.connection.execute(
            "UPDATE pricing_tasks SET status = 'failed', attempts = max_attempts, dead_letter_at = ? WHERE tool_id = ?",
            [runner.utc_now_iso(), tool_id],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_due_tasks(10), 0)
        task_count = self.connection.execute(
            "SELECT count(*) AS total FROM pricing_tasks WHERE tool_id = ?",
            [tool_id],
        ).fetchone()["total"]
        self.assertEqual(task_count, 1)

    async def test_failed_pricing_task_retries_same_row_after_backoff(self) -> None:
        tool_id = self.add_tool("pricing-bounded-retry", status="published")
        store = runner.D1PricingStore(self.d1)
        await store.insert_pricing_source(tool_id, "https://pricing-bounded-retry.example/pricing", "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        first = (await store.claim_due_tasks(10, lease_owner="pricing-retry-one"))[0]
        self.assertTrue(await store.finish_task(first, "failed", "temporary failure", None))
        self.connection.execute(
            "UPDATE pricing_tasks SET run_after = '2000-01-01T00:00:00Z' WHERE id = ?",
            [first.task_id],
        )
        self.connection.commit()

        self.assertEqual(await store.queue_due_tasks(10), 0)
        second = (await store.claim_due_tasks(10, lease_owner="pricing-retry-two"))[0]
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.attempts, 2)

    async def test_approved_pricing_review_materializes_once(self) -> None:
        tool_id = self.add_tool("pricing-review-flow")
        store = runner.D1PricingStore(self.d1)
        source_url = "https://pricing-review-flow.example/pricing"
        await store.insert_pricing_source(tool_id, source_url, "manual", 100)
        self.assertEqual(await store.queue_due_tasks(10), 1)
        task = (await store.claim_due_tasks(10, lease_owner="pricing-review-worker"))[0]
        result = runner.PricingFetchResult(
            url=source_url,
            final_url=source_url,
            status=200,
            content_type="text/html",
            html="<html><body>Free plan</body></html>",
        )
        snapshot_id = await store.insert_snapshot(task, result)
        payload = {
            "plans": [
                {
                    "source_plan_key": "free",
                    "name": "Free",
                    "description": "Free individual plan",
                    "audience": "individual",
                    "is_enterprise": False,
                    "prices": [
                        {
                            "kind": "recurring",
                            "amount": "0",
                            "currency": "USD",
                            "billing_interval": "monthly",
                            "commitment_interval": None,
                            "unit": None,
                            "starting_at": False,
                            "custom_quote": False,
                            "display_text": "$0",
                        }
                    ],
                }
            ]
        }
        extraction_id = await store.insert_extraction(
            snapshot_id,
            payload,
            review_status="manual_review",
            confidence=70,
            validation_errors=["human approval required"],
        )
        self.assertTrue(await store.finish_task(task, "manual_review", "human approval required", result))

        self.connection.execute("INSERT INTO app_users (id) VALUES ('pricing-reviewer')")
        self.connection.execute(
            """
            INSERT INTO pricing_extraction_reviews (extraction_id, decision, reviewer_user_id, notes)
            VALUES (?, 'approved', 'pricing-reviewer', 'approved for publication')
            """,
            [extraction_id],
        )
        self.connection.execute(
            "UPDATE pricing_extractions SET review_status = 'approved' WHERE id = ?",
            [extraction_id],
        )
        self.connection.commit()

        reviewed = await store.claim_reviewed_extractions(10)
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0].extraction_id, extraction_id)
        version_id = await store.materialize_reviewed_extraction(reviewed[0])

        materializations = self.connection.execute(
            """
            SELECT status, attempts, catalog_version_id
            FROM pricing_extraction_materializations
            WHERE extraction_id = ?
            """,
            [extraction_id],
        ).fetchall()
        self.assertEqual(len(materializations), 1)
        self.assertEqual(materializations[0]["status"], "succeeded")
        self.assertEqual(materializations[0]["attempts"], 1)
        self.assertEqual(materializations[0]["catalog_version_id"], version_id)
        catalog = self.connection.execute(
            "SELECT status FROM pricing_catalog_versions WHERE id = ?",
            [version_id],
        ).fetchone()
        self.assertEqual(catalog["status"], "active")
        pricing_task = self.connection.execute(
            "SELECT status FROM pricing_tasks WHERE id = ?",
            [task.task_id],
        ).fetchone()
        self.assertEqual(pricing_task["status"], "succeeded")
        self.assertEqual(await store.claim_reviewed_extractions(10), [])

    async def test_enrichment_promotes_pending_enrich_to_pending_review(self) -> None:
        tool_id = self.add_tool("enrichment-flow")
        category_id = self.connection.execute(
            "SELECT id FROM categories WHERE status = 'active' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        self.connection.execute("UPDATE tools SET primary_category_id = ? WHERE id = ?", [category_id, tool_id])
        self.connection.execute(
            """
            INSERT INTO tool_assets (tool_id, asset_kind, storage_bucket, storage_object_path, is_current)
            VALUES (?, 'screenshot', 'sitesimgs', 'enrichment-flow/screenshot.png', 1)
            """,
            [tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, short_description,
              feature_highlights, translation_status, published_at
            )
            VALUES (?, 'en', 'enrichment-flow', 'Enrichment Flow', 'Complete description', '[]', 'published', ?)
            """,
            [tool_id, runner.utc_now_iso()],
        )
        self.connection.execute(
            "INSERT INTO tool_key_features (tool_id, feature_name) VALUES (?, 'Feature one')",
            [tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_sources (tool_id, source_type, source_url, is_primary)
            VALUES (?, 'official_site', 'https://enrichment-flow.example', 1)
            """,
            [tool_id],
        )
        self.connection.commit()

        readiness = await runner.D1EnrichmentStore(self.d1).evaluate_tool(tool_id)

        self.assertEqual(readiness, "ready")
        tool = self.connection.execute("SELECT status FROM tools WHERE id = ?", [tool_id]).fetchone()
        self.assertEqual(tool["status"], "pending_review")
        state = self.connection.execute(
            "SELECT readiness, blocking_json FROM tool_enrichment_states WHERE tool_id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(state["readiness"], "ready")
        self.assertEqual(state["blocking_json"], "[]")

    async def test_catalog_auto_publish_publishes_ready_tool_with_audit_once(self) -> None:
        tool_id = self.add_tool("catalog-auto-publish", status="pending_review")
        self.seed_publishable_tool(tool_id, "catalog-auto-publish")
        publisher = runner.D1CatalogPublisher(self.d1, "assets-worker-test")

        result = await publisher.publish_ready(10)

        self.assertEqual(result, {"selected": 1, "published": 1, "skipped": 0})
        tool = self.connection.execute(
            "SELECT status, first_published_at FROM tools WHERE id = ?",
            [tool_id],
        ).fetchone()
        self.assertEqual(tool["status"], "published")
        self.assertTrue(tool["first_published_at"])
        changes = self.connection.execute(
            """
            SELECT change_type, old_value, new_value, verified_at, notes
            FROM tool_change_log
            WHERE tool_id = ?
            """,
            [tool_id],
        ).fetchall()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["change_type"], "status_changed")
        self.assertEqual(json.loads(changes[0]["old_value"]), {"status": "pending_review"})
        self.assertEqual(json.loads(changes[0]["new_value"]), {"status": "published"})
        self.assertTrue(changes[0]["verified_at"])
        self.assertIn(runner.CATALOG_AUTO_PUBLISH_POLICY_VERSION, changes[0]["notes"])

        replay = await publisher.publish_ready(10)
        self.assertEqual(replay, {"selected": 0, "published": 0, "skipped": 0})
        change_count = self.connection.execute(
            "SELECT count(*) FROM tool_change_log WHERE tool_id = ?",
            [tool_id],
        ).fetchone()[0]
        self.assertEqual(change_count, 1)

    async def test_catalog_auto_publish_rechecks_safety_and_live_requirements(self) -> None:
        unsafe_id = self.add_tool("catalog-auto-unsafe", status="pending_review")
        self.seed_publishable_tool(unsafe_id, "catalog-auto-unsafe")
        self.connection.execute(
            "UPDATE tools SET content_safety_status = 'needs_review' WHERE id = ?",
            [unsafe_id],
        )
        incomplete_id = self.add_tool("catalog-auto-incomplete", status="pending_review")
        now = runner.utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO tool_enrichment_states (
              tool_id, readiness, blocking_json, warnings_json, evaluated_at, updated_at
            ) VALUES (?, 'ready', '[]', '[]', ?, ?)
            """,
            [incomplete_id, now, now],
        )
        self.connection.commit()

        result = await runner.D1CatalogPublisher(
            self.d1,
            "assets-worker-test",
        ).publish_ready(10)

        self.assertEqual(result, {"selected": 0, "published": 0, "skipped": 0})
        statuses = {
            row["id"]: row["status"]
            for row in self.connection.execute(
                "SELECT id, status FROM tools WHERE id IN (?, ?)",
                [unsafe_id, incomplete_id],
            ).fetchall()
        }
        self.assertEqual(statuses[unsafe_id], "pending_review")
        self.assertEqual(statuses[incomplete_id], "pending_review")
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM tool_change_log WHERE tool_id IN (?, ?)",
                [unsafe_id, incomplete_id],
            ).fetchone()[0],
            0,
        )

    async def test_enrichment_reconciliation_promotes_after_manual_category_fix(self) -> None:
        tool_id = self.add_tool("enrichment-reconcile")
        self.connection.execute(
            """
            INSERT INTO tool_assets (tool_id, asset_kind, storage_bucket, storage_object_path, is_current)
            VALUES (?, 'screenshot', 'sitesimgs', 'enrichment-reconcile/screenshot.png', 1)
            """,
            [tool_id],
        )
        self.connection.execute(
            """
            INSERT INTO tool_localizations (
              tool_id, locale_code, localized_slug, name, short_description,
              feature_highlights, translation_status, published_at
            ) VALUES (?, 'en', 'enrichment-reconcile', 'Reconcile', 'Complete description',
                      '["Feature one"]', 'published', ?)
            """,
            [tool_id, runner.utc_now_iso()],
        )
        self.connection.execute(
            "INSERT INTO tool_sources (tool_id, source_type, source_url, is_primary) "
            "VALUES (?, 'official_site', 'https://enrichment-reconcile.example', 1)",
            [tool_id],
        )
        self.connection.commit()
        enrichment = runner.D1EnrichmentStore(self.d1)
        self.assertEqual(await enrichment.evaluate_tool(tool_id), "blocked")

        category_id = self.connection.execute(
            "SELECT id FROM categories WHERE status = 'active' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        self.connection.execute("UPDATE tools SET primary_category_id = ? WHERE id = ?", [category_id, tool_id])
        self.connection.commit()
        counts = await enrichment.reconcile_pending_tools(10)

        self.assertEqual(counts["ready"], 1)
        tool = self.connection.execute("SELECT status FROM tools WHERE id = ?", [tool_id]).fetchone()
        self.assertEqual(tool["status"], "pending_review")

    async def test_telemetry_marks_partial_failure_batch_degraded(self) -> None:
        class TelemetryConfig:
            runner_instance_id = "telemetry-partial-failure"
            runner_version = "test-version"

        telemetry = runner.RunnerTelemetry(self.d1, TelemetryConfig())
        run_id = await telemetry.start("traffic")
        await telemetry.finish(run_id, {"claimed": 1, "failed": 1})

        instance = self.connection.execute(
            "SELECT status, last_success_at, last_error FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()
        self.assertEqual(instance["status"], "degraded")
        self.assertIsNone(instance["last_success_at"])
        self.assertEqual(instance["last_error"], "Batch completed with failed=1")
        run = self.connection.execute(
            "SELECT status, error, counts_json FROM runner_runs WHERE id = ?",
            [run_id],
        ).fetchone()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "Batch completed with failed=1")
        self.assertEqual(run["counts_json"], '{"claimed": 1, "failed": 1}')

    async def test_telemetry_records_split_service_and_workload_heartbeat(self) -> None:
        class TelemetryConfig:
            runner_instance_id = "periodic-facts-test"
            runner_version = "test-version"
            runner_service_name = "periodic-facts-worker"
            runner_workloads = ("traffic", "domain_state")

        telemetry = runner.RunnerTelemetry(self.d1, TelemetryConfig(), "traffic")
        run_id = await telemetry.start("traffic")
        await telemetry.heartbeat()
        await telemetry.finish(run_id, {"claimed": 1, "done": 1})

        instance = self.connection.execute(
            "SELECT service, workloads_json, metadata_json FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()
        self.assertEqual(instance["service"], "periodic-facts-worker")
        self.assertEqual(json.loads(instance["workloads_json"]), ["traffic", "domain_state"])
        metadata = json.loads(instance["metadata_json"])
        self.assertIn("traffic", metadata["workload_heartbeats"])
        self.assertIn("process_heartbeat_at", metadata)

    async def test_service_heartbeat_registers_without_creating_a_batch_run(self) -> None:
        class TelemetryConfig:
            runner_instance_id = "taxonomy-service-heartbeat"
            runner_version = "test-version"
            runner_service_name = "taxonomy-worker"
            runner_workloads = ("taxonomy",)

        owner = self

        class FakeD1Context:
            async def __aenter__(self):
                return owner.d1

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def operation(telemetry):
            await asyncio.sleep(0.01)
            return telemetry.instance_id

        with patch.object(runner, "D1Client", return_value=FakeD1Context()):
            instance_id = await runner.run_with_service_heartbeat(
                TelemetryConfig(),
                operation,
                heartbeat_interval_seconds=30,
            )

        self.assertEqual(instance_id, TelemetryConfig.runner_instance_id)
        instance = self.connection.execute(
            "SELECT service, metadata_json FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()
        self.assertEqual(instance["service"], "taxonomy-worker")
        self.assertIn("process_heartbeat_at", json.loads(instance["metadata_json"]))
        run_count = self.connection.execute(
            "SELECT count(*) AS count FROM runner_runs WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()["count"]
        self.assertEqual(run_count, 0)

    async def test_service_schedule_records_and_clears_provider_backoff(self) -> None:
        class TelemetryConfig:
            runner_instance_id = "taxonomy-service-schedule"
            runner_version = "test-version"
            runner_service_name = "taxonomy-worker"
            runner_workloads = ("taxonomy",)

        telemetry = runner.RunnerTelemetry(self.d1, TelemetryConfig())
        await telemetry.register()
        await runner.report_service_schedule(
            telemetry,
            21600,
            backoff_reason="taxonomy_provider_blocked",
        )
        metadata = json.loads(self.connection.execute(
            "SELECT metadata_json FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()["metadata_json"])
        self.assertEqual(metadata["backoff_reason"], "taxonomy_provider_blocked")
        self.assertEqual(metadata["backoff_until"], metadata["next_poll_at"])

        await runner.report_service_schedule(telemetry, 300)
        metadata = json.loads(self.connection.execute(
            "SELECT metadata_json FROM runner_instances WHERE instance_id = ?",
            [TelemetryConfig.runner_instance_id],
        ).fetchone()["metadata_json"])
        self.assertNotIn("backoff_reason", metadata)
        self.assertNotIn("backoff_until", metadata)


if __name__ == "__main__":
    unittest.main()
