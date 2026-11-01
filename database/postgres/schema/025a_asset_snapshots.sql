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
    holding_pnl NUMERIC(20,4) GENERATED ALWAYS AS (market_value - cost) STORED,
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

-- One physical store for the three observation grains.  The discriminator
-- and partial keys preserve provider/session and account-specific anchors.
CREATE TABLE IF NOT EXISTS public.market_observations (
    quote_id BIGSERIAL PRIMARY KEY,
    observation_type TEXT NOT NULL CHECK (observation_type IN ('daily', 'quote', 'fund')),
    as_of DATE NOT NULL,
    quote_date DATE GENERATED ALWAYS AS (as_of) STORED,
    asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    account_code VARCHAR(50),
    symbol TEXT, name TEXT, market TEXT,
    currency TEXT NOT NULL DEFAULT 'CNY',
    source TEXT, source_key TEXT, provider TEXT,
    open_price NUMERIC(28,10), high_price NUMERIC(28,10),
    low_price NUMERIC(28,10), close_price NUMERIC(28,10),
    unit_nav NUMERIC(28,10), estimated_unit_nav NUMERIC(28,10),
    estimated_market_value NUMERIC(28,10), volume NUMERIC(28,10), turnover NUMERIC(28,10),
    confidence TEXT DEFAULT 'medium',
    quote_session VARCHAR(40) DEFAULT 'close',
    return_status VARCHAR(20) DEFAULT 'pending',
    price NUMERIC(28,10), price_time TIMESTAMPTZ,
    base_currency VARCHAR(10) DEFAULT 'CNY', fx_rate NUMERIC(28,10), source_hash VARCHAR(64),
    confirmed_nav_date DATE, confirmed_unit_nav NUMERIC(20,8), confirmed_growth_pct NUMERIC(12,6),
    estimate_time TIMESTAMP, estimated_growth_pct NUMERIC(12,6),
    anchor_as_of DATE, anchor_market_value NUMERIC(20,4), anchor_unit_nav NUMERIC(20,8),
    estimated_delta NUMERIC(20,4), estimated_delta_pct NUMERIC(12,6),
    status VARCHAR(40), note TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (observation_type <> 'daily' OR (source IS NOT NULL AND source_key IS NOT NULL AND confidence IS NOT NULL)),
    CHECK (observation_type <> 'fund' OR (account_code IS NOT NULL AND symbol IS NOT NULL AND name IS NOT NULL AND status IS NOT NULL)),
    CHECK (observation_type <> 'quote' OR (
      provider IS NOT NULL AND source IS NOT NULL AND source_key IS NOT NULL
      AND quote_session IS NOT NULL AND return_status IS NOT NULL AND base_currency IS NOT NULL
      AND quote_session IN ('open','close','premarket','intraday','afterhours','overnight','nav','unknown')
      AND return_status IN ('pending','estimated','confirmed')))
);
CREATE UNIQUE INDEX market_observations_daily_key
    ON public.market_observations (as_of, source, source_key) WHERE observation_type='daily';
CREATE UNIQUE INDEX market_observations_quote_key
    ON public.market_observations (as_of, provider, source_key, quote_session) WHERE observation_type='quote';
CREATE UNIQUE INDEX market_observations_fund_key
    ON public.market_observations (as_of, account_code, symbol, name) WHERE observation_type='fund';
CREATE INDEX market_observations_asset_date ON public.market_observations (asset_id, as_of DESC);
CREATE INDEX market_observations_symbol_date ON public.market_observations (symbol, as_of DESC);
CREATE INDEX market_observations_account_fund ON public.market_observations (account_code, symbol, as_of DESC)
    WHERE observation_type='fund';
