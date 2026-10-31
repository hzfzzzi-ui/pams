-- PAMS account master and default account seeds.

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
