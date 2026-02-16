-- =====================================================
-- PAMS P2/P3: 基本面门控 + 情绪 + 冷却期 + 事件监控
-- =====================================================

-- ① 候选表新增列
ALTER TABLE pams_strategy_candidates
  ADD COLUMN IF NOT EXISTS quality_score NUMERIC(5,1),
  ADD COLUMN IF NOT EXISTS sent_state VARCHAR(10),
  ADD COLUMN IF NOT EXISTS sent_score NUMERIC(5,1);

-- ② 订单表新增列
ALTER TABLE pams_order_drafts
  ADD COLUMN IF NOT EXISTS quality_score NUMERIC(5,1),
  ADD COLUMN IF NOT EXISTS sent_state VARCHAR(10);

-- ③ 基本面快照表
CREATE TABLE IF NOT EXISTS pams_fundamental_snapshot (
  id              SERIAL PRIMARY KEY,
  symbol          VARCHAR(20) NOT NULL,
  market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
  as_of           DATE NOT NULL,
  roe             NUMERIC(8,4),
  gross_margin    NUMERIC(8,4),
  debt_ratio      NUMERIC(8,4),
  op_cashflow_ttm NUMERIC(18,2),
  net_profit_ttm  NUMERIC(18,2),
  quality_score   NUMERIC(5,1),
  cashflow_deterioration BOOLEAN DEFAULT FALSE,
  industry_code   VARCHAR(20),
  industry_trend  VARCHAR(20),
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol, market, as_of)
);
CREATE INDEX IF NOT EXISTS idx_fundamental_symbol ON pams_fundamental_snapshot(symbol, market, as_of);

-- ④ 事件监控表
CREATE TABLE IF NOT EXISTS pams_event_monitor (
  id              SERIAL PRIMARY KEY,
  symbol          VARCHAR(20) NOT NULL,
  market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
  event_type      VARCHAR(50) NOT NULL,
  event_date      DATE NOT NULL,
  severity        VARCHAR(20) DEFAULT 'INFO',
  title           TEXT,
  source          VARCHAR(50),
  is_active       BOOLEAN DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_event_symbol ON pams_event_monitor(symbol, market, event_date DESC);
CREATE INDEX IF NOT EXISTS idx_event_active ON pams_event_monitor(is_active, event_type);

-- ⑤ 情绪状态表
CREATE TABLE IF NOT EXISTS pams_sentiment_state (
  id              SERIAL PRIMARY KEY,
  as_of           DATE NOT NULL UNIQUE,
  sent_state      VARCHAR(10) NOT NULL,
  sent_score      NUMERIC(5,1),
  advancers       INT,
  decliners       INT,
  limit_up        INT,
  limit_down      INT,
  big_red_7d      INT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ⑥ 冷却期跟踪表
CREATE TABLE IF NOT EXISTS pams_position_cooldown (
  id              SERIAL PRIMARY KEY,
  symbol          VARCHAR(20) NOT NULL,
  market          VARCHAR(10) NOT NULL DEFAULT 'CN_A',
  stop_date       DATE NOT NULL,
  stop_count      INT DEFAULT 1,
  cooldown_until  DATE NOT NULL,
  blacklist_until DATE,
  is_active       BOOLEAN DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cooldown_active ON pams_position_cooldown(symbol, market, is_active);
