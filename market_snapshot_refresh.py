"""Publish a completed traffic month from the collector, never from page requests.

State and the process lock live on a shared durable volume. D1 remains the source
of truth after a crash; the state file only throttles checks and records failures.
"""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import time

from report_exports import atomic_json, encoded, export_lock, shift_month


async def month_readiness(d1, month, *, source, visible_tools_ctes):
    """One read of every eligible domain, including domains not queued yet."""
    rows = await d1.query(f"""
        WITH {visible_tools_ctes}
        SELECT tool.id, tool.normalized_domain, tool.primary_term_id,
          current.visits, current.metrics_schema_version, current.updated_at,
          previous.visits AS previous_visits, previous.updated_at AS previous_updated_at,
          task.status AS task_status, task.generation, task.updated_at AS task_updated_at,
          (SELECT revision FROM market_catalog_eligibility_revision WHERE id=1) AS catalog_revision
        FROM visible_tools tool
        LEFT JOIN domain_traffic_monthly current ON current.normalized_domain=tool.normalized_domain
          AND current.source=? AND current.traffic_month=?
        LEFT JOIN domain_traffic_monthly previous ON previous.normalized_domain=tool.normalized_domain
          AND previous.source=? AND previous.traffic_month=?
        LEFT JOIN traffic_tasks task ON task.normalized_domain=tool.normalized_domain
          AND task.source=? AND task.traffic_month=?
        ORDER BY tool.id
    """, [source, month, source, shift_month(month[:7], -1)+'-01', source, month])
    counts = dict(eligible=len(rows), measured=0, terminal_absence=0, waiting=0, blocked=0)
    for row in rows:
        visits, status = row['visits'], row['task_status']
        if visits is not None and (type(visits) is not int or visits < 0 or row['metrics_schema_version'] != 2):
            counts['blocked'] += 1
        elif status in ('no_data', 'forbidden'):
            counts['terminal_absence' if visits is None else 'blocked'] += 1
        elif status in ('failed', 'sync_failed') or (status == 'done' and visits is None):
            counts['blocked'] += 1
        elif status not in (None, 'done') or visits is None:
            counts['waiting'] += 1
        else:
            counts['measured'] += 1
    state = 'ready'
    if not counts['eligible'] or counts['blocked']:
        state = 'blocked'
    elif counts['waiting']:
        state = 'waiting'
    elif not counts['measured']:
        state = 'blocked'
    return {'status': state, **counts, 'fingerprint': hashlib.sha256(encoded(rows)).hexdigest()}


async def refresh_month(d1, *, source, visible_tools_ctes, build, activate, state, save, now):
    # A release probe says the provider month exists; readiness below proves
    # our complete serving cohort has finished collection. Never use max(raw month).
    months = await d1.query("""
        SELECT release.traffic_month FROM traffic_month_release_checks release
        WHERE release.source=? AND release.status='available' AND release.traffic_month < ?
          AND release.traffic_month > coalesce((
            SELECT max(traffic_month) FROM market_snapshot_versions
            WHERE traffic_source=? AND status='active'
          ), '')
        ORDER BY release.traffic_month DESC LIMIT 12
    """, [source, datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-01'), source])
    if not months:
        state.update(status='current')
        save(state)
        return state
    state['months'] = []
    for row in months:
        month = row['traffic_month']
        readiness = await month_readiness(d1, month, source=source, visible_tools_ctes=visible_tools_ctes)
        state['months'].append({'month': month, **readiness})
        if readiness['status'] != 'ready':
            continue
        # Build a new immutable candidate for this attempt. Activation failures
        # retain it for diagnosis; a retry rebuilds optional metrics as well.
        state.update(status='building', traffic_month=month)
        save(state)
        candidate = await build(d1, month, activate=False)
        state['snapshot_id'] = candidate['snapshot_id']
        save(state)
        after = await month_readiness(d1, month, source=source, visible_tools_ctes=visible_tools_ctes)
        if after['status'] != 'ready' or after['fingerprint'] != readiness['fingerprint']:
            state.update(status='waiting', reason='Collection or catalog changed during build')
            save(state)
            return state
        # The existing activator checks facets, country/metric coverage and the
        # catalog revision, then switches both version statuses in one D1 batch.
        result = await activate(d1, candidate['snapshot_id'])
        state.update(status='active', snapshot_id=result['snapshot_id'], coverage=result['coverage'])
        state.pop('reason', None)
        save(state)
        return state
    state['status'] = 'blocked' if any(m['status'] == 'blocked' for m in state['months']) else 'waiting'
    save(state)
    return state


async def refresh_completed_market_snapshot(d1, *, source, visible_tools_ctes, build, activate, environ=None, now=None):
    env = os.environ if environ is None else environ
    if env.get('MARKET_SNAPSHOT_AUTO_PUBLISH_ENABLED', '0') != '1':
        return {'status': 'disabled'}
    root = Path(env.get('MARKET_SNAPSHOT_STATE_ROOT', './work/market-snapshots')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    lock = ExitStack()
    try:
        lock.enter_context(export_lock(root))
    except OSError as error:
        lock.close()
        if error.errno in (11, 13, 36):
            return {'status': 'locked'}
        raise
    with lock:
        file = root / 'publication-status.json'
        previous = json.loads(file.read_text('utf-8')) if file.exists() else {}
        if previous.get('next_check_at', 0) > now:
            return {'status': 'throttled'}
        interval = max(300, int(env.get('MARKET_SNAPSHOT_CHECK_INTERVAL_SECONDS', '3600')))
        state = {'status': 'checking', 'checked_at': now, 'next_check_at': now + interval}
        save = lambda value: atomic_json(file, value)
        save(state)  # Retry is bounded even after a crash or a D1 rejection.
        try:
            return await refresh_month(d1, source=source, visible_tools_ctes=visible_tools_ctes,
                                       build=build, activate=activate, state=state, save=save, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.update(status='blocked', reason=str(error)[:1200], error_type=type(error).__name__)
            save(state)
            return state
