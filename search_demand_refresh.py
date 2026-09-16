"""Publish keyword projections after a complete traffic month is released.

Reuse the published market cohort. The public coverage pointer is inserted last,
after observations, labels, metrics and taxonomy rows validate. Only unpublished
months are rebuilt; interrupted attempts are safe to retry.
"""
import asyncio
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time

from report_exports import atomic_json, encoded, export_lock, shift_month
from search_demand_brand import classify

PAGE_SIZE = 100
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def chunks(rows):
    for start in range(0, len(rows), PAGE_SIZE):
        yield rows[start:start + PAGE_SIZE]


def number(value, *, integer=False):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return 0 if integer else None
    if not math.isfinite(result) or result < 0:
        return 0 if integer else None
    if result > 9007199254740991:
        raise ValueError('Keyword metric exceeds the safe integer range')
    return math.floor(result + .5) if integer else result


def normalize_keyword(value):
    # Preserve the existing SQLite lower(trim()) identity (ASCII lower only).
    return value.strip(' ').translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))


async def source_rows(d1, snapshot, source):
    rows, cursor = [], 0
    while True:
        page = await d1.query('''
          SELECT cohort.tool_id AS product_id, cohort.normalized_domain,
            cohort.primary_term_id, cohort.visits, traffic.source_snapshot_id, traffic.captured_at,
            json_extract(traffic.metrics_json, '$.top_search_keywords') AS keywords_json,
            json_extract(traffic.metrics_json, '$.traffic_sources.direct') AS direct_share
          FROM tool_market_snapshots cohort JOIN tools tool ON tool.id=cohort.tool_id
          LEFT JOIN domain_traffic_monthly traffic ON traffic.normalized_domain=cohort.normalized_domain
            AND traffic.source=? AND traffic.traffic_month=?
          WHERE cohort.snapshot_id=? AND cohort.tool_id>?
            AND tool.status='published' AND tool.content_safety_status='safe' AND tool.duplicate_of_tool_id IS NULL
          ORDER BY cohort.tool_id LIMIT ?
        ''', [source, snapshot['traffic_month'], snapshot['id'], cursor, PAGE_SIZE], operation='search_demand.source.read')
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        cursor = page[-1]['product_id']


async def make_plan(d1, snapshot, source):
    rows = await source_rows(d1, snapshot, source)
    grouped, observations, source_limit = defaultdict(list), [], 0
    for row in rows:
        if row['visits'] is not None and not row['captured_at']:
            raise RuntimeError('Published traffic cohort has missing source data')
        keywords = json.loads(row['keywords_json']) if row['keywords_json'] else []
        if not isinstance(keywords, list):
            raise RuntimeError('Keyword source is not an array')
        source_limit = max(source_limit, len(keywords))
        seen = set()
        for position, item in enumerate(keywords, 1):
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                continue
            key = normalize_keyword(item['name'])
            if not key or key in seen:
                continue
            seen.add(key)
            observation = dict(normalized_keyword=key, keyword=item['name'].strip(' '),
                product_id=row['product_id'], primary_term_id=row['primary_term_id'],
                normalized_domain=row['normalized_domain'], keyword_position=position,
                observed_traffic=number(item.get('estimated_traffic'), integer=True),
                search_volume=number(item.get('volume'), integer=True), cpc=number(item.get('cpc')),
                source_snapshot_id=row['source_snapshot_id'], captured_at=row['captured_at'], direct_share=row['direct_share'])
            observations.append(observation)
            grouped[key].append(observation)
    if not observations:
        raise RuntimeError('No keyword observations; preserve the previous published month')
    sectors = await d1.query('''WITH RECURSIVE tree(term_id,sector_id) AS (
      SELECT id,id FROM taxonomy_terms WHERE dimension='primary_category' AND status='active' AND parent_id IS NULL
      UNION ALL SELECT child.id,parent.sector_id FROM taxonomy_terms child JOIN tree parent ON child.parent_id=parent.term_id
      WHERE child.dimension='primary_category' AND child.status='active') SELECT * FROM tree ORDER BY term_id
    ''', operation='search_demand.taxonomy.read')
    sector_ids = {row['term_id']: row['sector_id'] for row in sectors}
    keywords, metrics, terms, labels = [], [], [], []
    for key, group in sorted(grouped.items()):
        captured = max(row['captured_at'] for row in group)
        keywords.append(dict(normalized_keyword=key, display_keyword=min(row['keyword'] for row in group)))
        totals = defaultdict(list)
        for row in group:
            if row['primary_term_id'] is not None:
                totals[row['primary_term_id']].append(row)
        primary = min(totals, key=lambda term: (-sum(row['observed_traffic'] for row in totals[term]), term)) if totals else None
        metrics.append(dict(normalized_keyword=key, primary_term_id=primary,
            observed_traffic=sum(row['observed_traffic'] for row in group), search_volume=max(row['search_volume'] for row in group),
            observed_products=len(group), observed_markets=len({sector_ids[row['primary_term_id']] for row in group if row['primary_term_id'] in sector_ids}),
            captured_at=captured))
        labels.append(dict(normalized_keyword=key, **classify(group)))
        for term, members in sorted(totals.items()):
            terms.append(dict(normalized_keyword=key, term_id=term, observed_traffic=sum(row['observed_traffic'] for row in members),
                observed_products=len(members), captured_at=max(row['captured_at'] for row in members)))
    summary = dict(observed_terms=len(keywords), domain_coverage=len({row['normalized_domain'] for row in observations}),
        product_coverage=len({row['product_id'] for row in observations}),
        market_coverage=len({sector_ids[row['primary_term_id']] for row in observations if row['primary_term_id'] in sector_ids}),
        source_limit=source_limit, captured_at=max(row['captured_at'] for row in observations),
        observations=len(observations), term_rows=len(terms), traffic=sum(row['observed_traffic'] for row in observations))
    fingerprint = hashlib.sha256(encoded([rows, sectors])).hexdigest()
    return dict(month=snapshot['traffic_month'], snapshot_id=snapshot['id'], fingerprint=fingerprint,
                keywords=keywords, observations=observations, metrics=metrics, terms=terms, labels=labels, summary=summary)


