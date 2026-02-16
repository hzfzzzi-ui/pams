-- =====================================================
-- PAMS Strategy System Tables (P0: A股卫星策略)
-- =====================================================

-- ① OHLCV 日线历史
CREATE TABLE IF NOT EXISTS pams_ohlcv_daily (
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(10) NOT NULL DEFAULT 'CN_A',
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    turnover    NUMERIC(18,2),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, market, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON pams_ohlcv_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON pams_ohlcv_daily(symbol, market);

-- ② 策略状态（月度趋势判定）
CREATE TABLE IF NOT EXISTS pams_strategy_state (
    id              SERIAL PRIMARY KEY,
    as_of           DATE NOT NULL UNIQUE,
    trend_state     VARCHAR(20) NOT NULL,
    spx_close       NUMERIC(12,2),
    spx_ma10        NUMERIC(12,2),
    hysteresis_months INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ③ 候选标的
CREATE TABLE IF NOT EXISTS pams_strategy_candidates (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    name            VARCHAR(100),
    market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
    scan_date       DATE NOT NULL,
    stage           VARCHAR(20) NOT NULL,
    close           NUMERIC(12,4),
    ma20            NUMERIC(12,4),
    ma60            NUMERIC(12,4),
    atr14           NUMERIC(12,4),
    high_20d        NUMERIC(12,4),
    low_20d         NUMERIC(12,4),
    max_return_40d  NUMERIC(8,4),
    flow_result     VARCHAR(10),
    flow_score      NUMERIC(5,1),
    confirm_score   NUMERIC(5,1),
    eligible        BOOLEAN DEFAULT TRUE,
    hard_veto_hit   BOOLEAN DEFAULT FALSE,
    reason_codes    JSONB DEFAULT '[]',
    entry_price     NUMERIC(12,4),
    stop_price      NUMERIC(12,4),
    valid_until     DATE,
    position_size   INT,
    risk_amount_cny NUMERIC(12,2),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, market, scan_date)
);
CREATE INDEX IF NOT EXISTS idx_candidates_scan ON pams_strategy_candidates(scan_date, stage);

-- ④ 订单草稿
CREATE TABLE IF NOT EXISTS pams_order_drafts (
    id              SERIAL PRIMARY KEY,
    candidate_id    INT REFERENCES pams_strategy_candidates(id),
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
    b_nav           NUMERIC(14,2) NOT NULL,
    risk_mode       VARCHAR(10) NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    stop_price      NUMERIC(12,4) NOT NULL,
    valid_until     DATE NOT NULL,
    position_size   INT NOT NULL,
    position_value_pct NUMERIC(5,4),
    max_loss_cny    NUMERIC(12,2),
    risk_amount_cny NUMERIC(12,2),
    concurrent_risk_after NUMERIC(5,4),
    flow_confirm    VARCHAR(10),
    confirm_score   NUMERIC(5,1),
    eligible        BOOLEAN DEFAULT TRUE,
    hard_veto_hit   BOOLEAN DEFAULT FALSE,
    reason_codes    JSONB DEFAULT '[]',
    status          VARCHAR(20) DEFAULT 'pending',
    confirmed_at    TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON pams_order_drafts(status);

-- ⑤ 黑名单
CREATE TABLE IF NOT EXISTS pams_strategy_blacklist (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
    reason          VARCHAR(50) NOT NULL,
    expires_at      DATE,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_blacklist_active ON pams_strategy_blacklist(active, market);

-- ⑥ 风控快照
CREATE TABLE IF NOT EXISTS pams_risk_snapshot (
    id              SERIAL PRIMARY KEY,
    as_of           DATE NOT NULL UNIQUE,
    b_nav           NUMERIC(14,2),
    open_positions  INT,
    concurrent_risk_pct NUMERIC(5,4),
    monthly_drawdown_pct NUMERIC(5,4),
    circuit_breaker VARCHAR(20) DEFAULT 'normal',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
