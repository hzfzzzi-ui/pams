-- ============================================
-- IBKR read-only trade ledger for reconciliation
-- ============================================

CREATE TABLE IF NOT EXISTS pams_ibkr_trades (
    id BIGSERIAL PRIMARY KEY,
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    source_account_id TEXT,
    source_trade_key TEXT NOT NULL,
    trade_id TEXT,
    transaction_id TEXT,
    order_id TEXT,
    symbol VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    currency VARCHAR(10) NOT NULL,
    asset_category TEXT,
    trade_date DATE NOT NULL,
    side VARCHAR(12),
    quantity NUMERIC(28, 10),
    price NUMERIC(28, 10),
    gross_amount NUMERIC(28, 10),
    commission NUMERIC(28, 10),
    net_cash NUMERIC(28, 10),
    realized_pnl NUMERIC(28, 10),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_code, source_trade_key)
);

CREATE INDEX IF NOT EXISTS idx_pams_ibkr_trades_trade_date
    ON pams_ibkr_trades (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_ibkr_trades_symbol_date
    ON pams_ibkr_trades (symbol, trade_date DESC);