async def published_month(d1, source):
    rows = await d1.query('SELECT max(observed_month) AS month FROM search_demand_coverage_snapshots WHERE source=?',
                          [source], operation='search_demand.publication.read')
    return rows[0]['month'] if rows else None


async def clear_unpublished(d1, month, source):
    for table in ('search_keyword_term_snapshots', 'search_keyword_monthly_metrics', 'search_keyword_observations'):
        while True:
            rows = await d1.query(f'SELECT rowid AS row_id FROM {table} WHERE observed_month=? AND source=? LIMIT ?',
                                 [month, source, PAGE_SIZE], operation='search_demand.retry.read')
            if not rows:
                break
            result = await d1.execute(f'''DELETE FROM {table} WHERE rowid IN (SELECT value FROM json_each(?))
              AND NOT EXISTS(SELECT 1 FROM search_demand_coverage_snapshots WHERE source=? AND observed_month>=?)''',
                [json.dumps([r['row_id'] for r in rows]), source, month], operation='search_demand.retry.clean')
            if result.get('meta', {}).get('changes', 0) != len(rows):
                raise RuntimeError('Publication changed during retry cleanup')


async def insert_rows(d1, table, rows, columns, month, source, *, extra_columns='', extra_values='', suffix=''):
    names = ','.join(columns)
    values = ','.join("json_extract(item.value, '$." + column + "')" for column in columns)
    sql = f'''INSERT INTO {table}(keyword_id,{names}{extra_columns})
      SELECT keyword.id,{values}{extra_values} FROM json_each(?) item
      JOIN search_keywords keyword ON keyword.normalized_keyword=json_extract(item.value,'$.normalized_keyword')
      WHERE NOT EXISTS(SELECT 1 FROM search_demand_coverage_snapshots WHERE source=? AND observed_month>=?) {suffix}'''
    for page in chunks(rows):
        await d1.execute(sql, [json.dumps(page, ensure_ascii=False), source, month], operation='search_demand.' + table + '.write')


