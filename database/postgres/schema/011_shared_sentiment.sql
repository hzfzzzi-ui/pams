-- Shared sentiment data used by the independent market snapshot application.
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
