import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import report_exports as producer
spec = importlib.util.spec_from_file_location('draft_worker', ROOT / 'report-runtime/scripts/reports/draft-worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

NOW = 1789380000  # A closed August report in September 2026.
MARKET = {'sector': 'image-generation', 'categorySlug': 'basic-image-generation', 'supplements': []}


def rows():
    return [dict(domain=d, slug=d.split('.')[0], category_member=1,
                 current_visits=200, previous_visits=100, older_visits=90,
                 current_schema=2, previous_schema=2, older_schema=2,
                 current_task='done', previous_task='done') for d in ['a.com', 'b.com', 'midjourney.com']]


class D1:
    def __init__(self, observations=None, available=True):
        self.rows = observations or rows()
        self.available = available
        self.calls = []

    async def query(self, sql, params):
        self.calls.append((sql, params))
        assert sql.lstrip().startswith(('SELECT', 'WITH'))
        if 'traffic_month_release_checks' in sql:
            return [{'status': 'available' if self.available else 'unavailable'}]
        return copy.deepcopy(self.rows)


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_market_bypasses_old_backoff_without_rewriting_frozen_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            root, d1 = Path(temp), D1()
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW)
            frozen = root / '2026-08/image-generation/complete.json'
            before = frozen.read_bytes()
            count = len(d1.calls)
            added = {**MARKET, 'sector': 'presentations-visualization'}
            await producer.export_month(d1, root, '2026-08', [MARKET, added], NOW + 1)
            self.assertEqual(len(d1.calls), count + 2)
            self.assertEqual(frozen.read_bytes(), before)
            self.assertTrue((root / '2026-08/presentations-visualization/complete.json').exists())
            await producer.export_month(d1, root, '2026-08', [MARKET, added], NOW + 2)
            self.assertEqual(len(d1.calls), count + 2)

    async def test_market_inventory_is_read_only_and_throttles_success_and_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root, d1 = Path(temp), D1([{'slug': 'text-to-speech', 'name': 'Speech', 'candidates': 20, 'comparable': 19}])
            await producer.export_market_inventory(d1, root, '2026-08', NOW)
            result = json.loads((root / '2026-08/market-inventory.json').read_text())
            self.assertEqual(result['status'], 'checked')
            self.assertEqual(result['markets'][0]['comparable'], 19)
            self.assertEqual(d1.calls[0][1], [producer.SOURCE, '2026-08-01', producer.SOURCE, '2026-07-01'] * 2)
            await producer.export_market_inventory(d1, root, '2026-08', NOW + 1)
            self.assertEqual(len(d1.calls), 1)
            d1.query = AsyncMock(side_effect=RuntimeError('synthetic private failure'))
            await producer.export_market_inventory(d1, root, '2026-08', NOW + 21599)
            self.assertEqual(d1.query.call_count, 0)
            await producer.export_market_inventory(d1, root, '2026-08', NOW + 21600)
            await producer.export_market_inventory(d1, root, '2026-08', NOW + 21601)
            self.assertEqual(d1.query.call_count, 1)
            result = json.loads((root / '2026-08/market-inventory.json').read_text())
            self.assertEqual(result['status'], 'blocked')
            self.assertNotIn('synthetic private failure', json.dumps(result))

    async def test_disabled_hook_has_no_io(self):
        d1 = D1()
        await producer.export_ready_reports(d1, '2026-08-01', environ={})
        self.assertEqual(d1.calls, [])

    async def test_release_gate_does_not_open_early(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            d1 = D1(available=False)
            result = await producer.export_month(d1, root, '2026-08', [MARKET], NOW)
            self.assertEqual(result['results'][0]['status'], 'waiting')
            self.assertEqual(len(d1.calls), 1)
            self.assertFalse(list(root.rglob('complete.json')))

    async def test_pending_candidate_prevents_completion_even_with_other_rows(self):
        values = rows()
        values[0]['current_task'] = 'running'
        values[0]['current_visits'] = None
        with tempfile.TemporaryDirectory() as temp:
            root, d1 = Path(temp), D1(values)
            result = await producer.export_month(d1, root, '2026-08', [MARKET], NOW)
            self.assertEqual(result['results'][0]['status'], 'waiting')
            self.assertFalse(list(root.rglob('complete.json')))
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW + 10)
            self.assertEqual(len(d1.calls), 2)  # Durable six-hour backoff, no new queries.
            d1.rows = rows()
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW + 21599)
            self.assertEqual(len(d1.calls), 2)
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW + 21600)
            self.assertEqual(len(list(root.rglob('complete.json'))), 1)

    async def test_completion_hashes_and_freeze(self):
        with tempfile.TemporaryDirectory() as temp:
            root, d1 = Path(temp), D1()
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW)
            manifest = root / '2026-08/image-generation/complete.json'
            original = manifest.read_bytes()
            entry = json.loads(original)['sectors'][0]
            for kind in ['snapshot', 'traffic']:
                data = (manifest.parent / entry[kind]['file']).read_bytes()
                self.assertEqual(producer.hashlib.sha256(data).hexdigest(), entry[kind]['sha256'])
            d1.rows[0]['current_visits'] += 99
            await producer.export_month(d1, root, '2026-08', [MARKET], NOW + 21601)
            self.assertEqual(manifest.read_bytes(), original)
            self.assertEqual(sum('WITH candidates' in call[0] for call in d1.calls), 1)

    def test_terminal_absence_is_distinct_from_zero_and_uncollected_baseline(self):
        values = rows()
        values[0].update(current_visits=None, current_task='no_data')
        state, batch = producer.prepare_export(values, MARKET, '2026-08', NOW)
        self.assertEqual(state['status'], 'complete')
        self.assertIsNone(batch['sectors'][0]['snapshot']['peers'][0]['visits'])
        values[0]['current_task'] = 'done'
        self.assertEqual(producer.prepare_export(values, MARKET, '2026-08', NOW)[0]['status'], 'blocked')
        values = rows()
        values[0].update(previous_visits=None, previous_task=None)
        self.assertEqual(producer.prepare_export(values, MARKET, '2026-08', NOW)[0]['status'], 'waiting')
        values[0].update(previous_visits=0, previous_task='done')
        self.assertEqual(producer.prepare_export(values, MARKET, '2026-08', NOW)[0]['status'], 'complete')

    def test_no_truncation_invalid_schema_or_duplicate_domains(self):
        self.assertEqual(producer.prepare_export(rows() * 1667, MARKET, '2026-08', NOW)[0]['status'], 'blocked')
        values = rows()
        values[0]['current_schema'] = 1
        self.assertEqual(producer.prepare_export(values, MARKET, '2026-08', NOW)[0]['status'], 'blocked')
        with self.assertRaises(ValueError):
            producer.prepare_export(rows() + [rows()[0]], MARKET, '2026-08', NOW)

    async def test_old_waiting_month_retries_after_rollover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '2026-07').mkdir()
            config = root / 'markets.json'
            config.write_text(json.dumps({'schemaVersion': 1, 'markets': [MARKET]}))
            d1 = D1()
            await producer.export_ready_reports(d1, '2026-08-01', now=NOW, environ={'REPORT_EXPORT_ENABLED': '1', 'REPORT_EXPORT_ROOT': str(root), 'REPORT_MARKETS_FILE': str(config)})
            self.assertEqual(len(list(root.rglob('complete.json'))), 2)


