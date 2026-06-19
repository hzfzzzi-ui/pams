-- Generic data sync run status table.
-- The IBKR Flex collector writes sync_key='ibkr_flex' into this existing
-- shape so status cards can share one operational audit table.

CREATE TABLE IF NOT EXISTS pams_data_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    sync_key TEXT NOT NULL,
    asset_scope TEXT NOT NULL,
    target_trade_date DATE,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    latest_trade_date_before DATE,
    latest_trade_date_after DATE,
    rows_upserted INTEGER,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pams_data_sync_runs_sync_key_started_at
    ON pams_data_sync_runs (sync_key, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_pams_data_sync_runs_target_trade_date
    ON pams_data_sync_runs (target_trade_date DESC);
