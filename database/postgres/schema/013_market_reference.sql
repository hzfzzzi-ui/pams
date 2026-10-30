-- Shared reference tables required by asset services and independent backtest.
-- =====================================================
-- PIT (Point-in-Time) 数据表
-- 用于消除回测中的幸存者偏差和时光旅行
-- =====================================================

-- ① 证券主表 (上市/退市/交易所/名称)
CREATE TABLE IF NOT EXISTS pit_security_master (
    ts_code     VARCHAR(12) PRIMARY KEY,          -- Tushare 格式: 000001.SZ
    symbol      VARCHAR(20) NOT NULL,              -- 内部格式: sz000001
    name        VARCHAR(100),
    exchange    VARCHAR(10) NOT NULL,              -- SSE / SZSE
    list_date   DATE,
    delist_date DATE,
    industry    VARCHAR(100),
    market_type VARCHAR(20) DEFAULT 'CN_A',        -- CN_A / CN_INDEX
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pit_sm_symbol
    ON pit_security_master(symbol);
CREATE INDEX IF NOT EXISTS idx_pit_sm_list_date
    ON pit_security_master(list_date);
CREATE INDEX IF NOT EXISTS idx_pit_sm_exchange
    ON pit_security_master(exchange);

-- ② 指数日线 (独立于 ohlcv, 避免 market 混杂)
CREATE TABLE IF NOT EXISTS pit_index_daily (
    index_code  VARCHAR(12) NOT NULL,              -- 000001.SH / 399001.SZ
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    turnover    NUMERIC(18,2),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (index_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_pit_idx_date
    ON pit_index_daily(trade_date);

-- ③ 交易日历
CREATE TABLE IF NOT EXISTS pit_trade_cal (
    exchange    VARCHAR(10) NOT NULL,              -- SSE / SZSE
    cal_date    DATE NOT NULL,
    is_open     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (exchange, cal_date)
);
CREATE INDEX IF NOT EXISTS idx_pit_cal_open
    ON pit_trade_cal(exchange, is_open, cal_date);

-- ④ ST 标记历史 (消除幸存者偏差)
CREATE TABLE IF NOT EXISTS pit_st_history (
    ts_code     VARCHAR(12) NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE,                              -- NULL = 仍然是 ST
    st_type     VARCHAR(20) NOT NULL DEFAULT 'ST', -- ST / *ST / S*ST
    name_before VARCHAR(100),
    name_after  VARCHAR(100),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ts_code, start_date)
);
CREATE INDEX IF NOT EXISTS idx_pit_st_active
    ON pit_st_history(ts_code, end_date);

-- ⑤ 行业归属历史 (消除行业时光旅行)
CREATE TABLE IF NOT EXISTS pit_industry_history (
    ts_code     VARCHAR(12) NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE,                              -- NULL = 当前行业
    standard    VARCHAR(20) NOT NULL DEFAULT 'SW', -- SW=申万 / CSRC=证监会
    industry_l1 VARCHAR(100),
    industry_l2 VARCHAR(100),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ts_code, start_date, standard)
);
CREATE INDEX IF NOT EXISTS idx_pit_ind_ts
    ON pit_industry_history(ts_code, standard);
