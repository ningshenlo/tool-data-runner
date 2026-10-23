-- Offline legacy-SQL adapter fixture; schema only, no production data.
-- Captured from Sigpik migrations on 2026-10-23; used to test activation gates.
CREATE TABLE market_snapshot_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'building' CHECK (
    status IN ('building', 'candidate', 'active', 'retired', 'failed')
  ),
  traffic_source TEXT NOT NULL DEFAULT 'similarweb' CHECK (trim(traffic_source) <> ''),
  traffic_month TEXT NOT NULL CHECK (trim(traffic_month) <> ''),
  baseline_month TEXT,
  dr_source TEXT NOT NULL DEFAULT 'ahrefs' CHECK (trim(dr_source) <> ''),
  dr_baseline_days INTEGER NOT NULL DEFAULT 30 CHECK (dr_baseline_days > 0),
  catalog_eligibility_revision INTEGER NOT NULL DEFAULT 0 CHECK (
    catalog_eligibility_revision >= 0
  ),
  coverage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(coverage_json)),
  build_started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  built_at TEXT,
  activated_at TEXT,
  retired_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE market_catalog_eligibility_revision (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE traffic_month_release_checks (
  source TEXT NOT NULL CHECK (trim(source) <> ''),
  traffic_month TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unavailable' CHECK (status IN ('available', 'unavailable', 'error')),
  probe_domain TEXT NOT NULL CHECK (trim(probe_domain) <> ''),
  observed_latest_month TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_checked_at TEXT,
  next_check_at TEXT,
  available_at TEXT,
  last_error TEXT,
  response_meta_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(response_meta_json)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (source, traffic_month)
);

CREATE TABLE domain_traffic_monthly (
  normalized_domain TEXT NOT NULL CHECK (trim(normalized_domain) <> ''),
  source TEXT NOT NULL DEFAULT 'similarweb' CHECK (trim(source) <> ''),
  traffic_month TEXT NOT NULL,
  visits INTEGER CHECK (visits IS NULL OR visits >= 0),
  global_rank INTEGER CHECK (global_rank IS NULL OR global_rank > 0),
  country_rank_country TEXT,
  country_rank INTEGER CHECK (country_rank IS NULL OR country_rank > 0),
  bounce_rate REAL CHECK (bounce_rate IS NULL OR (bounce_rate >= 0 AND bounce_rate <= 1)),
  pages_per_visit REAL CHECK (pages_per_visit IS NULL OR pages_per_visit >= 0),
  avg_visit_duration_seconds INTEGER CHECK (
    avg_visit_duration_seconds IS NULL OR avg_visit_duration_seconds >= 0
  ),
  gen_ai_traffic_share REAL CHECK (gen_ai_traffic_share IS NULL OR gen_ai_traffic_share >= 0),
  ai_visits INTEGER CHECK (ai_visits IS NULL OR ai_visits >= 0),
  ai_referral_share REAL CHECK (ai_referral_share IS NULL OR ai_referral_share >= 0),
  metrics_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metrics_json)),
  metrics_schema_version INTEGER NOT NULL DEFAULT 1 CHECK (metrics_schema_version > 0),
  source_snapshot_id INTEGER REFERENCES domain_traffic_snapshots (id) ON DELETE SET NULL,
  captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (normalized_domain, source, traffic_month)
);
