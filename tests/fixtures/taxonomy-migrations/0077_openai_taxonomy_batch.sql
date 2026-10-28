-- Persistent OpenAI Batch API state for the multidimensional taxonomy pipeline.
-- The runner owns this state machine; the admin application only reads the
-- resulting product_profiles, classification_runs and taxonomy assignments.

CREATE TABLE IF NOT EXISTS taxonomy_batch_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_id INTEGER NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
  pipeline_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  taxonomy_version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'running', 'succeeded', 'partial', 'failed', 'skipped',
      'needs_review'
    )),
  current_stage TEXT NOT NULL DEFAULT 'profile'
    CHECK (current_stage IN (
      'profile', 'l1', 'l1_escalation', 'leaf', 'leaf_escalation',
      'capability', 'complete'
    )),
  source_url TEXT,
  source_text TEXT,
  source_content_hash TEXT,
  existing_entity_kind TEXT,
  existing_entity_source TEXT,
  profile_json TEXT CHECK (profile_json IS NULL OR json_valid(profile_json)),
  l1_json TEXT CHECK (l1_json IS NULL OR json_valid(l1_json)),
  leaf_json TEXT CHECK (leaf_json IS NULL OR json_valid(leaf_json)),
  capability_json TEXT CHECK (
    capability_json IS NULL OR json_valid(capability_json)
  ),
  model_trace_json TEXT CHECK (
    model_trace_json IS NULL OR json_valid(model_trace_json)
  ),
  error TEXT,
  classification_run_id INTEGER REFERENCES classification_runs(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  completed_at TEXT,
  UNIQUE (tool_id, pipeline_version, prompt_version, taxonomy_version)
);

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_items_stage_status
  ON taxonomy_batch_items (current_stage, status, id);

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_items_tool
  ON taxonomy_batch_items (tool_id, id DESC);

CREATE TABLE IF NOT EXISTS taxonomy_batch_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stage TEXT NOT NULL
    CHECK (stage IN (
      'profile', 'l1', 'l1_escalation', 'leaf', 'leaf_escalation',
      'capability'
    )),
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  openai_batch_id TEXT UNIQUE,
  input_file_id TEXT,
  output_file_id TEXT,
  error_file_id TEXT,
  status TEXT NOT NULL DEFAULT 'building'
    CHECK (status IN (
      'building', 'validating', 'in_progress', 'finalizing', 'completed',
      'failed', 'expired', 'cancelling', 'cancelled'
    )),
  request_count INTEGER NOT NULL DEFAULT 0,
  completed_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  submitted_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_jobs_status
  ON taxonomy_batch_jobs (status, id);

CREATE TABLE IF NOT EXISTS taxonomy_batch_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES taxonomy_batch_items(id) ON DELETE CASCADE,
  job_id INTEGER REFERENCES taxonomy_batch_jobs(id) ON DELETE SET NULL,
  custom_id TEXT NOT NULL UNIQUE,
  stage TEXT NOT NULL
    CHECK (stage IN (
      'profile', 'l1', 'l1_escalation', 'leaf', 'leaf_escalation',
      'capability'
    )),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
  request_json TEXT NOT NULL CHECK (json_valid(request_json)),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'submitted', 'succeeded', 'failed')),
  response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
  structured_output_json TEXT CHECK (
    structured_output_json IS NULL OR json_valid(structured_output_json)
  ),
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  submitted_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (item_id, stage, attempt)
);

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_requests_queue
  ON taxonomy_batch_requests (status, stage, model, id);

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_requests_job
  ON taxonomy_batch_requests (job_id, id);
