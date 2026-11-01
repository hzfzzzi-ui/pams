-- Shared daily market prices used by asset valuation and benchmark history.
-- New databases retain the shared physical table in public. Older databases
-- may instead expose an existing compatibility view over pams_external_market.
DO $market$
DECLARE
    physical_table regclass;
BEGIN
    physical_table := to_regclass('pams_external_market.pams_ohlcv_daily');
    IF physical_table IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_class
        WHERE oid = to_regclass('public.pams_ohlcv_daily') AND relkind IN ('r', 'p')
    ) THEN
        RAISE EXCEPTION 'Two physical pams_ohlcv_daily tables exist; resolve ownership before initialization';
    END IF;
    IF physical_table IS NULL THEN
        IF to_regclass('public.pams_ohlcv_daily') IS NULL THEN
            CREATE TABLE public.pams_ohlcv_daily (
                symbol VARCHAR(20) NOT NULL,
                market VARCHAR(10) NOT NULL DEFAULT 'CN_A',
                trade_date DATE NOT NULL,
                open NUMERIC(12,4), high NUMERIC(12,4),
                low NUMERIC(12,4), close NUMERIC(12,4),
                volume BIGINT, turnover NUMERIC(18,2),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (symbol, market, trade_date)
            );
        END IF;
        physical_table := to_regclass('public.pams_ohlcv_daily');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = physical_table AND relkind IN ('r', 'p')) THEN
        RAISE EXCEPTION 'pams_ohlcv_daily physical table is unavailable';
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON %s (trade_date)', physical_table);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON %s (symbol, market)', physical_table);
END
$market$;
