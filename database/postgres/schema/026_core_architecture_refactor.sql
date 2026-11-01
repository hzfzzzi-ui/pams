-- PAMS target-architecture objects for the empty-database manifest only.
-- This file does not migrate or backfill an existing database.
-- Shared v2 market relations remain public base tables.

CREATE SCHEMA IF NOT EXISTS pams_external_market;

-- ---------------------------------------------------------------------------
-- P0 field upgrades
-- ---------------------------------------------------------------------------

ALTER TABLE IF EXISTS public.pams_accounts
    ADD COLUMN IF NOT EXISTS account_type TEXT,
    ADD COLUMN IF NOT EXISTS institution TEXT,
    ADD COLUMN IF NOT EXISTS region TEXT,
    ADD COLUMN IF NOT EXISTS owner_ratio NUMERIC(12, 8) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS account_role TEXT,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS opened_at DATE,
    ADD COLUMN IF NOT EXISTS closed_at DATE,
    ADD COLUMN IF NOT EXISTS sort_order INTEGER,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE public.pams_accounts
SET
    status = CASE WHEN COALESCE(active, TRUE) THEN 'active' ELSE 'inactive' END,
    institution = COALESCE(institution, display_name),
    account_type = COALESCE(
        account_type,
        CASE
            WHEN account_code IN ('ibkr', 'tonghuashun') THEN 'brokerage'
            WHEN account_code IN ('tencent_licaitong', 'ccb_yhs300_fund') THEN 'fund_platform'
            WHEN account_code IN ('binance', 'wechat') THEN 'wallet'
            WHEN account_code IN ('loan', 'jiekuan', 'ccb_receivable_loan') OR COALESCE(is_asset, TRUE) = FALSE THEN 'liability'
            WHEN account_code IN ('gongjijin', 'hpf', 'huawei_stock') THEN 'benefit'
            ELSE 'bank'
        END
    ),
    account_role = COALESCE(
        account_role,
        CASE
            WHEN account_code IN ('ibkr', 'tonghuashun', 'tencent_licaitong', 'ccb_yhs300_fund', 'huawei_stock') THEN 'investment'
            WHEN account_code IN ('loan', 'jiekuan') OR COALESCE(is_asset, TRUE) = FALSE THEN 'liability'
            WHEN account_code IN ('ccb_receivable_loan') THEN 'receivable'
            WHEN account_code IN ('gongjijin', 'hpf') THEN 'benefit'
            ELSE 'cash'
        END
    ),
    updated_at = NOW();

ALTER TABLE IF EXISTS public.pams_cashflow_fact
    ADD COLUMN IF NOT EXISTS source_channel TEXT,
    ADD COLUMN IF NOT EXISTS location TEXT,
    ADD COLUMN IF NOT EXISTS auto_classified BOOLEAN,
    ADD COLUMN IF NOT EXISTS linked_holding_snapshot_id BIGINT;

ALTER TABLE IF EXISTS public.pams_investment_trade_fact
    ADD COLUMN IF NOT EXISTS asset_major_category TEXT,
    ADD COLUMN IF NOT EXISTS asset_minor_category TEXT,
    ADD COLUMN IF NOT EXISTS execution_status TEXT,
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS created_by TEXT;

-- ---------------------------------------------------------------------------
-- New target tables
-- ---------------------------------------------------------------------------

-- Fresh database initialization deliberately performs no historical backfill.

-- All shared market relations stay in their existing schemas. Independent
-- consumers may create indexes or alter public tables, which a view cannot
-- support. Older compatibility views remain untouched by this empty-db file.

-- Document the target-state intent in the database itself.
COMMENT ON SCHEMA pams_external_market IS 'External OHLCV market-data system tables excluded from PAMS audit scope.';

COMMENT ON TABLE public.market_observations IS 'PAMS daily, quote and account fund observations, excluding shared external OHLCV tables.';
