import inspect
import sqlite3
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import runner


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "ainav" / "d1" / "migrations"


class FakeD1:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    async def execute(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
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

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        result = await self.execute(sql, params)
        return result["results"]

    async def run(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        result = await self.execute(sql, params)
        return result["meta"]

    async def batch(self, statements: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
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
    ) -> None:
        await runner.D1Client.insert_snapshot(self, domain, task_month, status, row, error, raw_payload)

    async def upsert_tool_traffic_monthly(self, domain: str, rows: list[dict[str, Any]]) -> None:
        await runner.D1Client.upsert_tool_traffic_monthly(self, domain, rows)

    async def insert_result(self, task: runner.TrafficTask, result: runner.FetchResult) -> None:
        await runner.D1Client.insert_result(self, task, result)


class AssetExtractionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_run_json_request_uses_current_json_schema_contract(self) -> None:
        class RecordingClient(runner.CloudflareBrowserRunAssetClient):
            def __init__(self) -> None:
                self.timeout_seconds = 30
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
                self.calls.append((endpoint, body))
                return {
                    "title": "Example",
                    "description": "Example description",
                    "favicon_href": "",
                    "category_l1": "image-processing",
                    "category_l2": "image-editing",
                    "key_features": [
                        {"name": "Feature one", "description": "Description one"},
                        {"name": "Feature two", "description": "Description two"},
                        {"name": "Feature three", "description": "Description three"},
                    ],
                }

        client = RecordingClient()
        result = await client.fetch_homepage_metadata(
            "https://example.com",
            ["image-processing", "image-editing"],
        )

        self.assertEqual(result["category_l2"], "image-editing")
        self.assertEqual(len(client.calls), 1)
        endpoint, body = client.calls[0]
        self.assertEqual(endpoint, "json")
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertNotIn("schema", response_format)
        schema = response_format["json_schema"]
        self.assertEqual(schema["properties"]["key_features"]["minItems"], 3)
        self.assertEqual(schema["properties"]["key_features"]["maxItems"], 6)
        self.assertEqual(
            set(schema["required"]),
            {"title", "description", "favicon_href", "category_l1", "category_l2", "key_features"},
        )


class RunnerStoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.d1 = FakeD1(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def add_tool(self, suffix: str, *, status: str = "pending_enrich") -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO tools (canonical_slug, official_url, normalized_domain, status)
            VALUES (?, ?, ?, ?)
            """,
            [suffix, f"https://{suffix}.example", f"{suffix}.example", status],
        )
        self.connection.commit()
        return int(cursor.lastrowid)

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

    async def test_asset_queue_includes_category_only_gap(self) -> None:
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

        queued = await runner.D1AssetStore(self.d1).queue_missing_asset_tasks(10)

        self.assertEqual(queued, 1)
        row = self.task_row("asset_tasks", "tool_id = ? AND source = ?", [tool_id, runner.ASSET_SOURCE])
        self.assertEqual(row["status"], "queued")

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
            "SELECT visits FROM tool_traffic_monthly WHERE tool_id = ? AND traffic_month = ?",
            [tool_id, traffic_month],
        ).fetchone()
        self.assertEqual(monthly["visits"], 1234)

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
        }
        rows = runner.parse_monthly_rows(raw_payload, task.normalized_domain, traffic_month)
        self.assertEqual(rows[0]["top_keywords"][0]["name"], "keyword one")
        self.assertEqual(rows[0]["top_keywords"][0]["volume"], 1200)

        result = runner.FetchResult(
            status="done",
            monthly_rows=rows,
            observed_latest_month=traffic_month,
            raw_payload=raw_payload,
        )
        await self.d1.insert_result(task, result)

        monthly = self.connection.execute(
            "SELECT raw_payload FROM tool_traffic_monthly WHERE tool_id = ? AND traffic_month = ?",
            [tool_id, traffic_month],
        ).fetchone()
        self.assertIn('"top_keywords"', monthly["raw_payload"])
        snapshot = self.connection.execute(
            "SELECT raw_payload FROM domain_traffic_snapshots WHERE normalized_domain = ? ORDER BY id DESC LIMIT 1",
            [task.normalized_domain],
        ).fetchone()
        self.assertIn('"TopKeywords"', snapshot["raw_payload"])

        await self.d1.upsert_tool_traffic_monthly(
            task.normalized_domain,
            [{"traffic_month": traffic_month, "visits": 5000, "website": task.normalized_domain}],
        )
        preserved = self.connection.execute(
            "SELECT visits, json_array_length(raw_payload, '$.top_keywords') AS keyword_count "
            "FROM tool_traffic_monthly WHERE tool_id = ? AND traffic_month = ?",
            [tool_id, traffic_month],
        ).fetchone()
        self.assertEqual(preserved["visits"], 5000)
        self.assertEqual(preserved["keyword_count"], 2)

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
            ),
        )
        row = self.task_row(
            "domain_state_tasks",
            "normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        )
        self.assert_completed_lease(row, "done")
        state = self.connection.execute(
            "SELECT domain_rating FROM domain_states WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchone()
        self.assertEqual(state["domain_rating"], 42.0)
        history = self.connection.execute(
            "SELECT domain_rating, observed_date FROM domain_rating_history "
            "WHERE normalized_domain = ? AND source = ?",
            [task.normalized_domain, runner.DOMAIN_STATE_SOURCE],
        ).fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["domain_rating"], 42.0)
        self.assertEqual(len(history[0]["observed_date"]), 10)

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

        await self.d1.upsert_tool_traffic_monthly(
            "traffic-history.example",
            runner.parse_monthly_rows(first_payload, "traffic-history.example", "2026-03-01"),
        )
        await self.d1.upsert_tool_traffic_monthly(
            "traffic-history.example",
            runner.parse_monthly_rows(second_payload, "traffic-history.example", "2026-04-01"),
        )

        rows = self.connection.execute(
            "SELECT traffic_month, visits FROM tool_traffic_monthly "
            "WHERE tool_id = ? ORDER BY traffic_month",
            [tool_id],
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
        self.assertEqual(run["status"], "succeeded")
        self.assertIsNone(run["error"])
        self.assertEqual(run["counts_json"], '{"claimed": 1, "failed": 1}')


if __name__ == "__main__":
    unittest.main()
