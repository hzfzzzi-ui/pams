-- Read-only TradingView portfolio transaction ledger.
-- TradingView CSV exports are archived under data/raw/ before normalized rows
-- are inserted here. This table stores no broker credentials and has no
-- trading permissions.

CREATE TABLE IF NOT EXISTS pams_tradingview_transactions (
    id BIGSERIAL PRIMARY KEY,
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    portfolio_name VARCHAR(100) NOT NULL DEFAULT 'a_share',
    source_file TEXT NOT NULL,
    source_hash VARCHAR(64) NOT NULL,
    row_hash VARCHAR(64) NOT NULL UNIQUE,
    symbol VARCHAR(40) NOT NULL,
    normalized_symbol VARCHAR(20),
    side VARCHAR(20) NOT NULL,
    quantity NUMERIC(20,6) NOT NULL DEFAULT 0,
    fill_price NUMERIC(20,6),
    commission NUMERIC(20,6),
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    closing_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tradingview_transactions_account_time
    ON pams_tradingview_transactions(account_code, closing_time);

CREATE INDEX IF NOT EXISTS idx_tradingview_transactions_symbol
    ON pams_tradingview_transactions(normalized_symbol);
