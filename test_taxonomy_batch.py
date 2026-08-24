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
    build_responses_batch_line,
    enqueue_stage,
    extract_response_output_text,
    is_retryable_stage_error,
    parse_batch_output_line,
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
from taxonomy_shadow import TaxonomyCatalog, TaxonomyTerm


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


class OpenAIBatchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_endpoints_and_existing_key_are_used(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.headers))
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-input"})
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
            client = OpenAIBatchClient("existing-env-key", client=http_client)
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
                "/v1/batches",
                "/v1/batches/batch-1",
                "/v1/files/file-output/content",
            ],
        )
        self.assertTrue(
            all(headers.get("authorization") == "Bearer existing-env-key" for _, _, headers in seen)
        )


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


if __name__ == "__main__":
    unittest.main()
