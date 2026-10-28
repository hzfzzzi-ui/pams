-- Opt-in account import staging. Apply only in an approved migration window.
-- No existing financial table, row or configuration is modified here.
CREATE TABLE IF NOT EXISTS pams_account_import_batch (
    batch_id UUID PRIMARY KEY,
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    mode TEXT NOT NULL CHECK (mode IN ('manual', 'upload')),
    kind TEXT NOT NULL CHECK (kind IN ('holding', 'trade', 'cash')),
    completeness TEXT NOT NULL DEFAULT 'partial' CHECK (completeness IN ('partial', 'complete')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'voided')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    rows_json JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(rows_json) = 'array'),
    preview_json JSONB,
    preview_token VARCHAR(64),
    published_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_import_batch_account
    ON pams_account_import_batch(account_code, created_at DESC);

CREATE TABLE IF NOT EXISTS pams_account_import_file (
    file_id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES pams_account_import_batch(batch_id),
    filename VARCHAR(200) NOT NULL,
    content_type VARCHAR(80) NOT NULL,
    sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL CHECK (octet_length(content) <= 10485760),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_account_import_file_hash ON pams_account_import_file(sha256);

CREATE TABLE IF NOT EXISTS pams_account_import_template (
    account_code VARCHAR(50) NOT NULL REFERENCES pams_accounts(account_code),
    kind TEXT NOT NULL CHECK (kind IN ('holding', 'trade', 'cash')),
    mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_code, kind)
);

CREATE TABLE IF NOT EXISTS pams_account_import_audit (
    audit_id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES pams_account_import_batch(batch_id),
    event TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_import_audit_batch
    ON pams_account_import_audit(batch_id, created_at);