async def validate(d1, plan, source):
    month, summary = plan['month'], plan['summary']
    observations = (await d1.query('''SELECT count(*) AS observations,count(DISTINCT keyword_id) AS observed_terms,
      count(DISTINCT product_id) AS product_coverage,count(DISTINCT normalized_domain) AS domain_coverage,
      sum(observed_traffic) AS traffic FROM search_keyword_observations WHERE observed_month=? AND source=?''',
      [month, source], operation='search_demand.validate.observations'))[0]
    if any(observations[key] != summary[key] for key in observations):
        raise RuntimeError('Observation coverage does not match the source plan')
    counts = (await d1.query('''SELECT count(*) AS terms,sum(observed_traffic) AS traffic,sum(observed_products) AS observations,
      sum(CASE WHEN label.id IS NULL THEN 1 ELSE 0 END) AS missing_labels
      FROM search_keyword_monthly_metrics metrics LEFT JOIN search_keyword_brand_labels label
        ON label.keyword_id=metrics.keyword_id AND label.is_current=1 WHERE metrics.observed_month=? AND metrics.source=?''',
      [month, source], operation='search_demand.validate.metrics'))[0]
    if counts != dict(terms=summary['observed_terms'], traffic=summary['traffic'], observations=summary['observations'], missing_labels=0):
        raise RuntimeError('Monthly metric totals or keyword labels are incomplete')
    # Every persisted value must match, not just totals that could hide swapped rows.
    for table, expected, columns in (
        ('search_keyword_observations',plan['observations'],['product_id','primary_term_id','normalized_domain','keyword_position','observed_traffic','search_volume','cpc','source_snapshot_id','captured_at']),
        ('search_keyword_monthly_metrics',plan['metrics'],['primary_term_id','observed_traffic','search_volume','observed_products','observed_markets','captured_at']),
        ('search_keyword_term_snapshots',plan['terms'],['term_id','observed_traffic','observed_products','captured_at']),
    ):
        count = (await d1.query(f'SELECT count(*) AS count FROM {table} WHERE observed_month=? AND source=?', [month,source],
                               operation='search_demand.validate.count'))[0]['count']
        if count != len(expected):
            raise RuntimeError('Incomplete taxonomy/metric projection')
        for page in chunks(expected):
            comparisons = ' AND '.join(f"actual.{column} IS json_extract(item.value,'$.{column}')" for column in columns)
            result = await d1.query(f'''SELECT count(*) AS count FROM json_each(?) item
              JOIN search_keywords keyword ON keyword.normalized_keyword=json_extract(item.value,'$.normalized_keyword')
              JOIN {table} actual ON actual.keyword_id=keyword.id AND actual.observed_month=? AND actual.source=?
              WHERE {comparisons}''', [json.dumps(page,ensure_ascii=False),month,source], operation='search_demand.validate.rows')
            if result[0]['count'] != len(page):
                raise RuntimeError('Persisted projection differs from its source plan')


async def publish_month(d1, snapshot, source, *, dry_run=False, progress=None):
    month = snapshot['traffic_month']
    shift_month(month[:7], 0)
    if month != month[:7] + '-01' or month >= datetime.now(timezone.utc).strftime('%Y-%m-01'):
        raise ValueError('Only closed calendar months can be published')
    latest = await published_month(d1, source)
    if latest and latest >= month:
        return dict(status='current', month=latest)
    versions = await d1.query('''SELECT id FROM market_snapshot_versions WHERE id=? AND traffic_source=?
      AND traffic_month=? AND status IN ('active','retired') AND activated_at IS NOT NULL''',
      [snapshot['id'],source,month], operation='search_demand.release.verify')
    if not versions:
        raise RuntimeError('Traffic month has not passed the existing publication gates')
    plan = await make_plan(d1, snapshot, source)
    if dry_run:
        return dict(status='ready',month=month,snapshot_id=snapshot['id'],fingerprint=plan['fingerprint'],**plan['summary'])
    if progress:
        progress(dict(stage='building',month=month,**plan['summary']))
    await clear_unpublished(d1, month, source)
    for page in chunks(plan['keywords']):
        await d1.execute(f'''INSERT INTO search_keywords(normalized_keyword,display_keyword,first_seen_month,last_seen_month)
          SELECT json_extract(value,'$.normalized_keyword'),json_extract(value,'$.display_keyword'),?,? FROM json_each(?) WHERE true
          ON CONFLICT(normalized_keyword) DO UPDATE SET first_seen_month=min(search_keywords.first_seen_month,excluded.first_seen_month),
            last_seen_month=max(search_keywords.last_seen_month,excluded.last_seen_month),updated_at={NOW_SQL}
          WHERE search_keywords.first_seen_month>excluded.first_seen_month OR search_keywords.last_seen_month<excluded.last_seen_month''',
          [month,month,json.dumps(page,ensure_ascii=False)], operation='search_demand.keywords.write')
    for rows in (plan['observations'],plan['metrics'],plan['terms']):
        for row in rows:
            row.update(observed_month=month,source=source)
    await insert_rows(d1,'search_keyword_observations',plan['observations'],
        ['product_id','primary_term_id','observed_month','source','normalized_domain','keyword_position','observed_traffic','search_volume','cpc','source_snapshot_id','captured_at'],month,source)
    for row in plan['labels']:
        row['effective_from_month'] = month
    await insert_rows(d1,'search_keyword_brand_labels',plan['labels'],
        ['demand_type','matched_tool_id','classification_method','confidence','effective_from_month'],month,source,
        suffix='AND NOT EXISTS(SELECT 1 FROM search_keyword_brand_labels current WHERE current.keyword_id=keyword.id AND current.is_current=1)')
    await insert_rows(d1,'search_keyword_monthly_metrics',plan['metrics'],
        ['primary_term_id','observed_month','source','observed_traffic','search_volume','observed_products','observed_markets','captured_at'],month,source,
        extra_columns=',product_change_1m', extra_values=",(SELECT json_extract(item.value,'$.observed_products')-previous.observed_products FROM search_keyword_monthly_metrics previous WHERE previous.keyword_id=keyword.id AND previous.source=json_extract(item.value,'$.source') AND previous.observed_month=date(json_extract(item.value,'$.observed_month'),'-1 month'))")
    await insert_rows(d1,'search_keyword_term_snapshots',plan['terms'],
        ['term_id','observed_month','source','observed_traffic','observed_products','captured_at'],month,source)
    if progress:
        progress(dict(stage='validating',month=month))
    await validate(d1,plan,source)
    recheck = await make_plan(d1,snapshot,source)
    if recheck['fingerprint'] != plan['fingerprint']:
        raise RuntimeError('Source changed during build; preserve the previous month and retry')
    summary = plan['summary']
    fields = ['observed_terms','domain_coverage','product_coverage','market_coverage','source_limit','captured_at']
    # One final insert is the publication boundary. The route discovers months here.
    result = await d1.execute(f'''INSERT INTO search_demand_coverage_snapshots(observed_month,source,{','.join(fields)})
      SELECT ?,?,{','.join('?' for _ in fields)} WHERE NOT EXISTS(
        SELECT 1 FROM search_demand_coverage_snapshots WHERE source=? AND observed_month>=?)
        AND EXISTS(SELECT 1 FROM market_snapshot_versions WHERE id=? AND traffic_source=? AND traffic_month=?
          AND status IN ('active','retired') AND activated_at IS NOT NULL)
        AND (SELECT count(*) FROM search_keyword_monthly_metrics WHERE observed_month=? AND source=?)=?
        AND (SELECT count(*) FROM search_keyword_observations WHERE observed_month=? AND source=?)=?''',
        [month,source,*[summary[f] for f in fields],source,month,snapshot['id'],source,month,
         month,source,summary['observed_terms'],month,source,summary['observations']], operation='search_demand.publication.commit')
    if result.get('meta',{}).get('changes') != 1:
        raise RuntimeError('Publication preconditions changed; no month was activated')
    return dict(status='active',month=month,snapshot_id=snapshot['id'],fingerprint=plan['fingerprint'],**summary)


