-- =====================================================
-- 扩展 pams_ohlcv_daily: PIT 必要列
-- =====================================================

ALTER TABLE pams_ohlcv_daily
    ADD COLUMN IF NOT EXISTS adj_factor    NUMERIC(14,6),
    ADD COLUMN IF NOT EXISTS limit_up      NUMERIC(12,4),
    ADD COLUMN IF NOT EXISTS limit_down    NUMERIC(12,4),
    ADD COLUMN IF NOT EXISTS is_suspended  BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN pams_ohlcv_daily.adj_factor   IS '复权因子 (Tushare adj_factor)';
COMMENT ON COLUMN pams_ohlcv_daily.limit_up     IS '涨停价';
COMMENT ON COLUMN pams_ohlcv_daily.limit_down   IS '跌停价';
COMMENT ON COLUMN pams_ohlcv_daily.is_suspended IS '停牌标记';
