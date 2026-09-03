import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from taxonomy_batch import (
    OpenAIBatchClient,
    ParsedBatchResult,
    apply_request_result,
    batch_failure_error,
    build_responses_batch_line,
    enqueue_stage,
    extract_response_output_text,
    is_retryable_stage_error,
    parse_batch_output_line,
    poll_active_batches,
    resume_due_model_retries,
    response_usage,
    schedule_source_retry,
    should_escalate_l1,
    should_escalate_leaf,
    stage_policy,
    strict_json_schema,
    submit_queued_batches,
    taxonomy_retry_delay_seconds,
)
from taxonomy_shadow import (
    TaxonomyCatalog,
    TaxonomyTerm,
    leaf_adjudication_prompt,
    top2_l1_prompt,
)


class AsyncSqliteD1:
    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    async def run(self, sql, params=None, **_kwargs):
        cursor = self.connection.execute(sql, params or [])
        self.connection.commit()
        return {"last_row_id": cursor.lastrowid, "changes": cursor.rowcount}

    async def query(self, sql, params=None, **_kwargs):
        return [dict(row) for row in self.connection.execute(sql, params or []).fetchall()]


class TaxonomyBatchPureTests(unittest.TestCase):
    def test_batch_failure_error_preserves_file_validation_details(self):
        error = batch_failure_error(
            {
                "errors": {
                    "data": [
                        {
                            "code": "invalid_request",
                            "param": "file_id",
                            "message": "Cannot find file file-input.",
                        }
                    ]
                }
            },
            "failed",
        )

        self.assertEqual(
            error,
            "openai_batch_failed: invalid_request:file_id: Cannot find file file-input.",
        )

    def test_security_market_prompts_require_primary_job_evidence(self):
        roots = [
            TaxonomyTerm(
                term_id=28,
                dimension="primary_category",
                slug="ai-security-compliance",
                name="AI Detection, Security, and Compliance",
                parent_id=None,
            )
        ]
        catalog = TaxonomyCatalog(roots)
        profile = {
            "primary_job": {
                "value": "Build enterprise AI assistants securely",
                "evidence": [{"quote": "Build enterprise AI assistants securely"}],
            }
        }

        l1_prompt = top2_l1_prompt(roots, catalog, profile)
        leaf_prompt = leaf_adjudication_prompt(
            roots,
            ["ai-security-compliance"],
            catalog,
            profile,
        )

        self.assertIn("HIGH-PRECISION SECURITY RULE", l1_prompt)
        self.assertIn("supporting attributes", l1_prompt)
        self.assertIn("main sold workflow", leaf_prompt)
        self.assertIn("incidental security", leaf_prompt)

    def test_build_line_uses_responses_structured_outputs(self):
        line = build_responses_batch_line(
            custom_id="taxonomy-12-leaf-1",
            model="gpt-5.6-luna",
            prompt="pick a leaf",
            schema_name="taxonomy_leaf",
            schema={"type": "object", "properties": {}},
            reasoning_effort="high",
            max_output_tokens=4096,
        )
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["url"], "/v1/responses")
        self.assertEqual(line["body"]["reasoning"], {"effort": "high"})
        self.assertFalse(line["body"]["store"])
        output_format = line["body"]["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertTrue(output_format["strict"])
        self.assertIn("prompt_cache_key", line["body"])
        self.assertLessEqual(len(line["body"]["prompt_cache_key"]), 64)

    def test_strict_schema_requires_every_nested_property(self):
        schema = strict_json_schema(
            {
                "type": "object",
                "properties": {
                    "evidence": {
                        "type": "object",
                        "properties": {
                            "quote": {"type": "string"},
                            "node_id": {"type": "string"},
                        },
                        "required": ["quote"],
                    }
                },
                "required": [],
            }
        )
        self.assertEqual(schema["required"], ["evidence"])
        self.assertEqual(
            schema["properties"]["evidence"]["required"],
            ["quote", "node_id"],
        )
        self.assertFalse(schema["properties"]["evidence"]["additionalProperties"])

    def test_extract_and_parse_batch_result_with_usage(self):
        structured = {"leaf_slug": "image-generators", "confidence": 0.83}
        body = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": json.dumps(structured)}
                    ]
                }
            ],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 32},
                "cache_write_tokens": 7,
                "output_tokens": 40,
                "output_tokens_details": {"reasoning_tokens": 25},
                "total_tokens": 140,
            },
        }
        self.assertEqual(json.loads(extract_response_output_text(body)), structured)
        parsed = parse_batch_output_line(
            {
                "custom_id": "taxonomy-12-leaf-1",
                "response": {"status_code": 200, "body": body},
            }
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.structured_output, structured)
        self.assertEqual(parsed.usage["cached_input_tokens"], 32)
        self.assertEqual(parsed.usage["reasoning_tokens"], 25)
        self.assertEqual(response_usage(body)["cache_write_tokens"], 7)

    def test_invalid_output_is_a_failed_request(self):
        parsed = parse_batch_output_line(
            {
                "custom_id": "taxonomy-2-l1-1",
                "response": {
                    "status_code": 200,
                    "body": {"output_text": "not json"},
                },
            }
        )
        self.assertFalse(parsed.ok)
        self.assertIn("invalid_structured_output", parsed.error)

    def test_retry_policy_uses_exponential_delay_and_skips_request_defects(self):
        config = SimpleNamespace(
            taxonomy_batch_max_attempts=3,
            taxonomy_batch_retry_base_seconds=300,
        )
        self.assertEqual(taxonomy_retry_delay_seconds(config, 1), 300)
        self.assertEqual(taxonomy_retry_delay_seconds(config, 2), 600)
        self.assertTrue(is_retryable_stage_error("openai_batch_expired"))
        self.assertFalse(is_retryable_stage_error("http_400 invalid_request_error"))

    def test_escalation_is_selective(self):
        clear_hits = [
            {"confidence": 0.90},
            {"confidence": 0.60},
        ]
        close_hits = [
            {"confidence": 0.66},
            {"confidence": 0.62},
        ]
        self.assertFalse(should_escalate_l1(clear_hits, 0.08))
        self.assertTrue(should_escalate_l1(close_hits, 0.08))
        decision = {"confidence": 0.82, "evidence": [{"quote": "Creates images"}]}
        self.assertFalse(
            should_escalate_leaf(
                decision,
                clear_hits,
                min_confidence=0.60,
                min_l1_gap=0.08,
            )
        )
        self.assertTrue(
            should_escalate_leaf(
                {"confidence": 0.58, "evidence": [{"quote": "Creates images"}]},
                clear_hits,
                min_confidence=0.60,
                min_l1_gap=0.08,
            )
        )
        self.assertTrue(
            should_escalate_leaf(
                {"confidence": 0.90, "evidence": []},
                clear_hits,
                min_confidence=0.60,
                min_l1_gap=0.08,
            )
        )

    def test_stage_policy_uses_luna_and_terra_only_for_escalation(self):
        config = SimpleNamespace(taxonomy_batch_max_output_tokens=4096)
        self.assertEqual(stage_policy(config, "l1")[0], "gpt-5.6-luna")
        self.assertEqual(stage_policy(config, "l1")[1], "low")
        self.assertEqual(
            stage_policy(config, "leaf_escalation")[0], "gpt-5.6-terra"
        )
        self.assertEqual(stage_policy(config, "leaf_escalation")[1], "high")

    def test_migration_contains_resumable_state_and_usage_columns(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sigpik"
            / "d1"
            / "migrations"
            / "0077_openai_taxonomy_batch.sql"
        ).read_text(encoding="utf-8")
        for table in (
            "taxonomy_batch_items",
            "taxonomy_batch_jobs",
            "taxonomy_batch_requests",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", migration)
        self.assertIn("cached_input_tokens", migration)
        self.assertIn("reasoning_tokens", migration)
        retry_migration = (
            Path(__file__).resolve().parent.parent
            / "sigpik"
            / "d1"
            / "migrations"
            / "0079_taxonomy_batch_retry.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("retry_kind", retry_migration)
        self.assertIn("retry_attempt", retry_migration)
        self.assertIn("next_retry_at", retry_migration)
        self.assertIn("idx_taxonomy_batch_items_retry_due", retry_migration)

    def test_post_seed_legacy_backfill_preserves_trusted_primary_and_prefers_leaf(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tools (
              id INTEGER PRIMARY KEY,
              status TEXT NOT NULL,
              duplicate_of_tool_id INTEGER,
              primary_category_id INTEGER
            );
            CREATE TABLE categories (
              id INTEGER PRIMARY KEY,
              parent_category_id INTEGER
            );
            CREATE TABLE tool_categories (
              tool_id INTEGER NOT NULL,
              category_id INTEGER NOT NULL,
              source TEXT NOT NULL,
              UNIQUE(tool_id, category_id)
            );
            CREATE TABLE taxonomy_terms (
              id INTEGER PRIMARY KEY,
              dimension TEXT NOT NULL,
              parent_id INTEGER,
              source_category_id INTEGER,
              status TEXT NOT NULL,
              display_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE product_taxonomy_assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool_id INTEGER NOT NULL,
              term_id INTEGER NOT NULL,
              run_id INTEGER,
              is_primary INTEGER NOT NULL,
              confidence REAL,
              decision_status TEXT NOT NULL,
              source TEXT NOT NULL,
              evidence_json TEXT,
              assigned_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(tool_id, term_id)
            );
            CREATE TABLE taxonomy_change_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor TEXT,
              action TEXT NOT NULL,
              payload_json TEXT
            );
            INSERT INTO tools VALUES
              (1, 'pending_enrich', NULL, 10),
              (2, 'published', NULL, 10);
            INSERT INTO categories VALUES (10, NULL), (20, 10);
            INSERT INTO tool_categories VALUES
              (1, 10, 'auto'), (1, 20, 'auto'),
              (2, 10, 'auto'), (2, 20, 'auto');
            INSERT INTO taxonomy_terms VALUES
              (100, 'primary_category', NULL, 10, 'active', 10),
              (200, 'primary_category', 100, 20, 'active', 20);
            INSERT INTO product_taxonomy_assignments (
              tool_id, term_id, run_id, is_primary, confidence,
              decision_status, source, evidence_json, assigned_at, updated_at
            ) VALUES (
              2, 200, 99, 1, 0.95,
              'verified', 'manual', '{}',
              '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z'
            );
            """
        )
        migration = (
            Path(__file__).resolve().parent.parent
            / "sigpik"
            / "d1"
            / "migrations"
            / "0089_backfill_post_seed_legacy_taxonomy.sql"
        ).read_text(encoding="utf-8")
        connection.executescript(migration)

        tool_one = connection.execute(
            """
            SELECT term_id, decision_status, source
            FROM product_taxonomy_assignments
            WHERE tool_id = 1 AND is_primary = 1
            """
        ).fetchall()
        self.assertEqual(
            [(row["term_id"], row["decision_status"], row["source"]) for row in tool_one],
            [(200, "legacy", "legacy")],
        )
        tool_two = connection.execute(
            """
            SELECT term_id, decision_status, source
            FROM product_taxonomy_assignments
            WHERE tool_id = 2 AND is_primary = 1
            """
        ).fetchall()
        self.assertEqual(
            [(row["term_id"], row["decision_status"], row["source"]) for row in tool_two],
            [(200, "verified", "manual")],
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) AS total FROM product_taxonomy_assignments WHERE tool_id = 2"
            ).fetchone()["total"],
            1,
        )
        connection.close()


class OpenAIBatchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_endpoints_and_existing_key_are_used(self):
        seen = []
        file_retrievals = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal file_retrievals
            seen.append((request.method, request.url.path, request.headers))
            if request.url.path == "/v1/files":
                return httpx.Response(
                    200, json={"id": "file-input", "status": "uploaded"}
                )
            if request.url.path == "/v1/files/file-input":
                file_retrievals += 1
                return httpx.Response(
                    200,
                    json={
                        "id": "file-input",
                        "status": "uploaded" if file_retrievals == 1 else "processed",
                    },
                )
            if request.url.path == "/v1/batches" and request.method == "POST":
                return httpx.Response(
                    200, json={"id": "batch-1", "status": "validating"}
                )
            if request.url.path == "/v1/batches/batch-1":
                return httpx.Response(200, json={"id": "batch-1", "status": "completed"})
            if request.url.path == "/v1/files/file-output/content":
                return httpx.Response(200, text='{"custom_id":"one"}\n')
            return httpx.Response(404, text="missing")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAIBatchClient(
                "existing-env-key",
                file_ready_poll_seconds=0,
                client=http_client,
            )
            uploaded = await client.upload_jsonl(b"{}\n", "test.jsonl")
            batch = await client.create_batch(
                uploaded["id"], metadata={"pipeline": "test"}
            )
            await client.retrieve_batch(batch["id"])
            content = await client.download_file("file-output")
        self.assertIn('"custom_id":"one"', content)
        self.assertEqual(
            [path for _, path, _ in seen],
            [
                "/v1/files",
                "/v1/files/file-input",
                "/v1/files/file-input",
                "/v1/batches",
                "/v1/batches/batch-1",
                "/v1/files/file-output/content",
            ],
        )
        self.assertTrue(
            all(headers.get("authorization") == "Bearer existing-env-key" for _, _, headers in seen)
        )

    async def test_upload_stops_when_file_processing_fails(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-bad"})
            if request.url.path == "/v1/files/file-bad":
                return httpx.Response(
                    200,
                    json={
                        "id": "file-bad",
                        "status": "error",
                        "status_details": "invalid JSONL",
                    },
                )
            return httpx.Response(404, text="missing")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAIBatchClient("existing-env-key", client=http_client)
            with self.assertRaisesRegex(RuntimeError, "invalid JSONL"):
                await client.upload_jsonl(b"not-jsonl\n", "bad.jsonl")


class TaxonomyBatchStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE tools (
              id INTEGER PRIMARY KEY,
              entity_kind TEXT,
              entity_kind_source TEXT
            );
            CREATE TABLE taxonomy_terms (
              id INTEGER PRIMARY KEY,
              dimension TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE product_profiles (
              tool_id INTEGER PRIMARY KEY,
              profile_json TEXT NOT NULL,
              profile_version INTEGER NOT NULL,
              extracted_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE classification_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool_id INTEGER NOT NULL,
              taxonomy_version INTEGER NOT NULL,
              prompt_version TEXT NOT NULL,
              extractor_version TEXT NOT NULL,
              provider TEXT,
              model_name TEXT,
              candidate_terms_json TEXT,
              raw_output TEXT,
              run_status TEXT NOT NULL,
              error TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE product_taxonomy_assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool_id INTEGER NOT NULL,
              term_id INTEGER NOT NULL,
              run_id INTEGER,
              is_primary INTEGER NOT NULL DEFAULT 0,
              confidence REAL,
              decision_status TEXT NOT NULL,
              source TEXT NOT NULL,
              evidence_json TEXT,
              assigned_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              reviewed_at TEXT,
              UNIQUE(tool_id, term_id)
            );
            INSERT INTO tools VALUES (10, 'unresolved', 'auto');
            INSERT INTO taxonomy_terms VALUES (1, 'primary_category', 'active');
            INSERT INTO taxonomy_terms VALUES (2, 'primary_category', 'active');
            INSERT INTO taxonomy_terms VALUES (3, 'capability', 'active');
            """
        )
        migration_dir = (
            Path(__file__).resolve().parent.parent
            / "sigpik"
            / "d1"
            / "migrations"
        )
        self.connection.executescript(
            (migration_dir / "0077_openai_taxonomy_batch.sql").read_text(
                encoding="utf-8"
            )
        )
        self.connection.executescript(
            (migration_dir / "0079_taxonomy_batch_retry.sql").read_text(
                encoding="utf-8"
            )
        )
        self.d1 = AsyncSqliteD1(self.connection)
        root = TaxonomyTerm(
            term_id=1,
            dimension="primary_category",
            slug="image-generation",
            name="Image generation",
            taxonomy_version=2,
        )
        leaf = TaxonomyTerm(
            term_id=2,
            dimension="primary_category",
            slug="image-generators",
            name="Image generators",
            parent_id=1,
            parent_slug="image-generation",
            taxonomy_version=2,
        )
        capability = TaxonomyTerm(
            term_id=3,
            dimension="capability",
            slug="text-to-image",
            name="Text to image",
            taxonomy_version=2,
        )
        self.catalog = TaxonomyCatalog([root, leaf, capability])
        self.config = SimpleNamespace(
            taxonomy_batch_model="gpt-5.6-luna",
            taxonomy_batch_escalation_model="gpt-5.6-terra",
            taxonomy_batch_max_output_tokens=4096,
            taxonomy_batch_l1_min_gap=0.08,
            taxonomy_batch_leaf_min_confidence=0.60,
            taxonomy_batch_max_attempts=3,
            taxonomy_batch_retry_base_seconds=300,
            taxonomy_capabilities_enabled=True,
            taxonomy_capability_candidate_limit=96,
            taxonomy_auto_accept_confidence=0.50,
        )

    def tearDown(self):
        self.connection.close()

    async def _request(self, stage):
        row = self.connection.execute(
            "SELECT * FROM taxonomy_batch_requests WHERE stage = ? ORDER BY id DESC LIMIT 1",
            (stage,),
        ).fetchone()
        return dict(row)

    async def _apply(self, stage, output):
        request = await self._request(stage)
        return await apply_request_result(
            self.d1,
            self.config,
            self.catalog,
            ParsedBatchResult(
                custom_id=request["custom_id"],
                ok=True,
                response_body={"output_text": json.dumps(output)},
                structured_output=output,
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "cache_write_tokens": 0,
                    "output_tokens": 30,
                    "reasoning_tokens": 10,
                    "total_tokens": 130,
                },
            ),
        )

    async def test_four_stage_result_reaches_primary_and_capability(self):
        source = "Acme creates images from text. Generate art from a written prompt."
        cursor = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, source_text, source_content_hash,
              existing_entity_kind, existing_entity_source
            ) VALUES (10, 'test', 'test', 2, 'https://acme.example', ?, 'hash',
                      'unresolved', 'auto')
            """,
            (source,),
        )
        item_id = cursor.lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )
        await self._apply(
            "profile",
            {
                "entity_kind": "independent_product",
                "entity_confidence": 0.95,
                "entity_reason": "Product homepage",
                "entity_evidence": [{"quote": "Acme creates images from text."}],
                "primary_job": {
                    "value": "Create images from text",
                    "evidence": [{"quote": "Acme creates images from text."}],
                },
                "primary_outputs": [
                    {
                        "value": "Art",
                        "evidence": [
                            {"quote": "Generate art from a written prompt."}
                        ],
                    }
                ],
                "capabilities_raw": [
                    {
                        "value": "Text to image",
                        "evidence": [
                            {"quote": "Generate art from a written prompt."}
                        ],
                    }
                ],
            },
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT current_stage FROM taxonomy_batch_items WHERE id = ?", (item_id,)
            ).fetchone()[0],
            "l1",
        )
        await self._apply(
            "l1",
            {
                "l1_candidates": [
                    {
                        "slug": "image-generation",
                        "confidence": 0.93,
                        "reason": "Main market",
                    }
                ]
            },
        )
        await self._apply(
            "leaf",
            {
                "leaf_slug": "image-generators",
                "confidence": 0.88,
                "reason": "Creates images",
                "evidence": [{"quote": "Acme creates images from text."}],
                "secondary_leaves": [],
            },
        )
        self.assertIsNotNone(await self._request("capability"))
        await self._apply(
            "capability",
            {
                "capability_slugs": [
                    {
                        "slug": "text-to-image",
                        "role": "core",
                        "confidence": 0.91,
                        "evidence": [
                            {"quote": "Generate art from a written prompt."}
                        ],
                    }
                ]
            },
        )
        item = self.connection.execute(
            "SELECT status, current_stage, source_text FROM taxonomy_batch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(item), ("succeeded", "complete", None))
        assignments = self.connection.execute(
            """
            SELECT term_id, is_primary, decision_status
            FROM product_taxonomy_assignments ORDER BY term_id
            """
        ).fetchall()
        self.assertEqual([tuple(row) for row in assignments], [
            (2, 1, "auto_accepted"),
            (3, 0, "provisional"),
        ])
        run = self.connection.execute(
            "SELECT prompt_version, provider, run_status FROM classification_runs"
        ).fetchone()
        self.assertEqual(run[1:], ("openai_batch_api", "succeeded"))
        self.assertTrue(str(run[0]).startswith("openai-batch-"))

    async def test_capability_only_keeps_existing_deepseek_primary(self):
        existing_run_id = self.connection.execute(
            """
            INSERT INTO classification_runs (
              tool_id, taxonomy_version, prompt_version, extractor_version,
              provider, model_name, run_status, created_at
            ) VALUES (
              10, 2, 'deepseek-existing', 'existing-extractor',
              'browser_rendering_cleaned_text_custom_ai',
              'deepseek/deepseek-v4-flash', 'succeeded', '2026-08-24T00:00:00Z'
            )
            """
        ).lastrowid
        self.connection.execute(
            """
            INSERT INTO product_taxonomy_assignments (
              tool_id, term_id, run_id, is_primary, confidence,
              decision_status, source, evidence_json, assigned_at, updated_at
            ) VALUES (
              10, 2, ?, 1, 0.88, 'auto_accepted', 'auto', '{}',
              '2026-08-24T00:00:00Z', '2026-08-24T00:00:00Z'
            )
            """,
            (existing_run_id,),
        )
        profile = {
            "primary_job": {
                "value": "Create images from text",
                "evidence": [{"quote": "Acme creates images from text."}],
            },
            "primary_outputs": [],
            "capabilities_raw": [
                {
                    "value": "Text to image",
                    "evidence": [
                        {"quote": "Generate art from a written prompt."}
                    ],
                }
            ],
            "source_url": "https://acme.example",
            "extractor_version": "cleaned-main-content-v2-evidence-grounded-2026-08-14",
        }
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, profile_json, existing_entity_kind,
              existing_entity_source, model_trace_json
            ) VALUES (
              10, 'test-capability', 'test-capability', 2,
              'https://acme.example', ?, 'independent_product', 'auto', ?
            )
            """,
            (
                json.dumps(profile),
                json.dumps(
                    {
                        "mode": "capability_only",
                        "profile_reused": True,
                        "existing_market_slugs": ["image-generators"],
                    }
                ),
            ),
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1,
            self.config,
            self.catalog,
            item_id=item_id,
            stage="capability",
        )
        await self._apply(
            "capability",
            {
                "capability_slugs": [
                    {
                        "slug": "text-to-image",
                        "role": "core",
                        "confidence": 0.92,
                        "evidence": [
                            {"quote": "Generate art from a written prompt."}
                        ],
                    }
                ]
            },
        )
        primary = self.connection.execute(
            """
            SELECT run_id, is_primary, decision_status
            FROM product_taxonomy_assignments WHERE term_id = 2
            """
        ).fetchone()
        self.assertEqual(tuple(primary), (existing_run_id, 1, "auto_accepted"))
        capability = self.connection.execute(
            """
            SELECT is_primary, decision_status
            FROM product_taxonomy_assignments WHERE term_id = 3
            """
        ).fetchone()
        self.assertEqual(tuple(capability), (0, "provisional"))
        latest = self.connection.execute(
            """
            SELECT raw_output FROM classification_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        raw = json.loads(latest[0])
        self.assertEqual(raw["capability_backfill"], 1)
        self.assertTrue(raw["primary_preserved"])

    async def test_retryable_model_failure_retries_three_attempts_and_ignores_late_result(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, source_text, source_content_hash,
              existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'retry-test', 'retry-test', 2,
              'https://acme.example', 'Acme creates images.', 'hash',
              'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )
        first = await self._request("profile")
        applied = await apply_request_result(
            self.d1,
            self.config,
            self.catalog,
            ParsedBatchResult(
                custom_id=first["custom_id"],
                ok=False,
                response_body={},
                structured_output={},
                usage={
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                },
                error="openai_batch_expired",
            ),
        )
        self.assertTrue(applied)
        pending = self.connection.execute(
            """
            SELECT status, retry_kind, retry_attempt, next_retry_at
            FROM taxonomy_batch_items WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(pending[:3]), ("pending", "model", 1))
        self.assertIsNotNone(pending[3])

        self.connection.execute(
            "UPDATE taxonomy_batch_items SET next_retry_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (item_id,),
        )
        self.connection.commit()
        self.assertEqual(
            await resume_due_model_retries(
                self.d1, self.config, self.catalog, limit=10
            ),
            1,
        )
        second = await self._request("profile")
        self.assertEqual(second["attempt"], 2)
        late = await apply_request_result(
            self.d1,
            self.config,
            self.catalog,
            ParsedBatchResult(
                custom_id=first["custom_id"],
                ok=True,
                response_body={},
                structured_output={"entity_kind": "independent_product"},
                usage={
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                },
            ),
        )
        self.assertFalse(late)
        self.assertIsNone(
            self.connection.execute(
                "SELECT profile_json FROM taxonomy_batch_items WHERE id = ?", (item_id,)
            ).fetchone()[0]
        )

        for expected_attempt in (2, 3):
            current = await self._request("profile")
            self.assertEqual(current["attempt"], expected_attempt)
            await apply_request_result(
                self.d1,
                self.config,
                self.catalog,
                ParsedBatchResult(
                    custom_id=current["custom_id"],
                    ok=False,
                    response_body={},
                    structured_output={},
                    usage={
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                    },
                    error="temporary_connection_failure",
                ),
            )
            if expected_attempt == 2:
                self.connection.execute(
                    "UPDATE taxonomy_batch_items SET next_retry_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                    (item_id,),
                )
                self.connection.commit()
                await resume_due_model_retries(
                    self.d1, self.config, self.catalog, limit=10
                )

        terminal = self.connection.execute(
            "SELECT status, current_stage, retry_kind FROM taxonomy_batch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(terminal), ("needs_review", "complete", None))

    async def test_source_failure_uses_same_three_attempt_review_budget(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'source-retry-test', 'source-retry-test', 2,
              'https://acme.example', 'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()
        self.assertTrue(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, "timeout-1"
            )
        )
        self.assertTrue(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, "timeout-2"
            )
        )
        self.assertFalse(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, "timeout-3"
            )
        )
        terminal = self.connection.execute(
            "SELECT status, current_stage, retry_attempt FROM taxonomy_batch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(terminal), ("needs_review", "complete", 3))

    async def test_non_retryable_source_failure_goes_to_review_immediately(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'source-terminal-test', 'source-terminal-test', 2,
              'https://acme.example', 'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()

        error = RuntimeError("parked domain")
        error.retryable = False
        self.assertFalse(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, error
            )
        )
        terminal = self.connection.execute(
            "SELECT status, current_stage, retry_attempt, error "
            "FROM taxonomy_batch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(terminal[:3]), ("needs_review", "complete", 1))
        self.assertIn("non_retryable", terminal[3])

    async def test_browser_6000_source_failure_gets_one_long_retry(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'source-6000-test', 'source-6000-test', 2,
              'https://acme.example', 'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()

        error = RuntimeError("browser_run_content_api_error: 6000")
        error.retryable = True
        error.max_attempts = 2
        error.retry_after_seconds = 21600
        self.assertTrue(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, error
            )
        )
        scheduled = self.connection.execute(
            """
            SELECT status, retry_kind, retry_attempt,
                   (julianday(next_retry_at) - julianday('now')) * 86400 AS delay_seconds
            FROM taxonomy_batch_items WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(scheduled[:3]), ("pending", "source", 1))
        self.assertGreater(float(scheduled[3]), 21500)

        self.assertFalse(
            await schedule_source_retry(
                self.d1, self.config, self.catalog, item_id, error
            )
        )
        terminal = self.connection.execute(
            "SELECT status, current_stage, retry_attempt FROM taxonomy_batch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(terminal), ("needs_review", "complete", 2))

    async def test_batch_submission_failure_is_persisted_and_retried(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              source_url, source_text, source_content_hash,
              existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'submit-retry-test', 'submit-retry-test', 2,
              'https://acme.example', 'Acme creates images.', 'hash',
              'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )

        class FailingOpenAI:
            async def upload_jsonl(self, *_args, **_kwargs):
                raise RuntimeError("temporary upload connection failure")

        counts = await submit_queued_batches(
            self.d1, self.config, self.catalog, FailingOpenAI()
        )
        self.assertEqual(counts["submit_failed"], 1)
        self.assertEqual(counts["submit_retries_scheduled"], 1)
        request = await self._request("profile")
        self.assertEqual(request["status"], "failed")
        item = self.connection.execute(
            """
            SELECT status, retry_kind, retry_attempt
            FROM taxonomy_batch_items WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(item), ("pending", "model", 1))

    async def test_large_batch_reserves_requests_in_d1_safe_chunks_before_openai(self):
        request_count = 130
        for offset in range(request_count):
            tool_id = 1000 + offset
            self.connection.execute(
                "INSERT INTO tools VALUES (?, 'independent_product', 'auto')",
                (tool_id,),
            )
            item_id = self.connection.execute(
                """
                INSERT INTO taxonomy_batch_items (
                  tool_id, pipeline_version, prompt_version, taxonomy_version,
                  status, current_stage, source_url, source_text,
                  source_content_hash, existing_entity_kind,
                  existing_entity_source
                ) VALUES (?, ?, 'chunk-test', 2, 'running', 'l1',
                          'https://example.com', 'Product evidence', 'hash',
                          'independent_product', 'auto')
                """,
                [tool_id, f"chunk-test-{offset}"],
            ).lastrowid
            self.connection.execute(
                """
                INSERT INTO taxonomy_batch_requests (
                  item_id, custom_id, stage, model, reasoning_effort,
                  max_output_tokens, request_json
                ) VALUES (?, ?, 'l1', 'gpt-5.6-luna', 'low', 2048, ?)
                """,
                [item_id, f"chunk-test-{offset}", json.dumps({"offset": offset})],
            )
        self.connection.commit()

        class VariableLimitedD1(AsyncSqliteD1):
            def __init__(self, connection):
                super().__init__(connection)
                self.max_bound_parameters = 0

            async def run(self, sql, params=None, **kwargs):
                bound = params or []
                self.max_bound_parameters = max(
                    self.max_bound_parameters, len(bound)
                )
                if len(bound) > 100:
                    raise RuntimeError("too many SQL variables")
                return await super().run(sql, bound, **kwargs)

        limited_d1 = VariableLimitedD1(self.connection)
        test_case = self

        class SuccessfulOpenAI:
            def __init__(self):
                self.upload_calls = 0
                self.create_calls = 0

            async def upload_jsonl(self, *_args, **_kwargs):
                self.upload_calls += 1
                reserved = test_case.connection.execute(
                    """
                    SELECT COUNT(*) FROM taxonomy_batch_requests
                    WHERE job_id IS NOT NULL AND status = 'queued'
                    """
                ).fetchone()[0]
                test_case.assertEqual(reserved, request_count)
                return {"id": "file-chunk-test"}

            async def create_batch(self, *_args, **_kwargs):
                self.create_calls += 1
                return {"id": "batch-chunk-test", "status": "validating"}

        openai = SuccessfulOpenAI()
        counts = await submit_queued_batches(
            limited_d1, self.config, self.catalog, openai
        )
        self.assertEqual(counts["batches_submitted"], 1)
        self.assertEqual(counts["requests_submitted"], request_count)
        self.assertEqual(counts["submit_failed"], 0)
        self.assertEqual(openai.upload_calls, 1)
        self.assertEqual(openai.create_calls, 1)
        self.assertLessEqual(limited_d1.max_bound_parameters, 100)
        submitted = self.connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT job_id)
            FROM taxonomy_batch_requests WHERE status = 'submitted'
            """
        ).fetchone()
        self.assertEqual(tuple(submitted), (request_count, 1))

    async def test_post_create_checkpoint_failure_cannot_duplicate_submission(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              status, current_stage, source_url, source_text,
              source_content_hash, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'checkpoint-test', 'checkpoint-test', 2,
              'running', 'profile', 'https://acme.example',
              'Acme creates images.', 'hash', 'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )

        class CheckpointFailsOnceD1(AsyncSqliteD1):
            def __init__(self, connection):
                super().__init__(connection)
                self.fail_submitted_checkpoint = True

            async def run(self, sql, params=None, **kwargs):
                normalized = " ".join(sql.split())
                if (
                    self.fail_submitted_checkpoint
                    and "SET status = 'submitted'" in normalized
                ):
                    self.fail_submitted_checkpoint = False
                    raise RuntimeError("temporary D1 checkpoint failure")
                return await super().run(sql, params, **kwargs)

        class SuccessfulOpenAI:
            def __init__(self):
                self.upload_calls = 0
                self.create_calls = 0

            async def upload_jsonl(self, *_args, **_kwargs):
                self.upload_calls += 1
                return {"id": "file-checkpoint-test"}

            async def create_batch(self, *_args, **_kwargs):
                self.create_calls += 1
                return {"id": "batch-checkpoint-test", "status": "validating"}

            async def retrieve_batch(self, *_args, **_kwargs):
                return {
                    "id": "batch-checkpoint-test",
                    "status": "in_progress",
                    "request_counts": {"completed": 0, "failed": 0},
                }

        d1 = CheckpointFailsOnceD1(self.connection)
        first_openai = SuccessfulOpenAI()
        counts = await submit_queued_batches(
            d1, self.config, self.catalog, first_openai
        )
        self.assertEqual(counts["batches_submitted"], 1)
        self.assertEqual(counts["submission_persist_failed"], 1)
        reserved = self.connection.execute(
            """
            SELECT status, job_id FROM taxonomy_batch_requests
            WHERE item_id = ?
            """,
            [item_id],
        ).fetchone()
        self.assertEqual(reserved["status"], "queued")
        self.assertIsNotNone(reserved["job_id"])

        second_openai = SuccessfulOpenAI()
        second_counts = await submit_queued_batches(
            d1, self.config, self.catalog, second_openai
        )
        self.assertEqual(second_counts["batches_submitted"], 0)
        self.assertEqual(second_openai.upload_calls, 0)
        self.assertEqual(second_openai.create_calls, 0)

        await poll_active_batches(d1, self.config, self.catalog, first_openai)
        recovered_status = self.connection.execute(
            "SELECT status FROM taxonomy_batch_requests WHERE item_id = ?",
            [item_id],
        ).fetchone()[0]
        self.assertEqual(recovered_status, "submitted")

    async def test_failed_batch_persists_provider_detail_and_retries_request(self):
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              status, current_stage, source_url, source_text,
              source_content_hash, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'failed-batch-test', 'failed-batch-test', 2,
              'running', 'profile', 'https://acme.example',
              'Acme creates images.', 'hash', 'unresolved', 'auto'
            )
            """
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )

        class FailedBatchOpenAI:
            async def upload_jsonl(self, *_args, **_kwargs):
                return {"id": "file-failed-batch"}

            async def create_batch(self, *_args, **_kwargs):
                return {"id": "batch-failed-batch", "status": "validating"}

            async def retrieve_batch(self, *_args, **_kwargs):
                return {
                    "id": "batch-failed-batch",
                    "status": "failed",
                    "request_counts": {"total": 0, "completed": 0, "failed": 0},
                    "errors": {
                        "data": [
                            {
                                "code": "invalid_request",
                                "param": "file_id",
                                "message": "Cannot find file file-failed-batch.",
                            }
                        ]
                    },
                }

        openai = FailedBatchOpenAI()
        submitted = await submit_queued_batches(
            self.d1, self.config, self.catalog, openai
        )
        self.assertEqual(submitted["requests_submitted"], 1)

        counts = await poll_active_batches(
            self.d1, self.config, self.catalog, openai
        )
        self.assertEqual(counts["batch_requests_failed"], 1)

        job = self.connection.execute(
            "SELECT status, error FROM taxonomy_batch_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIn("invalid_request:file_id", job["error"])
        attempts = self.connection.execute(
            """
            SELECT attempt, status, error
            FROM taxonomy_batch_requests
            WHERE item_id = ? ORDER BY attempt
            """,
            [item_id],
        ).fetchall()
        self.assertEqual(
            [(row["attempt"], row["status"]) for row in attempts],
            [(1, "failed")],
        )
        self.assertIn("Cannot find file", attempts[0]["error"])
        item = self.connection.execute(
            """
            SELECT status, retry_kind, retry_attempt, next_retry_at, error
            FROM taxonomy_batch_items WHERE id = ?
            """,
            [item_id],
        ).fetchone()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["retry_kind"], "model")
        self.assertEqual(item["retry_attempt"], 1)
        self.assertIsNotNone(item["next_retry_at"])
        self.assertIn("Cannot find file", item["error"])

    async def test_batch_file_access_outage_falls_back_to_direct_responses(self):
        source = "Acme creates images from text. Generate art from a written prompt."
        item_id = self.connection.execute(
            """
            INSERT INTO taxonomy_batch_items (
              tool_id, pipeline_version, prompt_version, taxonomy_version,
              status, current_stage, source_url, source_text,
              source_content_hash, existing_entity_kind, existing_entity_source
            ) VALUES (
              10, 'sync-fallback-test', 'sync-fallback-test', 2,
              'running', 'profile', 'https://acme.example', ?,
              'hash', 'unresolved', 'auto'
            )
            """,
            [source],
        ).lastrowid
        self.connection.commit()
        await enqueue_stage(
            self.d1, self.config, self.catalog, item_id=item_id, stage="profile"
        )

        profile = {
            "entity_kind": "independent_product",
            "entity_confidence": 0.95,
            "entity_reason": "Product homepage",
            "entity_evidence": [{"quote": "Acme creates images from text."}],
            "primary_job": {
                "value": "Create images from text",
                "evidence": [{"quote": "Acme creates images from text."}],
            },
            "primary_outputs": [
                {
                    "value": "Art",
                    "evidence": [
                        {"quote": "Generate art from a written prompt."}
                    ],
                }
            ],
            "capabilities_raw": [],
        }

        class OutageBatchOpenAI:
            direct_calls = 0

            async def upload_jsonl(self, *_args, **_kwargs):
                return {"id": "file-outage"}

            async def create_batch(self, *_args, **_kwargs):
                return {"id": "batch-outage", "status": "validating"}

            async def retrieve_batch(self, *_args, **_kwargs):
                return {
                    "id": "batch-outage",
                    "status": "failed",
                    "request_counts": {"total": 0, "completed": 0, "failed": 0},
                    "errors": {
                        "data": [
                            {
                                "code": "invalid_request",
                                "param": "file_id",
                                "message": (
                                    "Cannot find file file-outage, or organization "
                                    "org-example does not have access to it."
                                ),
                            }
                        ]
                    },
                }

            async def create_response(self, payload):
                self.direct_calls += 1
                self.assert_payload = payload
                return {
                    "output_text": json.dumps(profile),
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 30,
                        "total_tokens": 130,
                    },
                }

        openai = OutageBatchOpenAI()
        submitted = await submit_queued_batches(
            self.d1, self.config, self.catalog, openai
        )
        self.assertEqual(submitted["requests_submitted"], 1)

        counts = await poll_active_batches(
            self.d1, self.config, self.catalog, openai
        )
        self.assertEqual(openai.direct_calls, 1)
        self.assertEqual(counts["sync_fallback_jobs"], 1)
        self.assertEqual(counts["sync_fallback_completed"], 1)
        self.assertEqual(counts["batch_requests_completed"], 1)

        job = self.connection.execute(
            """
            SELECT status, completed_count, failed_count, error
            FROM taxonomy_batch_jobs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["completed_count"], 1)
        self.assertEqual(job["failed_count"], 0)
        self.assertIn("sync_responses_fallback=1/1", job["error"])

        request = self.connection.execute(
            """
            SELECT status, input_tokens, output_tokens
            FROM taxonomy_batch_requests
            WHERE item_id = ? AND stage = 'profile'
            """,
            [item_id],
        ).fetchone()
        self.assertEqual(request["status"], "succeeded")
        self.assertEqual(request["input_tokens"], 100)
        self.assertEqual(request["output_tokens"], 30)
        item = self.connection.execute(
            """
            SELECT status, current_stage, profile_json
            FROM taxonomy_batch_items WHERE id = ?
            """,
            [item_id],
        ).fetchone()
        self.assertEqual(item["status"], "running")
        self.assertEqual(item["current_stage"], "l1")
        self.assertTrue(item["profile_json"])


if __name__ == "__main__":
    unittest.main()
