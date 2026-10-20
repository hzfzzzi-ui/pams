-- Canonical daily return state and quote snapshots.
--
-- This schema is the accounting boundary between market data refresh jobs and
-- investment pages.  Refresh jobs may write estimated or confirmed marks here;
-- frontend pages should consume API payloads derived from this state instead of
-- recalculating daily return status in browser code.

BEGIN;

CREATE TABLE IF NOT EXISTS public.pams_market_quote_snapshot (
    quote_id BIGSERIAL PRIMARY KEY,
    quote_date DATE NOT NULL,
    asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    symbol TEXT,
    name TEXT,
    market TEXT,
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    quote_session VARCHAR(40) NOT NULL DEFAULT 'close',
    return_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    price NUMERIC(28, 10),
    price_time TIMESTAMPTZ,
    base_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    fx_rate NUMERIC(28, 10),
    source_hash VARCHAR(64),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_pams_market_quote_snapshot_session CHECK (
        quote_session IN ('open', 'close', 'premarket', 'intraday', 'afterhours', 'overnight', 'nav', 'unknown')
    ),
    CONSTRAINT ck_pams_market_quote_snapshot_status CHECK (
        return_status IN ('pending', 'estimated', 'confirmed')
    ),
    CONSTRAINT uq_pams_market_quote_snapshot_key UNIQUE (
        quote_date, provider, source_key, quote_session
    )
);

CREATE INDEX IF NOT EXISTS idx_pams_market_quote_snapshot_asset_date
    ON public.pams_market_quote_snapshot (asset_id, quote_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_market_quote_snapshot_symbol_date
    ON public.pams_market_quote_snapshot (symbol, quote_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_market_quote_snapshot_status
    ON public.pams_market_quote_snapshot (quote_date DESC, return_status, provider);

CREATE TABLE IF NOT EXISTS public.pams_asset_daily_pnl (
    daily_pnl_id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    account_code TEXT NOT NULL DEFAULT '',
    asset_key TEXT NOT NULL,
    asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    symbol TEXT,
    name TEXT,
    major_category TEXT,
    minor_category TEXT,
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    base_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    quantity NUMERIC(28, 10),
    previous_price NUMERIC(28, 10),
    current_price NUMERIC(28, 10),
    market_value_cny NUMERIC(28, 10) NOT NULL DEFAULT 0,
    daily_pnl_cny NUMERIC(28, 10) NOT NULL DEFAULT 0,
    total_pnl_cny NUMERIC(28, 10) NOT NULL DEFAULT 0,
    return_rate NUMERIC(18, 10),
    daily_return_rate NUMERIC(18, 10),
    return_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    provider TEXT,
    source_quote_id BIGINT REFERENCES public.pams_market_quote_snapshot(quote_id) ON DELETE SET NULL,
    source_hash VARCHAR(64),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_pams_asset_daily_pnl_status CHECK (
        return_status IN ('pending', 'estimated', 'confirmed')
    ),
    CONSTRAINT uq_pams_asset_daily_pnl_key UNIQUE (
        trade_date, account_code, asset_key, base_currency
    )
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_daily_pnl_asset_date
    ON public.pams_asset_daily_pnl (asset_id, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_asset_daily_pnl_symbol_date
    ON public.pams_asset_daily_pnl (symbol, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_pams_asset_daily_pnl_status
    ON public.pams_asset_daily_pnl (trade_date DESC, return_status);

CREATE TABLE IF NOT EXISTS public.pams_sync_run_log (
    run_id BIGSERIAL PRIMARY KEY,
    sync_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    assets_requested INTEGER NOT NULL DEFAULT 0,
    assets_success INTEGER NOT NULL DEFAULT 0,
    assets_failed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    source_hash VARCHAR(64),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_pams_sync_run_log_status CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS idx_pams_sync_run_log_provider_started
    ON public.pams_sync_run_log (provider, started_at DESC);

COMMIT;
