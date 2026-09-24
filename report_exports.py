"""Read-only monthly export hook. complete.json is the durable draft event.

No provider requests, task repairs, D1 writes, or publication. An independent
renderer consumes the shared volume so chart rendering cannot stall collection.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid
from datetime import datetime, timezone

SOURCE = "similarweb"  # Private provenance, never forwarded into public data.
MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
SAFE_INTEGER = 9007199254740991
KNOWN_SECTORS = {'music-generation', 'image-generation', 'video-generation', 'speech-text-conversion', 'presentations-visualization'}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def atomic_json(file, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        if os.name != 'nt':
            descriptor = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def shift_month(month, offset):
    if not MONTH.fullmatch(month):
        raise ValueError("Invalid report month")
    year, part = map(int, month.split("-"))
    year, part = divmod(year * 12 + part - 1 + offset, 12)
    return f"{year:04d}-{part + 1:02d}"


def observations_query(supplements):
    # A single statement fixes cohort + task states + three observations to one
    # database read. Keep original category membership separate from additions.
    placeholders = ",".join("?" for _ in supplements) or "NULL"
    return f"""
    WITH candidates AS (
      SELECT t.normalized_domain AS domain, min(t.canonical_slug) AS slug,
        max(CASE WHEN term.slug = ? THEN 1 ELSE 0 END) AS category_member
      FROM tools t
      LEFT JOIN current_tool_primary_taxonomy primary_taxonomy ON primary_taxonomy.tool_id = t.id
      LEFT JOIN taxonomy_terms term ON term.id = primary_taxonomy.term_id
      WHERE t.status = 'published' AND t.content_safety_status = 'safe'
        AND t.duplicate_of_tool_id IS NULL
        AND t.verification_status IN ('verified', 'pending')
        AND t.staleness_status IN ('fresh', 'aging')
        AND (term.slug = ? OR t.normalized_domain IN ({placeholders}))
      GROUP BY t.normalized_domain
    )
    SELECT c.*, current.visits AS current_visits, previous.visits AS previous_visits,
      older.visits AS older_visits,
      current.metrics_schema_version AS current_schema,
      previous.metrics_schema_version AS previous_schema,
      older.metrics_schema_version AS older_schema,
      task.status AS current_task, baseline_task.status AS previous_task
    FROM candidates c
    LEFT JOIN domain_traffic_monthly current ON current.normalized_domain = c.domain AND current.source = ? AND current.traffic_month = ?
    LEFT JOIN domain_traffic_monthly previous ON previous.normalized_domain = c.domain AND previous.source = ? AND previous.traffic_month = ?
    LEFT JOIN domain_traffic_monthly older ON older.normalized_domain = c.domain AND older.source = ? AND older.traffic_month = ?
    LEFT JOIN traffic_tasks task ON task.normalized_domain = c.domain AND task.source = ? AND task.traffic_month = ?
    LEFT JOIN traffic_tasks baseline_task ON baseline_task.normalized_domain = c.domain AND baseline_task.source = ? AND baseline_task.traffic_month = ?
    ORDER BY c.domain LIMIT 5001
    """


def prepare_export(rows, market, month, now):
    if not 3 <= len(rows) <= 5000:
        return {"status": "blocked", "reason": "Candidate count outside 3..5000; never truncate a cohort"}, None
    gaps, seen = [], set()
    for row in rows:
        domain = row["domain"]
        if domain in seen or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", domain):
            raise ValueError("Duplicate or invalid candidate domain")
        seen.add(domain)
        for period in ("current", "previous", "older"):
            visits, schema = row.get(period + "_visits"), row.get(period + "_schema")
            if visits is not None and (type(visits) is not int or not 0 <= visits <= SAFE_INTEGER or schema != 2):
                gaps.append({"domain": domain, "period": period, "status": "blocked", "reason": "Invalid observation or schema"})
            if period == "older":
                continue
            task = row.get(period + "_task")
            if task in ("no_data", "forbidden"):
                if visits is not None:
                    gaps.append({"domain": domain, "period": period, "status": "blocked", "reason": "Terminal absence conflicts with observation"})
            elif task == "failed" or (task == "done" and visits is None):
                gaps.append({"domain": domain, "period": period, "status": "blocked", "reason": task or "Missing materialization"})
            elif task not in (None, "done") or visits is None:
                gaps.append({"domain": domain, "period": period, "status": "waiting", "reason": task or "No observation or terminal absence"})
    if gaps:
        return {"status": "blocked" if any(g["status"] == "blocked" for g in gaps) else "waiting", "gaps": gaps}, None
    peers, additions, traffic = [], [], []
    for row in rows:
        peer = {"normalizedDomain": row["domain"], "canonicalSlug": row["slug"], "visits": row["current_visits"], "previousVisits": row["previous_visits"]}
        if row["category_member"]:
            peers.append(peer)
        else:
            evidence = next(s for s in market.get("supplements", []) if s["domain"] == row["domain"])
            additions.append({"peer": peer, **{k: evidence[k] for k in ("reason", "checkedOn", "evidenceUrl")}})
        for offset, period in ((0, "current"), (-1, "previous"), (-2, "older")):
            if row[period + "_visits"] is not None:
                traffic.append({"normalized_domain": row["domain"], "traffic_month": shift_month(month, offset) + "-01", "visits": row[period + "_visits"]})
    if len(peers) < 3:
        return {"status": "blocked", "reason": "Fewer than three original category candidates"}, None
    entry = {"sector": market["sector"], "expectedDomains": len(peers), "templateVersion": "editorial-v1", "supplementalPeers": additions,
             "snapshot": {"marketSlug": market["categorySlug"], "latestMonth": month + "-01", "baselineMonth": shift_month(month, -1) + "-01", "peers": peers}, "traffic": {"rows": traffic}}
    comparable = {r["domain"] for r in rows if r["current_visits"] is not None and (r["previous_visits"] or 0) > 0}
    if market.get("scopeNotes"):
        entry["scopeNotes"] = [note for note in market["scopeNotes"] if note["domain"] in comparable]
    overrides = market.get("editions", {}).get(month, {})
    if not set(overrides) <= {"reuse", "coverageExceptions", "editorial"}:
        raise ValueError("Unknown edition override")
    entry.update(overrides)
    return {"status": "complete", "candidates": len(rows)}, {
        "schemaVersion": 2, "state": "complete", "expectedSectors": [market["sector"]], "month": month,
        "preparedOn": datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
        "preparedBy": "periodic-facts-worker / report-export-v1", "sectors": [entry]}


def write_export(directory, batch):
    directory.mkdir(parents=True, exist_ok=True)
    entry = dict(batch["sectors"][0])
    for kind in ("snapshot", "traffic"):
        digest = hashlib.sha256(encoded(entry[kind])).hexdigest()
        file = f"{kind}-{digest}.json"
        atomic_json(directory / file, entry[kind])
        entry[kind] = {"file": file, "sha256": digest}
    # Last and atomic: partial payloads can never trigger the render worker.
    atomic_json(directory / "complete.json", {**batch, "sectors": [entry]})


async def export_month(d1, root, month, markets, now, interval=21600):
    month_dir = root / month
    month_dir.mkdir(parents=True, exist_ok=True)
    state_file = month_dir / "export-status.json"
    previous = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
    configuration = hashlib.sha256(encoded(markets)).hexdigest()
    if previous.get('configuration') == configuration and previous.get("nextCheckAt", 0) > now:
        return previous
    state = {"month": month, "configuration": configuration, "checkedAt": now, "nextCheckAt": now + interval, "results": []}
    atomic_json(state_file, state)  # Persist backoff even when a query is interrupted.
    try:
        release = await d1.query("SELECT status FROM traffic_month_release_checks WHERE source = ? AND traffic_month = ?", [SOURCE, month + "-01"])
    except Exception as error:
        state['results'] = [{'sector': m['sector'], 'status': 'blocked', 'reason': str(error)[:500]} for m in markets]
        atomic_json(state_file, state)
        return state
    if not release or release[0]["status"] != "available":
        state["results"] = [{"sector": m["sector"], "status": "waiting", "reason": "Monthly release unavailable"} for m in markets]
    else:
        for market in markets:
            directory = month_dir / market["sector"]
            try:
                if (directory / "complete.json").exists():
                    result = {"status": "complete", "reason": "Existing frozen export retained"}
                else:
                    domains = [s["domain"] for s in market.get("supplements", [])]
                    params = [market["categorySlug"], market["categorySlug"], *domains]
                    for offset in (0, -1, -2, 0, -1):
                        params.extend([SOURCE, shift_month(month, offset) + "-01"])
                    rows = await d1.query(observations_query(domains), params)
                    result, batch = prepare_export(rows, market, month, now)
                    if batch is not None:
                        write_export(directory, batch)
                state["results"].append({"sector": market["sector"], **result})
            except Exception as error:
                state["results"].append({"sector": market["sector"], "status": "blocked", "reason": str(error)[:500], "errorType": type(error).__name__})
    atomic_json(state_file, state)
    return state


async def export_market_inventory(d1, root, month, now, interval=21600):
    """Bounded read-only readiness audit for the next two report markets."""
    file = root / month / 'market-inventory.json'
    if file.exists() and read_inventory_time(file) > now - interval:
        return
    state = {'month': month, 'checkedAt': now, 'status': 'checking', 'markets': []}
    atomic_json(file, state)
    try:
        sql = """
        WITH candidates AS (
          SELECT DISTINCT term.slug, term.name, t.normalized_domain AS domain
          FROM tools t
          JOIN current_tool_primary_taxonomy p ON p.tool_id = t.id
          JOIN taxonomy_terms term ON term.id = p.term_id
          WHERE t.status = 'published' AND t.content_safety_status = 'safe'
            AND t.duplicate_of_tool_id IS NULL AND t.verification_status IN ('verified', 'pending')
            AND t.staleness_status IN ('fresh', 'aging') AND term.status = 'active'
            AND (term.slug LIKE '%speech%' OR term.slug LIKE '%presentation%')
        ), measured AS (
          SELECT c.*, cur.visits AS current_visits, prev.visits AS previous_visits,
            cur.metrics_schema_version AS current_schema, prev.metrics_schema_version AS previous_schema,
            task.status AS current_task, oldtask.status AS previous_task,
            row_number() OVER (PARTITION BY c.slug ORDER BY cur.visits DESC, c.domain) AS position
          FROM candidates c
          LEFT JOIN domain_traffic_monthly cur ON cur.normalized_domain = c.domain AND cur.source = ? AND cur.traffic_month = ?
          LEFT JOIN domain_traffic_monthly prev ON prev.normalized_domain = c.domain AND prev.source = ? AND prev.traffic_month = ?
          LEFT JOIN traffic_tasks task ON task.normalized_domain = c.domain AND task.source = ? AND task.traffic_month = ?
          LEFT JOIN traffic_tasks oldtask ON oldtask.normalized_domain = c.domain AND oldtask.source = ? AND oldtask.traffic_month = ?
        )
        SELECT slug, name, count(*) AS candidates,
          sum(CASE WHEN current_visits IS NOT NULL AND previous_visits > 0 AND current_schema = 2 AND previous_schema = 2 THEN 1 ELSE 0 END) AS comparable,
          sum(CASE WHEN current_visits IS NULL THEN 1 ELSE 0 END) AS missing_current,
          sum(CASE WHEN previous_visits IS NULL THEN 1 ELSE 0 END) AS missing_baseline,
          sum(CASE WHEN (current_task IS NOT NULL AND current_task NOT IN ('done','no_data','forbidden'))
            OR (previous_task IS NOT NULL AND previous_task NOT IN ('done','no_data','forbidden')) THEN 1 ELSE 0 END) AS unsettled,
          group_concat(CASE WHEN position <= 5 THEN domain END, ',') AS leading_domains
        FROM measured GROUP BY slug, name ORDER BY comparable DESC, slug LIMIT 30
        """
        params = []
        for offset in (0, -1, 0, -1):
            params.extend([SOURCE, shift_month(month, offset) + '-01'])
        state.update(status='checked', markets=await d1.query(sql, params))
    except Exception as error:
        state.update(status='blocked', reason=type(error).__name__)
        if getattr(error, 'reason', None) == 'cost_guard_stopped':
            state['reason'] = 'cost_guard_stopped'
        if isinstance(getattr(error, 'status_code', None), int):
            state['httpStatus'] = error.status_code
    atomic_json(file, state)


def read_inventory_time(file):
    try:
        return json.loads(file.read_text('utf-8')).get('checkedAt', 0)
    except (ValueError, OSError):
        return 0


async def _export_ready_reports(d1, traffic_month, *, environ=None, now=None):
    env = os.environ if environ is None else environ
    if env.get("REPORT_EXPORT_ENABLED", "0") != "1":
        return {"report_exports_complete": 0, "report_exports_blocked": 0}
    root = Path(env["REPORT_EXPORT_ROOT"]).resolve()
    config = json.loads(Path(env.get("REPORT_MARKETS_FILE", Path(__file__).with_name("report-markets.json"))).read_text("utf-8"))
    markets = config["markets"]
    if config.get("schemaVersion") != 1 or not markets or len({m["sector"] for m in markets}) != len(markets) or any(m['sector'] not in KNOWN_SECTORS for m in markets):
        raise ValueError("Invalid report markets configuration")
    month = traffic_month[:7]
    shift_month(month, 0)
    now = time.time() if now is None else now
    if month >= datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m"):
        raise ValueError("Only closed calendar months can be exported")
    root.mkdir(parents=True, exist_ok=True)
    interval = max(300, int(env.get("REPORT_EXPORT_INTERVAL_SECONDS", "21600")))
    if env.get('REPORT_MARKET_INVENTORY_ENABLED') == '1':
        await export_market_inventory(d1, root, month, now, interval)
    months = sorted({month, *(p.name for p in root.iterdir() if p.is_dir() and MONTH.fullmatch(p.name) and p.name < month)})
    counts = {"report_exports_complete": 0, "report_exports_blocked": 0}
    for item in months:
        if all((root / item / m["sector"] / "complete.json").exists() for m in markets):
            continue
        state = await export_month(d1, root, item, markets, now, interval)
        for result in state["results"]:
            for status in ("complete", "blocked"):
                counts["report_exports_" + status] += int(result["status"] == status)
    return counts


@contextmanager
def export_lock(root):
    with (root / 'export.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


async def export_ready_reports(d1, traffic_month, *, environ=None, now=None):
    env = os.environ if environ is None else environ
    if env.get('REPORT_EXPORT_ENABLED', '0') != '1':
        return {'report_exports_complete': 0, 'report_exports_blocked': 0}
    root = Path(env['REPORT_EXPORT_ROOT']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with export_lock(root):
        return await _export_ready_reports(d1, traffic_month, environ=env, now=now)


async def check_month(month):
    """Deployment preflight: SELECTs only, no completion marker or provider call."""
    import runner
    shift_month(month, 0)
    if month >= datetime.now(timezone.utc).strftime('%Y-%m'):
        raise ValueError('Only closed calendar months can be checked')
    config = runner.load_config(require_brightdata=False)
    markets = json.loads(Path(os.environ.get('REPORT_MARKETS_FILE', Path(__file__).with_name('report-markets.json'))).read_text('utf-8'))['markets']
    results = []
    async with runner.D1Client(config) as d1:
        release = await d1.query('SELECT status FROM traffic_month_release_checks WHERE source = ? AND traffic_month = ?', [SOURCE, month + '-01'])
        if not release or release[0]['status'] != 'available':
            return {'month': month, 'status': 'waiting', 'results': [], 'databaseWrites': 0, 'providerCalls': 0}
        for market in markets:
            domains = [s['domain'] for s in market.get('supplements', [])]
            params = [market['categorySlug'], market['categorySlug'], *domains]
            for offset in (0, -1, -2, 0, -1):
                params.extend([SOURCE, shift_month(month, offset) + '-01'])
            rows = await d1.query(observations_query(domains), params)
            state, _ = prepare_export(rows, market, month, time.time())
            results.append({'sector': market['sector'], 'candidates': len(rows), **state})
    return {'month': month, 'results': results, 'databaseWrites': 0, 'providerCalls': 0}


if __name__ == '__main__':
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description='Check monthly report exports without writing data or completion events')
    parser.add_argument('--check-month', required=True)
    args = parser.parse_args()
    result = asyncio.run(check_month(args.check_month))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(2 if any(r['status'] == 'blocked' for r in result['results']) else 0)
