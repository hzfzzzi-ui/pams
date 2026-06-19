-- Account balance reconciliation review ledger.

CREATE TABLE IF NOT EXISTS pams_account_reconciliation_reviews (
    id BIGSERIAL PRIMARY KEY,
    account_code TEXT NOT NULL,
    review_action TEXT NOT NULL,
    source_status TEXT,
    review_fingerprint TEXT,
    reviewed_balance NUMERIC(20, 4),
    currency TEXT,
    balance_as_of DATE,
    note TEXT,
    reviewed_by TEXT NOT NULL DEFAULT 'user',
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pams_account_reconciliation_reviews_latest
    ON pams_account_reconciliation_reviews (account_code, reviewed_at DESC, id DESC);
