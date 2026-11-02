-- Schema version 6.
-- keyups.decoded means a DSD-FME was connected and fed during the key-up (until schema 5 the
-- watch also set it when a decoder had merely been assigned, before its DSD-FME connected).
-- keyups.dmr_heard: that DSD-FME printed a DMR sync during the key-up. Rows from before this
-- migration have 0: not known.
ALTER TABLE keyups ADD COLUMN dmr_heard INTEGER NOT NULL DEFAULT 0;
