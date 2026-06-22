-- Formal schema for intraday and confirmed off-exchange fund NAV estimates.
-- The refresh service still keeps a defensive runtime ensure, but fresh
-- environments should get this table from schema migrations before APIs join it.

BEGIN;

CREATE TABLE IF NOT EXISTS public.pams_fund_nav_estimates (
    id BIGSERIAL PRIMARY KEY,
    as_of DATE NOT NULL,
    account_code VARCHAR(50) NOT NULL,
    symbol VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    confirmed_nav_date DATE,
    confirmed_unit_nav NUMERIC(20, 8),
    confirmed_growth_pct NUMERIC(12, 6),
    estimate_time TIMESTAMP,
    estimated_unit_nav NUMERIC(20, 8),
    estimated_growth_pct NUMERIC(12, 6),
    anchor_as_of DATE,
    anchor_market_value NUMERIC(20, 4),
    anchor_unit_nav NUMERIC(20, 8),
    estimated_market_value NUMERIC(20, 4),
    estimated_delta NUMERIC(20, 4),
    estimated_delta_pct NUMERIC(12, 6),
    status VARCHAR(40) NOT NULL,
    note TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pams_fund_nav_estimates_as_of_account_code_symbol_name_key
        UNIQUE (as_of, account_code, symbol, name)
);

CREATE INDEX IF NOT EXISTS idx_pams_fund_nav_estimates_latest
    ON public.pams_fund_nav_estimates (account_code, symbol, as_of DESC);

CREATE INDEX IF NOT EXISTS idx_pams_fund_nav_estimates_status
    ON public.pams_fund_nav_estimates (as_of DESC, status);

COMMIT;
