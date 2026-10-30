-- Extend the physical market table, not the public compatibility view.
DO $market$
DECLARE
    physical_table regclass := COALESCE(
        to_regclass('pams_external_market.pams_ohlcv_daily'),
        to_regclass('public.pams_ohlcv_daily')
    );
BEGIN
    IF to_regclass('pams_external_market.pams_ohlcv_daily') IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_class
        WHERE oid = to_regclass('public.pams_ohlcv_daily') AND relkind IN ('r', 'p')
    ) THEN
        RAISE EXCEPTION 'Two physical pams_ohlcv_daily tables exist; resolve ownership before initialization';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = physical_table AND relkind IN ('r', 'p')) THEN
        RAISE EXCEPTION 'pams_ohlcv_daily physical table is unavailable';
    END IF;
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS adj_factor NUMERIC(14,6), ADD COLUMN IF NOT EXISTS limit_up NUMERIC(12,4), ADD COLUMN IF NOT EXISTS limit_down NUMERIC(12,4), ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN DEFAULT FALSE, ADD COLUMN IF NOT EXISTS turnover_rate DOUBLE PRECISION, ADD COLUMN IF NOT EXISTS turnover_rate_f DOUBLE PRECISION, ADD COLUMN IF NOT EXISTS volume_ratio DOUBLE PRECISION, ADD COLUMN IF NOT EXISTS float_share DOUBLE PRECISION, ADD COLUMN IF NOT EXISTS free_share DOUBLE PRECISION, ADD COLUMN IF NOT EXISTS circ_mv DOUBLE PRECISION', physical_table);
    EXECUTE format('COMMENT ON COLUMN %s.adj_factor IS %L', physical_table, '复权因子 (Tushare adj_factor)');
    EXECUTE format('COMMENT ON COLUMN %s.limit_up IS %L', physical_table, '涨停价');
    EXECUTE format('COMMENT ON COLUMN %s.limit_down IS %L', physical_table, '跌停价');
    EXECUTE format('COMMENT ON COLUMN %s.is_suspended IS %L', physical_table, '停牌标记');
    IF to_regclass('public.pams_ohlcv_daily') IS NULL THEN
        EXECUTE format('CREATE VIEW public.pams_ohlcv_daily AS SELECT * FROM %s', physical_table);
    ELSIF EXISTS (SELECT 1 FROM pg_class WHERE oid = to_regclass('public.pams_ohlcv_daily') AND relkind = 'v')
        AND (SELECT COUNT(*) FROM pg_attribute
             WHERE attrelid = to_regclass('public.pams_ohlcv_daily') AND attnum > 0 AND NOT attisdropped
               AND attname IN ('adj_factor','limit_up','limit_down','is_suspended','turnover_rate',
                               'turnover_rate_f','volume_ratio','float_share','free_share','circ_mv')) <> 10 THEN
        RAISE EXCEPTION 'Existing OHLCV view does not expose shared columns; review view migration separately';
    END IF;
END
$market$;
