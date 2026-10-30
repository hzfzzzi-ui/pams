-- Existing asset risk uses the industry field as a fallback.  Keep the legacy
-- shape so an empty database has the same shared-data contract as an upgrade.
-- This does not recreate the strategy subsystem.
DO $shared_industry$
BEGIN
    IF to_regclass('public.pams_fundamental_snapshot') IS NULL THEN
        IF to_regclass('pams_module.pams_fundamental_snapshot') IS NOT NULL THEN
            EXECUTE 'CREATE VIEW public.pams_fundamental_snapshot AS SELECT * FROM pams_module.pams_fundamental_snapshot';
        ELSE
            CREATE TABLE public.pams_fundamental_snapshot (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                market VARCHAR(10) NOT NULL DEFAULT 'CN_A',
                as_of DATE NOT NULL,
                roe NUMERIC(8,4),
                gross_margin NUMERIC(8,4),
                debt_ratio NUMERIC(8,4),
                op_cashflow_ttm NUMERIC(18,2),
                net_profit_ttm NUMERIC(18,2),
                quality_score NUMERIC(5,1),
                cashflow_deterioration BOOLEAN DEFAULT FALSE,
                industry_code VARCHAR(20),
                industry_trend VARCHAR(20),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(symbol, market, as_of)
            );
            CREATE INDEX idx_fundamental_symbol
                ON public.pams_fundamental_snapshot(symbol, market, as_of);
        END IF;
    END IF;
END
$shared_industry$;
