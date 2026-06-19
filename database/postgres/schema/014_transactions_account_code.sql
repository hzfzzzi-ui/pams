-- ============================================
-- PAMS transactions/account normalization
-- ============================================

-- 1) Canonical accounts table used by API validation.
CREATE TABLE IF NOT EXISTS pams_accounts (
    account_code VARCHAR(50) PRIMARY KEY,
    display_name VARCHAR(100) NOT NULL,
    base_currency VARCHAR(3) NOT NULL DEFAULT 'CNY',
    is_asset BOOLEAN NOT NULL DEFAULT TRUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2) Seed default accounts (idempotent).
INSERT INTO pams_accounts (account_code, display_name, base_currency, is_asset, active)
VALUES
    ('tencent_licaitong', 'Tencent Licaitong', 'CNY', TRUE, TRUE),
    ('jianhang', 'Jianhang', 'CNY', TRUE, TRUE),
    ('huawei', 'Huawei', 'CNY', TRUE, TRUE),
    ('gongjijin', 'Gongjijin', 'CNY', TRUE, TRUE),
    ('binance', 'Binance', 'USD', TRUE, FALSE),
    ('ibkr', 'IBKR', 'USD', TRUE, TRUE),
    ('guangfa', 'Guangfa', 'CNY', TRUE, TRUE),
    ('santander', 'Santander', 'MXN', TRUE, TRUE),
    ('tonghuashun', 'Tonghuashun', 'CNY', TRUE, TRUE),
    ('jiekuan', 'Jiekuan', 'CNY', FALSE, TRUE),
    ('wechat', 'WeChat', 'CNY', TRUE, TRUE)
ON CONFLICT (account_code) DO UPDATE
SET display_name = EXCLUDED.display_name,
    base_currency = EXCLUDED.base_currency,
    is_asset = EXCLUDED.is_asset,
    active = EXCLUDED.active;

-- 3) Add canonical account_code column if missing.
ALTER TABLE pams_transactions
    ADD COLUMN IF NOT EXISTS account_code VARCHAR(50);

-- 4) Backfill account_code from legacy "account" values.
UPDATE pams_transactions
SET account_code = CASE LOWER(TRIM(COALESCE(account, '')))
    WHEN 'santander' THEN 'santander'
    WHEN 'ibkr' THEN 'ibkr'
    WHEN 'binance' THEN 'binance'
    WHEN 'jianhang' THEN 'jianhang'
    WHEN 'huawei' THEN 'huawei'
    WHEN 'gongjijin' THEN 'gongjijin'
    WHEN 'guangfa' THEN 'guangfa'
    WHEN 'tonghuashun' THEN 'tonghuashun'
    WHEN 'jiekuan' THEN 'jiekuan'
    WHEN 'wechat' THEN 'wechat'
    WHEN 'tencent_licaitong' THEN 'tencent_licaitong'
    ELSE account_code
END
WHERE account_code IS NULL;

-- 5) Ensure not-null with sensible default for old rows.
UPDATE pams_transactions
SET account_code = 'santander'
WHERE account_code IS NULL;

ALTER TABLE pams_transactions
    ALTER COLUMN account_code SET NOT NULL;

-- 6) Keep a fast lookup index + referential consistency.
CREATE INDEX IF NOT EXISTS idx_transactions_account_code
    ON pams_transactions(account_code);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_transactions_account_code'
    ) THEN
        ALTER TABLE pams_transactions
            ADD CONSTRAINT fk_transactions_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code);
    END IF;
END $$;
