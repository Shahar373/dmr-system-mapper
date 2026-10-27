-- Schema version 1. See DESIGN.md §3. All timestamps are UTC epoch seconds.

CREATE TABLE carriers (
  id                  INTEGER PRIMARY KEY,
  freq_hz             INTEGER NOT NULL UNIQUE,
  band                TEXT,
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  dominant_cc         INTEGER,
  variant             TEXT NOT NULL DEFAULT 'unknown',
  variant_confidence  REAL NOT NULL DEFAULT 0,
  label               TEXT,
  notes               TEXT
);

CREATE TABLE observations (
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  bucket_start        INTEGER NOT NULL,
  source              TEXT NOT NULL,
  seconds_observed    REAL NOT NULL DEFAULT 0,
  bursts              INTEGER NOT NULL DEFAULT 0,
  csbk                INTEGER NOT NULL DEFAULT 0,
  voice               INTEGER NOT NULL DEFAULT 0,
  data                INTEGER NOT NULL DEFAULT 0,
  idle                INTEGER NOT NULL DEFAULT 0,
  crc_err             INTEGER NOT NULL DEFAULT 0,
  no_sync             INTEGER NOT NULL DEFAULT 0,
  cc_dominant         INTEGER,
  cc_consistency      REAL,
  slot1_syncs         INTEGER NOT NULL DEFAULT 0,
  slot2_syncs         INTEGER NOT NULL DEFAULT 0,
  snr_db_max          REAL,
  snr_db_avg          REAL,
  rest_lsn_last       INTEGER,
  cc_histogram        TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (carrier_id, bucket_start, source)
);

CREATE TABLE csbk_events (
  id                  INTEGER PRIMARY KEY,
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  ts                  INTEGER NOT NULL,
  slot                INTEGER,
  cc                  INTEGER,
  kind                TEXT NOT NULL,
  payload             TEXT NOT NULL,
  count               INTEGER NOT NULL DEFAULT 1,
  last_ts             INTEGER NOT NULL,
  dedup_key           TEXT NOT NULL,
  UNIQUE (carrier_id, kind, dedup_key)
);
CREATE INDEX csbk_events_carrier_kind ON csbk_events(carrier_id, kind, ts);

CREATE TABLE rest_events (
  id                  INTEGER PRIMARY KEY,
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  ts                  INTEGER NOT NULL,
  site                INTEGER,
  rest_lsn            INTEGER NOT NULL,
  prev_lsn            INTEGER
);
CREATE INDEX rest_events_ts ON rest_events(ts);
CREATE INDEX rest_events_carrier ON rest_events(carrier_id, ts);

CREATE TABLE identities (
  id                  INTEGER PRIMARY KEY,
  kind                TEXT NOT NULL,
  value               INTEGER NOT NULL,
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  alias               TEXT,
  UNIQUE (kind, value)
);

CREATE TABLE identity_sightings (
  identity_id         INTEGER NOT NULL REFERENCES identities(id),
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  day                 INTEGER NOT NULL,
  count               INTEGER NOT NULL DEFAULT 0,
  roles               TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (identity_id, carrier_id, day)
);

CREATE TABLE systems (
  id                  INTEGER PRIMARY KEY,
  label               TEXT,
  variant             TEXT NOT NULL,
  sys_code            TEXT,
  net_id              INTEGER,
  confidence          REAL NOT NULL DEFAULT 0,
  created_by          TEXT NOT NULL DEFAULT 'auto',
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);

CREATE TABLE sites (
  id                  INTEGER PRIMARY KEY,
  system_id           INTEGER NOT NULL REFERENCES systems(id),
  site_number         INTEGER,
  label               TEXT,
  cc                  INTEGER,
  directly_observed   INTEGER NOT NULL DEFAULT 0,
  first_seen          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL,
  UNIQUE (system_id, site_number)
);

CREATE TABLE carrier_sites (
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  site_id             INTEGER NOT NULL REFERENCES sites(id),
  lcn                 INTEGER,
  confidence          REAL NOT NULL,
  PRIMARY KEY (carrier_id, site_id)
);

CREATE TABLE links (
  id                  INTEGER PRIMARY KEY,
  a_carrier_id        INTEGER NOT NULL REFERENCES carriers(id),
  b_carrier_id        INTEGER NOT NULL REFERENCES carriers(id),
  kind                TEXT NOT NULL,
  score               REAL NOT NULL,
  label               TEXT NOT NULL,
  tier2_only          INTEGER NOT NULL DEFAULT 0,
  updated_at          INTEGER NOT NULL,
  UNIQUE (a_carrier_id, b_carrier_id, kind),
  CHECK (a_carrier_id < b_carrier_id)
);

CREATE TABLE evidence (
  id                  INTEGER PRIMARY KEY,
  link_id             INTEGER NOT NULL REFERENCES links(id),
  type                TEXT NOT NULL,
  class               TEXT NOT NULL,
  weight              REAL NOT NULL,
  raw_score           REAL NOT NULL,
  observed_at         INTEGER NOT NULL,
  first_observed_at   INTEGER NOT NULL,
  support_count       INTEGER NOT NULL DEFAULT 1,
  details             TEXT NOT NULL,
  source              TEXT NOT NULL DEFAULT 'auto',
  UNIQUE (link_id, type, source)
);

CREATE TABLE ingest_runs (
  id                  INTEGER PRIMARY KEY,
  source              TEXT NOT NULL,
  path                TEXT,
  carrier_id          INTEGER REFERENCES carriers(id),
  started             INTEGER NOT NULL,
  finished            INTEGER,
  lines               INTEGER NOT NULL DEFAULT 0,
  parsed              INTEGER NOT NULL DEFAULT 0,
  unrecognized        INTEGER NOT NULL DEFAULT 0,
  fingerprint         TEXT
);

CREATE TABLE unrecognized_lines (
  run_id              INTEGER NOT NULL REFERENCES ingest_runs(id),
  pattern             TEXT NOT NULL,
  count               INTEGER NOT NULL,
  example             TEXT NOT NULL,
  PRIMARY KEY (run_id, pattern)
);

CREATE TABLE survey_runs (
  id                  INTEGER PRIMARY KEY,
  band                TEXT NOT NULL,
  started             INTEGER NOT NULL,
  finished            INTEGER,
  device              TEXT,
  settings            TEXT NOT NULL
);

CREATE TABLE survey_hits (
  run_id              INTEGER NOT NULL REFERENCES survey_runs(id),
  freq_hz             INTEGER NOT NULL,
  snr_db              REAL,
  p95_snr_db          REAL,
  occupancy_pct       REAL,
  width_hz            REAL,
  class               TEXT,
  PRIMARY KEY (run_id, freq_hz)
);

CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta (version) VALUES (1);
