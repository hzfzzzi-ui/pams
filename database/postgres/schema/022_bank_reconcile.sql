-- Bank statement import rows and internal transfer matching.

CREATE TABLE IF NOT EXISTS pams_bank_statement_imports (
    id SERIAL PRIMARY KEY,
    profile_code VARCHAR(80) NOT NULL,
    bank VARCHAR(40) NOT NULL,
    account_code VARCHAR(80) NOT NULL,
    account_kind VARCHAR(40) NOT NULL,
    source_file TEXT NOT NULL,
    statement_start DATE,
    statement_end DATE,
    statement_balance NUMERIC(20, 4),
    row_count INTEGER NOT NULL DEFAULT 0,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pams_bank_statement_rows (
    id SERIAL PRIMARY KEY,
    import_id INTEGER REFERENCES pams_bank_statement_imports(id) ON DELETE CASCADE,
    source_uid VARCHAR(80) NOT NULL UNIQUE,
    profile_code VARCHAR(80) NOT NULL,
    bank VARCHAR(40) NOT NULL,
    account_code VARCHAR(80) NOT NULL,
    account_kind VARCHAR(40) NOT NULL,
    transaction_date DATE NOT NULL,
    posting_date DATE,
    amount NUMERIC(20, 4) NOT NULL,
    currency VARCHAR(10) NOT NULL,
    balance NUMERIC(20, 4),
    original_amount NUMERIC(20, 4),
    original_currency VARCHAR(10),
    category VARCHAR(80),
    subcategory VARCHAR(80),
    auto_classified BOOLEAN NOT NULL DEFAULT FALSE,
    summary TEXT,
    description TEXT,
    counterparty TEXT,
    raw_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pams_bank_transfer_matches (
    id SERIAL PRIMARY KEY,
    match_key VARCHAR(180) NOT NULL UNIQUE,
    match_type VARCHAR(60) NOT NULL,
    relation_code VARCHAR(100) NOT NULL,
    from_account_code VARCHAR(80) NOT NULL,
    to_account_code VARCHAR(80) NOT NULL,
    source_row_id INTEGER REFERENCES pams_bank_statement_rows(id) ON DELETE CASCADE,
    target_row_id INTEGER REFERENCES pams_bank_statement_rows(id) ON DELETE CASCADE,
    amount NUMERIC(20, 4) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    match_date DATE,
    date_delta_days INTEGER NOT NULL DEFAULT 0,
    confidence NUMERIC(6, 4) NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'auto',
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_rows_account_date
    ON pams_bank_statement_rows(account_code, transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_bank_rows_category
    ON pams_bank_statement_rows(category);

CREATE INDEX IF NOT EXISTS idx_bank_transfer_matches_type
    ON pams_bank_transfer_matches(match_type);

CREATE INDEX IF NOT EXISTS idx_bank_transfer_matches_rows
    ON pams_bank_transfer_matches(source_row_id, target_row_id);
