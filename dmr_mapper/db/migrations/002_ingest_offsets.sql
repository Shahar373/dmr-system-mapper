-- Schema version 2: remember how far into a growing file an ingest run got, so a raw log that
-- is still being appended to can be resumed instead of re-read (runner.ingest_log_file).
ALTER TABLE ingest_runs ADD COLUMN byte_offset INTEGER NOT NULL DEFAULT 0;
