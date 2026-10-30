-- Schema version 4 (Phase 3c).
-- 1. The receiver moves (car, GPS): where it was during a session, and where each survey hit was
--    measured, so a carrier heard at different strengths in different places is understood.
-- 2. Privacy: radios' own positions from LRRP reports are not kept. Mapping networks does not
--    need them and they say where people are. Stored ones are reduced to "has_position".

CREATE TABLE positions (
  id                  INTEGER PRIMARY KEY,
  session_id          INTEGER,
  ts                  INTEGER NOT NULL,
  lat                 REAL NOT NULL,
  lon                 REAL NOT NULL,
  alt_m               REAL,
  speed_mps           REAL,
  fix                 TEXT,
  source              TEXT
);
CREATE INDEX positions_ts ON positions(ts);

ALTER TABLE survey_hits ADD COLUMN lat REAL;
ALTER TABLE survey_hits ADD COLUMN lon REAL;

UPDATE csbk_events
   SET payload = json_set(json_remove(payload, '$.lat', '$.lon'), '$.has_position',
                          CASE WHEN json_extract(payload, '$.lat') IS NOT NULL THEN json('true') ELSE json('false') END)
 WHERE kind = 'lrrp';
