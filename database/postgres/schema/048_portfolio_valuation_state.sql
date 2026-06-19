-- Canonical published portfolio valuation state.
-- This table stores the single account-level valuation snapshot that portfolio
-- pages should converge on instead of recalculating assets, liabilities, and
-- net liquidation value independently.

CREATE TABLE IF NOT EXISTS public.pams_portfolio_valuation_state (
    valuation_date date NOT NULL,
    cycle varchar(40) NOT NULL DEFAULT 'manual_publish',
    base_currency varchar(10) NOT NULL DEFAULT 'CNY',
    status varchar(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'failed')),
    data_completeness varchar(20) NOT NULL DEFAULT 'complete',
    net_liquidation_value numeric(28, 10) NOT NULL DEFAULT 0,
    assets_cny numeric(28, 10) NOT NULL DEFAULT 0,
    liabilities_cny numeric(28, 10) NOT NULL DEFAULT 0,
    gross_exposure_cny numeric(28, 10) NOT NULL DEFAULT 0,
    investment_value_cny numeric(28, 10) NOT NULL DEFAULT 0,
    cash_cny numeric(28, 10) NOT NULL DEFAULT 0,
    daily_pnl_cny numeric(28, 10) NOT NULL DEFAULT 0,
    total_pnl_cny numeric(28, 10) NOT NULL DEFAULT 0,
    source_run_id bigint,
    source_hash varchar(64),
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (valuation_date, cycle, base_currency)
);

CREATE INDEX IF NOT EXISTS idx_pams_portfolio_valuation_state_latest
    ON public.pams_portfolio_valuation_state (base_currency, status, valuation_date DESC);
