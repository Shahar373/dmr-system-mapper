-- Schema version 5 (Phase 4).
-- 1. Key-ups seen by a watch session: one row per transmission on a raster channel of the watched
--    slice (the correlator lines them up with Capacity Max channel updates to learn LCNs).
-- 2. Names the operator gives to systems and sites. systems/sites are rebuilt by every correlator
--    run, so a name is anchored on the on-air identity (Tier III network / site, Connect Plus net)
--    or, when there is none, on the carriers it had when it was named.

CREATE TABLE keyups (
  id                  INTEGER PRIMARY KEY,
  carrier_id          INTEGER NOT NULL REFERENCES carriers(id),
  session_id          INTEGER,
  start               REAL NOT NULL,                  -- epoch seconds
  end                 REAL NOT NULL,
  peak_snr_db         REAL,
  decoded             INTEGER NOT NULL DEFAULT 0      -- a DSD-FME was listening during it
);
CREATE INDEX keyups_start ON keyups(start);
CREATE INDEX keyups_carrier ON keyups(carrier_id, start);

CREATE TABLE names (
  id                  INTEGER PRIMARY KEY,
  kind                TEXT NOT NULL,                  -- system|site
  anchor              TEXT,                           -- e.g. tier3:Large:2, tier3:Large:2:1.3, conplus:3; NULL = by carriers
  carriers            TEXT NOT NULL DEFAULT '[]',     -- JSON list of carrier frequencies when it was named
  name                TEXT NOT NULL,
  created             INTEGER NOT NULL,
  updated             INTEGER NOT NULL
);
CREATE UNIQUE INDEX names_anchor ON names(kind, anchor) WHERE anchor IS NOT NULL;

ALTER TABLE systems ADD COLUMN anchor TEXT;
ALTER TABLE systems ADD COLUMN name TEXT;
ALTER TABLE sites ADD COLUMN anchor TEXT;
ALTER TABLE sites ADD COLUMN name TEXT;
