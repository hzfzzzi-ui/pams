-- 012: 观察池管理 — 入池/退出日志表
-- 记录观察池的所有变更事件（入池、退出、状态变更）

CREATE TABLE IF NOT EXISTS pams_watchlist_log (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(20) NOT NULL,
    market        VARCHAR(10) NOT NULL DEFAULT 'CN',
    name          VARCHAR(100),
    action        VARCHAR(20) NOT NULL CHECK (action IN ('entry', 'exit', 'status_change')),
    old_status    VARCHAR(20),
    new_status    VARCHAR(20),
    reason        TEXT,
    reason_codes  JSONB DEFAULT '[]',
    scan_date     DATE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wl_log_action_date ON pams_watchlist_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wl_log_symbol ON pams_watchlist_log(symbol, market);
