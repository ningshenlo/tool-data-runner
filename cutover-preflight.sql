-- Read-only checks to run against the remote ainav D1 database before enabling
-- the lease-aware tool-data-runner build.

SELECT 'asset_tasks' AS queue, status, count(*) AS tasks,
       sum(CASE WHEN status = 'processing' AND lease_token IS NULL THEN 1 ELSE 0 END) AS legacy_inflight,
       sum(CASE WHEN status IN ('failed', 'sync_failed') AND attempts >= max_attempts AND dead_letter_at IS NULL THEN 1 ELSE 0 END) AS unmarked_exhausted
FROM asset_tasks
GROUP BY status
UNION ALL
SELECT 'traffic_tasks', status, count(*),
       sum(CASE WHEN status = 'processing' AND lease_token IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN status IN ('failed', 'sync_failed') AND attempts >= max_attempts AND dead_letter_at IS NULL THEN 1 ELSE 0 END)
FROM traffic_tasks
GROUP BY status
UNION ALL
SELECT 'domain_state_tasks', status, count(*),
       sum(CASE WHEN status = 'processing' AND lease_token IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN status IN ('failed', 'sync_failed') AND attempts >= max_attempts AND dead_letter_at IS NULL THEN 1 ELSE 0 END)
FROM domain_state_tasks
GROUP BY status
UNION ALL
SELECT 'pricing_tasks', status, count(*),
       sum(CASE WHEN status = 'running' AND lease_token IS NULL THEN 1 ELSE 0 END),
       sum(CASE WHEN status = 'failed' AND attempts >= max_attempts AND dead_letter_at IS NULL THEN 1 ELSE 0 END)
FROM pricing_tasks
GROUP BY status
ORDER BY queue, status;

SELECT id, name, applied_at
FROM d1_migrations
ORDER BY id DESC
LIMIT 20;

SELECT service, instance_id, version, status, last_heartbeat_at, last_success_at, last_error
FROM runner_instances
ORDER BY last_heartbeat_at DESC
LIMIT 20;
