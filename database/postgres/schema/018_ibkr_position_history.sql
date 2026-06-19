-- ========================================================
-- IBKR read-only historical position snapshots
-- ========================================================

CREATE TABLE IF NOT EXISTS pams_ibkr_position_history (
    id BIGSERIAL PRIMARY KEY,
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    source_account_id TEXT,
    symbol VARCHAR(100),
    name VARCHAR(200) NOT NULL,
    asset_liability VARCHAR(10) NOT NULL,
    currency VARCHAR(10) NOT NULL,
    major_category VARCHAR(50) NOT NULL,
    minor_category VARCHAR(50) NOT NULL,
    market_value NUMERIC(28, 10) NOT NULL,
    cost NUMERIC(28, 10),
    holding_pnl NUMERIC(28, 10)
        GENERATED ALWAYS AS (market_value - COALESCE(cost, 0)) STORED,
    quantity NUMERIC(28, 10),
    buy_price NUMERIC(28, 10),
    current_price NUMERIC(28, 10),
    as_of DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_code, name, as_of)
);

CREATE INDEX IF NOT EXISTS idx_pams_ibkr_position_history_as_of
    ON pams_ibkr_position_history (as_of DESC);

CREATE INDEX IF NOT EXISTS idx_pams_ibkr_position_history_symbol_asof
    ON pams_ibkr_position_history (symbol, as_of DESC);
