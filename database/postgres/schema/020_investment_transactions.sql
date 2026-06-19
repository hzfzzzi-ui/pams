-- Normalized investment transaction ledger for manual and web-imported trades.

CREATE TABLE IF NOT EXISTS pams_investment_transactions (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR(40) NOT NULL DEFAULT 'manual',
    source_key VARCHAR(160) NOT NULL,
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    trade_date DATE NOT NULL,
    trade_time TIMESTAMP WITHOUT TIME ZONE,
    major_category VARCHAR(50),
    minor_category VARCHAR(50),
    symbol VARCHAR(100),
    name VARCHAR(200) NOT NULL,
    side VARCHAR(20) NOT NULL,
    quantity NUMERIC(28, 10),
    price NUMERIC(28, 10),
    gross_amount NUMERIC(28, 10),
    fees NUMERIC(28, 10),
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    status VARCHAR(40),
    notes TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_pams_investment_transactions_date
    ON pams_investment_transactions (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_investment_transactions_account_date
    ON pams_investment_transactions (account_code, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_investment_transactions_symbol_date
    ON pams_investment_transactions (symbol, trade_date DESC);
