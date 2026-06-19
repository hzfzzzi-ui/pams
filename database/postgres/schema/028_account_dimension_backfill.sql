-- 028_account_dimension_backfill.sql
--
-- Complete the account dimension after the core table consolidation:
-- - add account master rows for account codes already present in facts/source rows
-- - preserve original fact account_code values; no fact rewrite is performed
-- - harden account references with foreign keys after the dimension is complete

BEGIN;

WITH account_seed (
    account_code,
    display_name,
    base_currency,
    is_asset,
    active,
    account_type,
    institution,
    region,
    owner_ratio,
    account_role,
    status,
    sort_order,
    source,
    raw_json
) AS (
    VALUES
        ('ccb_7332', '建行储蓄卡 7332', 'CNY', true, true, 'bank', '建设银行', 'CN', 1::numeric, 'cash', 'active', 210, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"preserve source account granularity"}'::jsonb),
        ('ccb_credit_0453', '建行信用卡 0453', 'CNY', false, true, 'credit_card', '建设银行', 'CN', 1::numeric, 'liability', 'active', 220, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"credit account kept separate from bank cash account"}'::jsonb),
        ('gf_9711', '广发银行卡 9711', 'CNY', true, true, 'bank', '广发银行', 'CN', 1::numeric, 'cash', 'active', 230, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"preserve card-level source account"}'::jsonb),
        ('gf_7443', '广发银行卡 7443', 'CNY', true, true, 'bank', '广发银行', 'CN', 1::numeric, 'cash', 'active', 240, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"preserve card-level source account"}'::jsonb),
        ('alipay', '支付宝', 'CNY', true, true, 'wallet', '支付宝', 'CN', 1::numeric, 'cash', 'active', 250, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"wallet account"}'::jsonb),
        ('wechat_balance', '微信零钱', 'CNY', true, true, 'wallet', '微信', 'CN', 1::numeric, 'cash', 'active', 260, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"counter/reconciliation account code referenced by facts","normalization":"wallet sub-account"}'::jsonb),
        ('wechat_lqt', '微信零钱通', 'CNY', true, true, 'wallet', '微信', 'CN', 1::numeric, 'cash', 'active', 270, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"reconciliation account code referenced by facts","normalization":"wallet money-market sub-account"}'::jsonb),
        ('medical_insurance', '医保账户', 'CNY', true, true, 'benefit', '医保', 'CN', 1::numeric, 'benefit', 'active', 280, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"benefit account"}'::jsonb),
        ('icbc_6689_cny', '工行账户 6689 CNY', 'CNY', true, true, 'bank', '工商银行', 'CN', 1::numeric, 'cash', 'active', 290, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"currency-specific bank account"}'::jsonb),
        ('icbc_6689_usd', '工行账户 6689 USD', 'USD', true, true, 'bank', '工商银行', 'CN', 1::numeric, 'cash', 'active', 300, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"account_code referenced by cashflow/source rows","normalization":"currency-specific bank account"}'::jsonb),
        ('direct_investment', '直接投资桥账户', 'CNY', true, true, 'bridge', 'PAMS', 'CN', 1::numeric, 'bridge', 'active', 310, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"counter/reconciliation account code referenced by investment funding records","normalization":"bridge account, not a broker connection"}'::jsonb),
        ('huawei_employer', '华为雇主权益来源', 'CNY', true, true, 'benefit', '华为', 'CN', 1::numeric, 'benefit', 'active', 320, 'migration_028_account_dimension', '{"migration":"028_account_dimension_backfill","reason":"counter/reconciliation account code referenced by benefit records","normalization":"benefit source account"}'::jsonb)
)
INSERT INTO pams_accounts (
    account_code,
    display_name,
    base_currency,
    is_asset,
    active,
    account_type,
    institution,
    region,
    owner_ratio,
    account_role,
    status,
    sort_order,
    source,
    raw_json,
    created_at,
    updated_at
)
SELECT
    account_code,
    display_name,
    base_currency,
    is_asset,
    active,
    account_type,
    institution,
    region,
    owner_ratio,
    account_role,
    status,
    sort_order,
    source,
    raw_json,
    now(),
    now()
FROM account_seed
ON CONFLICT (account_code) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    base_currency = EXCLUDED.base_currency,
    is_asset = EXCLUDED.is_asset,
    active = EXCLUDED.active,
    account_type = EXCLUDED.account_type,
    institution = EXCLUDED.institution,
    region = EXCLUDED.region,
    owner_ratio = EXCLUDED.owner_ratio,
    account_role = EXCLUDED.account_role,
    status = EXCLUDED.status,
    sort_order = EXCLUDED.sort_order,
    source = EXCLUDED.source,
    raw_json = EXCLUDED.raw_json,
    updated_at = now()
WHERE pams_accounts.source = 'migration_028_account_dimension';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_cashflow_fact_account_code'
    ) THEN
        ALTER TABLE pams_cashflow_fact
            ADD CONSTRAINT fk_pams_cashflow_fact_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_cashflow_fact_counter_account_code'
    ) THEN
        ALTER TABLE pams_cashflow_fact
            ADD CONSTRAINT fk_pams_cashflow_fact_counter_account_code
            FOREIGN KEY (counter_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_source_row_account_code'
    ) THEN
        ALTER TABLE pams_source_row
            ADD CONSTRAINT fk_pams_source_row_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_external_transaction_source_account_code'
    ) THEN
        ALTER TABLE pams_external_transaction_source
            ADD CONSTRAINT fk_pams_external_transaction_source_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_investment_trade_fact_account_code'
    ) THEN
        ALTER TABLE pams_investment_trade_fact
            ADD CONSTRAINT fk_pams_investment_trade_fact_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_investment_trade_snapshot_account_code'
    ) THEN
        ALTER TABLE pams_investment_trade_snapshot
            ADD CONSTRAINT fk_pams_investment_trade_snapshot_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_reconciliation_case_from_account_code'
    ) THEN
        ALTER TABLE pams_reconciliation_case
            ADD CONSTRAINT fk_pams_reconciliation_case_from_account_code
            FOREIGN KEY (from_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_reconciliation_case_to_account_code'
    ) THEN
        ALTER TABLE pams_reconciliation_case
            ADD CONSTRAINT fk_pams_reconciliation_case_to_account_code
            FOREIGN KEY (to_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;
END $$;

ALTER TABLE pams_cashflow_fact VALIDATE CONSTRAINT fk_pams_cashflow_fact_account_code;
ALTER TABLE pams_cashflow_fact VALIDATE CONSTRAINT fk_pams_cashflow_fact_counter_account_code;
ALTER TABLE pams_source_row VALIDATE CONSTRAINT fk_pams_source_row_account_code;
ALTER TABLE pams_external_transaction_source VALIDATE CONSTRAINT fk_pams_external_transaction_source_account_code;
ALTER TABLE pams_investment_trade_fact VALIDATE CONSTRAINT fk_pams_investment_trade_fact_account_code;
ALTER TABLE pams_investment_trade_snapshot VALIDATE CONSTRAINT fk_pams_investment_trade_snapshot_account_code;
ALTER TABLE pams_reconciliation_case VALIDATE CONSTRAINT fk_pams_reconciliation_case_from_account_code;
ALTER TABLE pams_reconciliation_case VALIDATE CONSTRAINT fk_pams_reconciliation_case_to_account_code;

COMMIT;
