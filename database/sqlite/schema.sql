-- PAMS V1 schema
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- ibkr|binance|a_cn_broker|ccb|licaitong|cash|huawei_esop|receivable|liability
  name TEXT NOT NULL,
  currency TEXT NOT NULL,
  notes TEXT,
  createdAt TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asOfDate TEXT NOT NULL,          -- YYYY-MM-DD (pricing date, close)
  accountId TEXT NOT NULL,
  symbol TEXT NOT NULL,
  symbolType TEXT NOT NULL,        -- stock|fund|etf|crypto|cash|esop
  quantity REAL NOT NULL,
  costBasis REAL,                  -- per-unit cost basis in account currency (optional)
  costCurrency TEXT,
  marketPrice REAL,                -- close price in account currency
  marketValue REAL,                -- market value in account currency
  pnlUnrealized REAL,              -- unrealized pnl in account currency
  notes TEXT,
  createdAt TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(accountId) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_positions_asof ON positions(asOfDate);
CREATE INDEX IF NOT EXISTS idx_positions_account ON positions(accountId);

CREATE TABLE IF NOT EXISTS fx_rates (
  asOfDate TEXT NOT NULL,
  pair TEXT NOT NULL,              -- e.g., USD/CNY, MXN/CNY
  rate REAL NOT NULL,
  source TEXT,
  createdAt TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (asOfDate, pair)
);

CREATE TABLE IF NOT EXISTS cashflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,              -- YYYY-MM-DD
  source TEXT NOT NULL,            -- santander|wechat|bank|investment
  direction TEXT NOT NULL,         -- in|out
  amount REAL NOT NULL,
  currency TEXT NOT NULL,
  category TEXT,                   -- salary|reimburse|loan|transfer|expense|...
  memo TEXT,
  createdAt TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cashflows_date ON cashflows(date);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  createdAt TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asOfDate TEXT NOT NULL,
  severity TEXT NOT NULL,          -- info|warn|critical
  kind TEXT NOT NULL,              -- drawdown|concentration|policy
  accountId TEXT,
  symbol TEXT,
  message TEXT NOT NULL,
  data TEXT,
  createdAt TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_asof ON alerts(asOfDate);

CREATE TABLE IF NOT EXISTS order_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asOfDate TEXT NOT NULL,
  accountId TEXT NOT NULL,
  venue TEXT NOT NULL,             -- IBKR|BINANCE|CN
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,              -- buy|sell
  quantity REAL NOT NULL,
  orderType TEXT NOT NULL DEFAULT 'MKT',
  limitPrice REAL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'draft', -- draft|proposed|approved|executed|cancelled
  createdAt TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(accountId) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_order_plans_asof ON order_plans(asOfDate);