class WorkerTests(unittest.TestCase):
    def test_six_hour_polling_keeps_liveness_without_rescanning(self):
        for environment, extra, interval in [({}, [], 21600),
                ({'REPORT_DRAFT_INTERVAL_SECONDS': '43200'}, [], 43200),
                ({'REPORT_DRAFT_INTERVAL_SECONDS': '60'}, ['--interval', '21600'], 21600)]:
            with self.subTest(environment=environment, extra=extra), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                clock, polls, sleeps = [0.0], [], []
                def poll(*args, **kwargs):
                    polls.append(clock[0])
                    if len(polls) == 2:
                        raise StopIteration
                    return []
                def sleep(seconds):
                    sleeps.append(seconds)
                    clock[0] += seconds
                argv = ['draft-worker.py', '--root', temp, '--exports', temp, '--state', str(root / 'state'), *extra]
                with patch.dict(worker.os.environ, environment, clear=True), patch.object(sys, 'argv', argv), \
                     patch.object(worker, 'poll', side_effect=poll), \
                     patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), \
                     patch.object(worker.time, 'sleep', side_effect=sleep), patch.object(Path, 'touch') as heartbeat:
                    with self.assertRaises(StopIteration):
                        worker.main()
                self.assertEqual(polls, [0, interval])
                self.assertEqual(sum(sleeps), interval)
                self.assertTrue(all(seconds <= 60 for seconds in sleeps))
                self.assertEqual(heartbeat.call_count, len(sleeps))

    def test_completion_event_failures_retry_restart_and_market_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, calls = Path(temp), []
            exports, state = root / 'exports', root / 'state'
            for sector in ['image-generation', 'video-generation']:
                directory = exports / '2026-08' / sector
                directory.mkdir(parents=True)
                (directory / 'snapshot.json').write_text('{}')
            def generate(root, directory, month, sector, attempt, timeout):
                calls.append(sector)
                if sector == 'video-generation':
                    raise RuntimeError('Expected unavailable evidence')
                return {'status': 'review', 'folder': '/draft', 'exitCode': 0}
            self.assertEqual(worker.poll(ROOT, exports, state, identity='v1', now=NOW, generate=generate), [])
            for sector in ['image-generation', 'video-generation']:
                (exports / '2026-08' / sector / 'complete.json').write_text('{}')
            result = worker.poll(ROOT, exports, state, identity='v1', now=NOW, generate=generate)
            self.assertEqual([r['status'] for r in result], ['review', 'blocked'])
            worker.poll(ROOT, exports, state, identity='v1', now=NOW + 2, generate=generate)
            self.assertEqual(len(calls), 2)
            worker.poll(ROOT, exports, state, identity='v1', now=NOW + 301, generate=generate)
            self.assertEqual(calls, ['image-generation', 'video-generation', 'video-generation'])
            worker.poll(ROOT, exports, state, identity='v2', now=NOW + 302, generate=generate)
            self.assertEqual(len(calls), 5)
            self.assertEqual(len(list(state.rglob('receipt.json'))), 5)

    def test_running_attempt_is_recovered_and_lock_excludes_second_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with worker.worker_lock(root):
                with self.assertRaises(OSError):
                    with worker.worker_lock(root):
                        pass
            with worker.worker_lock(root):
                pass

    def test_interrupted_attempt_replays_same_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exports, state = root / 'exports', root / 'state'
            event = exports / '2026-08/image-generation/complete.json'
            event.parent.mkdir(parents=True)
            event.write_text('{}')
            key = worker.hashlib.sha256(event.read_bytes() + b'v1').hexdigest()
            worker.atomic_json(state / '2026-08/image-generation/state.json', {'key': key, 'status': 'running', 'attempts': 1})
            results = worker.poll(ROOT, exports, state, now=NOW, identity='v1', generate=lambda *args: {'status': 'unchanged', 'folder': '/draft'})
            self.assertEqual(results[0]['attempts'], 2)
            self.assertEqual(results[0]['status'], 'unchanged')


class ActualRunnerHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_traffic_job_calls_hook_after_collection_and_isolates_failure(self):
        # Use the real entry point, with network and telemetry patched out.
        import runner
        events = []
        class Client:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        async def telemetry(*args):
            events.append('collection')
            return {'done': 3}
        async def export(*args):
            events.append('export')
            raise RuntimeError('Export storage unavailable')
        with patch.object(runner, 'D1Client', Client), patch.object(runner, 'run_with_telemetry', telemetry), patch.object(runner, 'export_ready_reports', export), patch.object(runner, 'log_error'):
            counts = await runner.run_once(object())
        self.assertEqual(events, ['collection', 'export'])
        self.assertEqual(counts['done'], 3)
        self.assertEqual(counts['report_exports_blocked'], 1)


if __name__ == '__main__':
    unittest.main()
