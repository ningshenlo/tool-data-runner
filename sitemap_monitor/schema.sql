PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sitemap_sites (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    homepage_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'blocked')),
    check_interval_sec INTEGER NOT NULL DEFAULT 3600 CHECK (check_interval_sec > 0),
    schedule_version INTEGER NOT NULL DEFAULT 1 CHECK (schedule_version > 0),
    next_check_at INTEGER NOT NULL,
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    last_checked_at INTEGER,
    last_changed_at INTEGER,
    error_streak INTEGER NOT NULL DEFAULT 0 CHECK (error_streak >= 0),
    dispatch_lease_owner TEXT,
    dispatch_lease_token TEXT,
    dispatch_lease_expires_at INTEGER,
    semantic_baseline_scan_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sitemap_sites_due
ON sitemap_sites(status, next_check_at, id);

CREATE TABLE IF NOT EXISTS sitemap_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL REFERENCES sitemap_sites(id) ON DELETE CASCADE,
    scheduled_for INTEGER NOT NULL,
    schedule_version INTEGER NOT NULL CHECK (schedule_version > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'retry', 'succeeded', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    base_error_streak INTEGER NOT NULL DEFAULT 0 CHECK (base_error_streak >= 0),
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at INTEGER,
    started_at INTEGER,
    finished_at INTEGER,
    last_error TEXT,
    dead_letter_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(site_id, scheduled_for, schedule_version)
);

CREATE INDEX IF NOT EXISTS idx_sitemap_jobs_claim
ON sitemap_jobs(status, available_at, lease_expires_at, id);

CREATE INDEX IF NOT EXISTS idx_sitemap_jobs_site_time
ON sitemap_jobs(site_id, scheduled_for DESC, id DESC);

CREATE TABLE IF NOT EXISTS sitemap_site_scans (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES sitemap_jobs(id) ON DELETE CASCADE,
    site_id TEXT NOT NULL REFERENCES sitemap_sites(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    baseline_scan_id TEXT REFERENCES sitemap_site_scans(id) ON DELETE SET NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    discovery_mode TEXT NOT NULL
        CHECK (discovery_mode IN ('explicit', 'robots', 'fallback')),
    traversal_complete INTEGER NOT NULL CHECK (traversal_complete IN (0, 1)),
    traversal_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    attempted_resource_count INTEGER NOT NULL CHECK (attempted_resource_count >= 0),
    successful_resource_count INTEGER NOT NULL CHECK (successful_resource_count >= 0),
    successful_resource_ratio REAL NOT NULL
        CHECK (successful_resource_ratio >= 0 AND successful_resource_ratio <= 1),
    resource_count_before INTEGER NOT NULL CHECK (resource_count_before >= 0),
    resource_count_after INTEGER NOT NULL CHECK (resource_count_after >= 0),
    url_count_before INTEGER NOT NULL CHECK (url_count_before >= 0),
    url_count_after INTEGER NOT NULL CHECK (url_count_after >= 0),
    raw_resource_set_hash_before TEXT,
    raw_resource_set_hash_after TEXT NOT NULL,
    normalized_resource_set_hash_before TEXT,
    normalized_resource_set_hash_after TEXT NOT NULL,
    resource_family_counts_json TEXT NOT NULL,
    resource_manifest_json TEXT NOT NULL,
    coverage_ratio REAL NOT NULL CHECK (coverage_ratio >= 0 AND coverage_ratio <= 1),
    comparability_status TEXT NOT NULL CHECK (comparability_status IN (
        'comparable', 'partial', 'resource_set_changed', 'possible_migration',
        'fetch_incomplete', 'baseline_invalid'
    )),
    is_comparable INTEGER NOT NULL CHECK (is_comparable IN (0, 1)),
    reason_codes_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    is_committed INTEGER NOT NULL DEFAULT 0 CHECK (is_committed IN (0, 1)),
    promoted_semantic_baseline INTEGER NOT NULL DEFAULT 0
        CHECK (promoted_semantic_baseline IN (0, 1)),
    committed_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(job_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_sitemap_site_scans_site_time
ON sitemap_site_scans(
    site_id, is_committed, committed_at DESC, finished_at DESC, id DESC
);

CREATE INDEX IF NOT EXISTS idx_sitemap_site_scans_job
ON sitemap_site_scans(job_id, attempt);

CREATE TABLE IF NOT EXISTS sitemap_resources (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sitemap_sites(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES sitemap_resources(id) ON DELETE SET NULL,
    url TEXT NOT NULL,
    canonical_fetch_url TEXT,
    type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (type IN ('unknown', 'sitemap_index', 'urlset', 'text')),
    etag TEXT,
    http_last_modified TEXT,
    content_hash TEXT,
    urlset_hash TEXT,
    metadata_hash TEXT,
    url_count INTEGER CHECK (url_count IS NULL OR url_count >= 0),
    current_state_key TEXT,
    last_checked_at INTEGER,
    last_changed_at INTEGER,
    force_verify_at INTEGER,
    missing_streak INTEGER NOT NULL DEFAULT 0 CHECK (missing_streak >= 0),
    error_streak INTEGER NOT NULL DEFAULT 0 CHECK (error_streak >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(site_id, url)
);

CREATE INDEX IF NOT EXISTS idx_sitemap_resources_site_parent
ON sitemap_resources(site_id, parent_id, id);

CREATE TABLE IF NOT EXISTS sitemap_runs (
    id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL REFERENCES sitemap_sites(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES sitemap_resources(id) ON DELETE CASCADE,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    http_status INTEGER,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (bytes_downloaded >= 0),
    url_count INTEGER CHECK (url_count IS NULL OR url_count >= 0),
    result TEXT NOT NULL
        CHECK (result IN ('baseline', 'not_modified', 'semantic_unchanged', 'changed', 'failed')),
    error_code TEXT,
    added_count INTEGER NOT NULL DEFAULT 0 CHECK (added_count >= 0),
    removed_count INTEGER NOT NULL DEFAULT 0 CHECK (removed_count >= 0),
    modified_count INTEGER NOT NULL DEFAULT 0 CHECK (modified_count >= 0),
    diff_key TEXT,
    state_key TEXT,
    site_scan_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sitemap_runs_resource_time
ON sitemap_runs(resource_id, started_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_sitemap_runs_site_scan
ON sitemap_runs(site_scan_id, resource_id);
