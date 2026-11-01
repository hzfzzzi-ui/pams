-- Disjoint holding-return snapshots and published portfolio valuations.
-- Both result types retain their own keys and cannot overwrite each other.
CREATE TABLE IF NOT EXISTS public.valuation_results (
    result_type text NOT NULL CHECK (result_type IN ('holding_return','portfolio_published')),
    base_currency varchar(10) NOT NULL DEFAULT 'CNY',
    snapshot_date date,
    section varchar(40),
    market_value numeric(28,10),
    cost numeric(28,10),
    position_pnl numeric(28,10),
    realized_income numeric(28,10),
    total_pnl numeric(28,10),
    net_flow numeric(28,10),
    cumulative_capital numeric(28,10),
    return_rate numeric(28,12),
    daily_return_rate numeric(28,12),
    time_weighted_return_rate numeric(28,12),
    capital_return_rate numeric(28,12),
    display_return_rate numeric(28,12),
    rows_count integer NOT NULL DEFAULT 0,
    holdings_count integer NOT NULL DEFAULT 0,
    accounts_count integer NOT NULL DEFAULT 0,
    source varchar(80),

    valuation_date date,
    cycle varchar(40),
    status varchar(20),
    net_liquidation_value numeric(28,10),
    assets_cny numeric(28,10),
    liabilities_cny numeric(28,10),
    gross_exposure_cny numeric(28,10),
    investment_value_cny numeric(28,10),
    cash_cny numeric(28,10),
    daily_pnl_cny numeric(28,10),
    total_pnl_cny numeric(28,10),
    source_run_id bigint,
    published_at timestamptz,

    data_completeness text,
    source_hash varchar(64),
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (result_type='holding_return' AND snapshot_date IS NOT NULL AND section IS NOT NULL
         AND section IN ('total','a_stock','a_fund','us_stock','precious_metal')
         AND valuation_date IS NULL AND cycle IS NULL AND status IS NULL
         AND published_at IS NULL AND net_liquidation_value IS NULL
         AND assets_cny IS NULL AND liabilities_cny IS NULL AND gross_exposure_cny IS NULL
         AND investment_value_cny IS NULL AND cash_cny IS NULL AND daily_pnl_cny IS NULL
         AND total_pnl_cny IS NULL AND source_run_id IS NULL)
        OR
        (result_type='portfolio_published' AND valuation_date IS NOT NULL
         AND cycle IS NOT NULL AND status IS NOT NULL AND status='published' AND data_completeness IS NOT NULL
         AND net_liquidation_value IS NOT NULL AND assets_cny IS NOT NULL
         AND liabilities_cny IS NOT NULL AND gross_exposure_cny IS NOT NULL
         AND investment_value_cny IS NOT NULL AND cash_cny IS NOT NULL
         AND snapshot_date IS NULL AND section IS NULL AND market_value IS NULL
         AND cost IS NULL AND position_pnl IS NULL AND realized_income IS NULL
         AND total_pnl IS NULL AND net_flow IS NULL AND cumulative_capital IS NULL
         AND return_rate IS NULL AND daily_return_rate IS NULL
         AND time_weighted_return_rate IS NULL AND capital_return_rate IS NULL
         AND display_return_rate IS NULL AND source IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS valuation_results_holding_key
    ON public.valuation_results (snapshot_date,section,base_currency)
    WHERE result_type='holding_return';
CREATE UNIQUE INDEX IF NOT EXISTS valuation_results_portfolio_key
    ON public.valuation_results (valuation_date,cycle,base_currency)
    WHERE result_type='portfolio_published';
CREATE INDEX IF NOT EXISTS idx_valuation_results_holding_series
    ON public.valuation_results (section,base_currency,snapshot_date)
    WHERE result_type='holding_return';
CREATE INDEX IF NOT EXISTS idx_valuation_results_portfolio_latest
    ON public.valuation_results (base_currency,status,valuation_date DESC)
    WHERE result_type='portfolio_published';
CREATE OR REPLACE FUNCTION public.valuation_results_type_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.result_type IS DISTINCT FROM OLD.result_type THEN
        RAISE EXCEPTION 'valuation result type cannot be changed';
    END IF;
    RETURN NEW;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.valuation_results'::regclass
                   AND tgname='valuation_results_type_guard') THEN
        CREATE TRIGGER valuation_results_type_guard BEFORE UPDATE ON public.valuation_results
            FOR EACH ROW EXECUTE FUNCTION public.valuation_results_type_guard();
    END IF;
END $$;
