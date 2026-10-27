-- Schema version 3: collector daemon state, sessions and the command queue (Phase 3a).
-- The daemon is the only writer of daemon_state/sessions; clients (TUI, MCP, CLI) only
-- insert into commands and read everything else.

CREATE TABLE daemon_state (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  pid                 INTEGER,
  started             INTEGER,
  heartbeat           INTEGER NOT NULL,
  mode                TEXT NOT NULL DEFAULT 'idle',   -- idle|survey|decode|stopping|offline
  session_id          INTEGER,
  sdr                 TEXT NOT NULL DEFAULT '{}',     -- JSON: status present|absent|busy|error|unknown, driver, serial, holder, checked
  power               TEXT NOT NULL DEFAULT '{}',     -- JSON PowerState
  progress            TEXT NOT NULL DEFAULT '{}',     -- JSON: what the running session is doing right now
  message             TEXT
);

CREATE TABLE sessions (
  id                  INTEGER PRIMARY KEY,
  kind                TEXT NOT NULL,                  -- survey|decode
  requested_by        TEXT NOT NULL,                  -- tui|mcp|cli
  params              TEXT NOT NULL DEFAULT '{}',     -- JSON: bands, freqs, ...
  budget_s            INTEGER NOT NULL,
  created             INTEGER NOT NULL,
  started             INTEGER,
  finished            INTEGER,
  status              TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|stopped|failed
  summary             TEXT NOT NULL DEFAULT '{}'     -- JSON: hits, carriers decoded, reasons, errors
);

CREATE TABLE commands (
  id                  INTEGER PRIMARY KEY,
  created             INTEGER NOT NULL,
  source              TEXT NOT NULL,                  -- tui|mcp|cli
  kind                TEXT NOT NULL,                  -- start_survey|start_decode|stop|correlate|export
  params              TEXT NOT NULL DEFAULT '{}',
  status              TEXT NOT NULL DEFAULT 'pending',   -- pending|taken|done|failed
  taken_at            INTEGER,
  done_at             INTEGER,
  result              TEXT
);
CREATE INDEX commands_pending ON commands(status, id);
