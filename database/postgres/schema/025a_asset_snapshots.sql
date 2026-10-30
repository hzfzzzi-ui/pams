-- Foundational asset tables, matching the observed PostgreSQL 16 schema.
-- Apply after accounts and asset master; this file contains no business data.
CREATE TABLE IF NOT EXISTS public.pams_holdings_snapshot (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(100),
    name VARCHAR(200) NOT NULL,
    asset_liability VARCHAR(10) NOT NULL CHECK (asset_liability IN ('资产', '负债')),
    currency VARCHAR(10) NOT NULL,
    account_code VARCHAR(50) NOT NULL REFERENCES public.pams_accounts(account_code),
    major_category VARCHAR(50) NOT NULL,
    minor_category VARCHAR(50) NOT NULL,
    market_value NUMERIC(20,4) NOT NULL,
    cost NUMERIC(20,4),
    holding_pnl NUMERIC(20,4) GENERATED ALWAYS AS (market_value - COALESCE(cost, 0)) STORED,
    as_of DATE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    quantity NUMERIC(20,6),
    buy_price NUMERIC(20,6),
    current_price NUMERIC(20,6),
    asset_id VARCHAR,
    UNIQUE (account_code, name, as_of)
);
CREATE INDEX IF NOT EXISTS idx_holdings_account ON public.pams_holdings_snapshot(account_code);
CREATE INDEX IF NOT EXISTS idx_holdings_asof ON public.pams_holdings_snapshot(as_of);
CREATE INDEX IF NOT EXISTS idx_holdings_category ON public.pams_holdings_snapshot(major_category, minor_category);

CREATE TABLE IF NOT EXISTS public.pams_asset_market_daily (
    as_of DATE NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    symbol TEXT, name TEXT, market TEXT,
    currency TEXT NOT NULL DEFAULT 'CNY',
    open_price NUMERIC(28,10), high_price NUMERIC(28,10),
    low_price NUMERIC(28,10), close_price NUMERIC(28,10),
    unit_nav NUMERIC(28,10), estimated_unit_nav NUMERIC(28,10),
    estimated_market_value NUMERIC(28,10), volume NUMERIC(28,10), turnover NUMERIC(28,10),
    confidence TEXT NOT NULL DEFAULT 'medium',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (as_of, source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_pams_asset_market_daily_symbol_date ON public.pams_asset_market_daily(symbol, as_of DESC);
