-- Holding asset-reference column, foreign key and index.

BEGIN;

ALTER TABLE pams_holdings_snapshot
    ADD COLUMN IF NOT EXISTS asset_id VARCHAR;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_holdings_snapshot_asset_id'
    ) THEN
        ALTER TABLE pams_holdings_snapshot
            ADD CONSTRAINT fk_pams_holdings_snapshot_asset_id
            FOREIGN KEY (asset_id)
            REFERENCES pams_asset_master(asset_id)
            ON UPDATE CASCADE
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END $$;

ALTER TABLE pams_holdings_snapshot VALIDATE CONSTRAINT fk_pams_holdings_snapshot_asset_id;

CREATE INDEX IF NOT EXISTS idx_pams_holdings_snapshot_asset_id
    ON pams_holdings_snapshot(asset_id);

COMMIT;