async def refresh_completed_search_demand(d1, *, source, environ=None, now=None):
    env = os.environ if environ is None else environ
    if env.get('SEARCH_DEMAND_AUTO_PUBLISH_ENABLED',env.get('MARKET_SNAPSHOT_AUTO_PUBLISH_ENABLED','0')) != '1':
        return {'status':'disabled'}
    root = Path(env.get('SEARCH_DEMAND_STATE_ROOT',str(Path(env.get('MARKET_SNAPSHOT_STATE_ROOT','./work/market-snapshots')) / 'search-demand'))).resolve()
    root.mkdir(parents=True,exist_ok=True)
    now = time.time() if now is None else now
    lock = ExitStack()
    try:
        lock.enter_context(export_lock(root))
    except OSError as error:
        lock.close()
        if error.errno in (11,13,36):
            return {'status':'locked'}
        raise
    with lock:
        file = root / 'publication-status.json'
        previous = json.loads(file.read_text('utf-8')) if file.exists() else {}
        if previous.get('next_check_at',0) > now:
            return {'status':'throttled'}
        state = dict(status='checking',checked_at=now,next_check_at=now+max(300,int(env.get('SEARCH_DEMAND_CHECK_INTERVAL_SECONDS','3600'))))
        def save(value):
            state.update(value)
            atomic_json(file,state)
        save({})
        try:
            candidates = await d1.query('''SELECT traffic_month,max(id) AS id FROM market_snapshot_versions
              WHERE traffic_source=? AND status IN ('active','retired') AND activated_at IS NOT NULL AND traffic_month<?
                AND traffic_month>coalesce((SELECT max(observed_month) FROM search_demand_coverage_snapshots WHERE source=?),'')
              GROUP BY traffic_month ORDER BY traffic_month LIMIT 1''',
              [source,datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-01'),source], operation='search_demand.publication.candidates')
            result = await publish_month(d1,candidates[0],source,progress=save) if candidates else {'status':'current'}
            save(result)
            return state
        except asyncio.CancelledError:
            raise
        except Exception as error:
            save(dict(status='blocked',reason=str(error)[:1200],error_type=type(error).__name__))
            return state
