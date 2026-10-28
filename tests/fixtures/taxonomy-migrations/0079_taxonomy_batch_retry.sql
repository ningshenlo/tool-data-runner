-- Bounded retry state for source capture and OpenAI Batch stage failures.
-- A retry remains resumable across runner restarts and becomes reviewable only
-- after the configured attempt budget is exhausted.

ALTER TABLE taxonomy_batch_items ADD COLUMN retry_kind TEXT
  CHECK (retry_kind IS NULL OR retry_kind IN ('source', 'model'));

ALTER TABLE taxonomy_batch_items ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0
  CHECK (retry_attempt >= 0);

ALTER TABLE taxonomy_batch_items ADD COLUMN next_retry_at TEXT;

CREATE INDEX IF NOT EXISTS idx_taxonomy_batch_items_retry_due
  ON taxonomy_batch_items (retry_kind, next_retry_at, id)
  WHERE status = 'pending' AND retry_kind IS NOT NULL;
