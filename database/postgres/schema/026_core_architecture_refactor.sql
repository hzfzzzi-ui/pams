-- PAMS target-architecture database refactor.
--
-- Scope:
-- - Keep P0/P1 target tables in public.
-- - Move legacy/module/external-market tables out of public as base tables.
-- - Create compatibility views in public for moved tables.
-- - Do not drop source data in this migration.

CREATE SCHEMA IF NOT EXISTS pams_legacy;
CREATE SCHEMA IF NOT EXISTS pams_module;
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
    ADD COLUMN IF NOT EXISTS budget_id INTEGER,
    ADD COLUMN IF NOT EXISTS linked_holding_snapshot_id BIGINT;

ALTER TABLE IF EXISTS public.pams_investment_trade_fact
    ADD COLUMN IF NOT EXISTS asset_major_category TEXT,
    ADD COLUMN IF NOT EXISTS asset_minor_category TEXT,
    ADD COLUMN IF NOT EXISTS execution_status TEXT,
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS created_by TEXT;

ALTER TABLE IF EXISTS public.pams_holding_return_snapshot
    ADD COLUMN IF NOT EXISTS holdings_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS accounts_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS data_completeness TEXT;

-- ---------------------------------------------------------------------------
-- New target tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.pams_investment_trade_snapshot (
    snapshot_date DATE NOT NULL,
    section TEXT NOT NULL DEFAULT 'total',
    account_code TEXT NOT NULL DEFAULT 'total',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    trade_count INTEGER NOT NULL DEFAULT 0,
    buy_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    sell_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    deposit_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    withdraw_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    dividend_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    fee_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    tax_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    realized_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    net_cash NUMERIC(28, 10) NOT NULL DEFAULT 0,
    rows_count INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, section, account_code, base_currency)
);

CREATE INDEX IF NOT EXISTS idx_pams_trade_snapshot_account_date
    ON public.pams_investment_trade_snapshot (account_code, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS public.pams_asset_exposure_relation (
    id BIGSERIAL PRIMARY KEY,
    asset_id VARCHAR(120) NOT NULL REFERENCES public.pams_asset_master(asset_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    related_asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    relation_key TEXT NOT NULL,
    relation_name TEXT,
    relation_level TEXT,
    relation_family TEXT,
    relation_role TEXT,
    weight NUMERIC(28, 10),
    score NUMERIC(12, 6),
    as_of DATE,
    source TEXT NOT NULL DEFAULT 'system',
    confidence TEXT NOT NULL DEFAULT 'medium',
    classification_scope TEXT,
    coverage_type TEXT,
    evidence TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_pams_asset_exposure_relation_type CHECK (
        relation_type IN ('industry', 'theme', 'component')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pams_asset_exposure_relation
    ON public.pams_asset_exposure_relation (
        asset_id,
        relation_type,
        COALESCE(related_asset_id, ''),
        relation_key,
        COALESCE(relation_level, ''),
        COALESCE(as_of, DATE '0001-01-01'),
        COALESCE(coverage_type, '')
    );

CREATE INDEX IF NOT EXISTS idx_pams_asset_exposure_relation_asset
    ON public.pams_asset_exposure_relation (asset_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_pams_asset_exposure_relation_key
    ON public.pams_asset_exposure_relation (relation_type, relation_key);

CREATE TABLE IF NOT EXISTS public.pams_asset_market_daily (
    as_of DATE NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    asset_id VARCHAR(120) REFERENCES public.pams_asset_master(asset_id) ON DELETE SET NULL,
    symbol TEXT,
    name TEXT,
    market TEXT,
    currency TEXT NOT NULL DEFAULT 'CNY',
    open_price NUMERIC(28, 10),
    high_price NUMERIC(28, 10),
    low_price NUMERIC(28, 10),
    close_price NUMERIC(28, 10),
    unit_nav NUMERIC(28, 10),
    estimated_unit_nav NUMERIC(28, 10),
    estimated_market_value NUMERIC(28, 10),
    volume NUMERIC(28, 10),
    turnover NUMERIC(28, 10),
    confidence TEXT NOT NULL DEFAULT 'medium',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (as_of, source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_market_daily_symbol_date
    ON public.pams_asset_market_daily (symbol, as_of DESC);

CREATE TABLE IF NOT EXISTS public.pams_source_row (
    source_row_id BIGSERIAL PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_uid TEXT,
    account_code TEXT,
    event_date DATE,
    source_file TEXT,
    row_hash TEXT,
    amount NUMERIC(28, 10),
    currency TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_pams_source_row_account_date
    ON public.pams_source_row (account_code, event_date DESC);

CREATE TABLE IF NOT EXISTS public.pams_reconciliation_case (
    case_id BIGSERIAL PRIMARY KEY,
    case_type TEXT NOT NULL,
    relation_code TEXT,
    from_account_code TEXT,
    to_account_code TEXT,
    source_ref_table TEXT,
    source_ref_id TEXT,
    target_ref_table TEXT,
    target_ref_id TEXT,
    amount NUMERIC(28, 10),
    currency TEXT,
    case_date DATE,
    confidence NUMERIC(12, 6),
    status TEXT NOT NULL DEFAULT 'open',
    review_action TEXT,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    note TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pams_reconciliation_case_status
    ON public.pams_reconciliation_case (status, case_date DESC);

CREATE TABLE IF NOT EXISTS public.pams_external_transaction_source (
    external_source_id BIGSERIAL PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_key TEXT,
    account_code TEXT,
    trade_date DATE,
    trade_time TIMESTAMPTZ,
    symbol TEXT,
    name TEXT,
    side TEXT,
    quantity NUMERIC(28, 10),
    price NUMERIC(28, 10),
    gross_amount NUMERIC(28, 10),
    fee_amount NUMERIC(28, 10),
    net_cash NUMERIC(28, 10),
    realized_pnl NUMERIC(28, 10),
    currency TEXT,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_pams_external_transaction_source_account_date
    ON public.pams_external_transaction_source (account_code, trade_date DESC);

CREATE TABLE IF NOT EXISTS public.pams_data_job_run (
    job_run_id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL,
    job_type TEXT NOT NULL,
    source_table TEXT,
    source_id TEXT,
    asset_scope TEXT,
    target_date DATE,
    data_as_of DATE,
    status TEXT NOT NULL DEFAULT 'unknown',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    rows_affected BIGINT,
    checksum TEXT,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_pams_data_job_run_key_date
    ON public.pams_data_job_run (job_key, COALESCE(target_date, data_as_of) DESC);

-- ---------------------------------------------------------------------------
-- Backfills into target tables
-- ---------------------------------------------------------------------------

INSERT INTO public.pams_cashflow_fact (
    event_date, posting_date, account_code, amount, currency, fx_rate_to_cny,
    amount_cny, cashflow_type, category, subcategory, summary, description,
    affects_spending, affects_nav, is_internal_transfer,
    source_table, source_id, source_uid, source_hash, raw_json,
    classification_source, confidence, review_status,
    source_channel, location, auto_classified, budget_id
)
SELECT
    t.date,
    t.date,
    COALESCE(NULLIF(t.account_code, ''), 'unknown'),
    t.amount,
    COALESCE(NULLIF(t.currency, ''), 'CNY'),
    1,
    t.amount,
    CASE
        WHEN COALESCE(t.category, '') = '内部划转' THEN 'internal_transfer'
        WHEN t.amount >= 0 THEN 'operating_income'
        ELSE 'operating_expense'
    END,
    t.category,
    t.subcategory,
    t.description,
    t.description,
    COALESCE(t.category, '') <> '内部划转',
    TRUE,
    COALESCE(t.category, '') = '内部划转',
    'pams_transactions',
    t.id::text,
    'pams_transactions:' || t.id::text,
    md5('pams_transactions:' || t.id::text),
    to_jsonb(t),
    CASE WHEN COALESCE(t.auto_classified, FALSE) THEN 'legacy_rule' ELSE 'legacy_manual' END,
    COALESCE(t.confidence, 0)::numeric,
    'legacy_imported',
    t.source,
    t.location,
    t.auto_classified,
    t.budget_id
FROM public.pams_transactions t
WHERE to_regclass('public.pams_transactions') IS NOT NULL
ON CONFLICT DO NOTHING;

UPDATE public.pams_cashflow_fact f
SET
    source_channel = COALESCE(f.source_channel, f.raw_json ->> 'source'),
    location = COALESCE(f.location, f.raw_json ->> 'location'),
    auto_classified = COALESCE(f.auto_classified, NULLIF(f.raw_json ->> 'auto_classified', '')::boolean),
    budget_id = COALESCE(f.budget_id, NULLIF(f.raw_json ->> 'budget_id', '')::integer)
WHERE f.source_table = 'pams_transactions';

UPDATE public.pams_investment_trade_fact f
SET
    asset_major_category = COALESCE(f.asset_major_category, tx.major_category),
    asset_minor_category = COALESCE(f.asset_minor_category, tx.minor_category),
    execution_status = COALESCE(f.execution_status, tx.status),
    notes = COALESCE(f.notes, tx.notes),
    created_by = COALESCE(f.created_by, tx.source)
FROM public.pams_investment_transactions tx
WHERE to_regclass('public.pams_investment_transactions') IS NOT NULL
  AND f.source_table = 'pams_investment_transactions'
  AND f.source_id = tx.id::text;

UPDATE public.pams_holding_return_snapshot s
SET
    holdings_count = COALESCE(h.holdings_count, s.holdings_count),
    accounts_count = COALESCE(h.accounts_count, s.accounts_count),
    data_completeness = COALESCE(s.data_completeness, 'snapshot_backfilled')
FROM (
    SELECT as_of, COUNT(*)::integer AS holdings_count, COUNT(DISTINCT account_code)::integer AS accounts_count
    FROM public.pams_holdings_snapshot
    GROUP BY as_of
) h
WHERE s.snapshot_date = h.as_of;

INSERT INTO public.pams_investment_trade_snapshot (
    snapshot_date, section, account_code, base_currency, trade_count,
    buy_amount, sell_amount, deposit_amount, withdraw_amount, dividend_amount,
    fee_amount, tax_amount, realized_pnl, net_cash, rows_count, source_hash, raw_json
)
SELECT
    f.trade_date,
    'total',
    COALESCE(NULLIF(f.account_code, ''), 'unknown'),
    'CNY',
    COUNT(*)::integer,
    COALESCE(SUM(CASE WHEN f.trade_type IN ('buy', 'fund_subscription') THEN ABS(COALESCE(f.gross_amount, f.net_cash, 0) * COALESCE(f.fx_rate_to_cny, 1)) ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN f.trade_type IN ('sell', 'fund_redemption') THEN ABS(COALESCE(f.gross_amount, f.net_cash, 0) * COALESCE(f.fx_rate_to_cny, 1)) ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN f.side IN ('deposit', 'cash_deposit', 'transfer_in') THEN ABS(COALESCE(f.net_cash_cny, f.net_cash * f.fx_rate_to_cny, 0)) ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN f.side IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN ABS(COALESCE(f.net_cash_cny, f.net_cash * f.fx_rate_to_cny, 0)) ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN f.trade_type IN ('dividend', 'interest', 'coupon') THEN ABS(COALESCE(f.net_cash_cny, f.realized_pnl_cny, 0)) ELSE 0 END), 0),
    COALESCE(SUM(ABS(COALESCE(f.fee_amount, 0) * COALESCE(f.fx_rate_to_cny, 1))), 0),
    COALESCE(SUM(ABS(COALESCE(f.tax_amount, 0) * COALESCE(f.fx_rate_to_cny, 1))), 0),
    COALESCE(SUM(COALESCE(f.realized_pnl_cny, 0)), 0),
    COALESCE(SUM(COALESCE(f.net_cash_cny, f.net_cash * f.fx_rate_to_cny, 0)), 0),
    COUNT(*)::integer,
    md5('pams_investment_trade_fact:' || f.trade_date::text || ':' || COALESCE(NULLIF(f.account_code, ''), 'unknown') || ':' || COUNT(*)::text),
    jsonb_build_object('source_table', 'pams_investment_trade_fact', 'aggregation', 'daily_account_total')
FROM public.pams_investment_trade_fact f
GROUP BY f.trade_date, COALESCE(NULLIF(f.account_code, ''), 'unknown')
ON CONFLICT (snapshot_date, section, account_code, base_currency) DO UPDATE SET
    trade_count = EXCLUDED.trade_count,
    buy_amount = EXCLUDED.buy_amount,
    sell_amount = EXCLUDED.sell_amount,
    deposit_amount = EXCLUDED.deposit_amount,
    withdraw_amount = EXCLUDED.withdraw_amount,
    dividend_amount = EXCLUDED.dividend_amount,
    fee_amount = EXCLUDED.fee_amount,
    tax_amount = EXCLUDED.tax_amount,
    realized_pnl = EXCLUDED.realized_pnl,
    net_cash = EXCLUDED.net_cash,
    rows_count = EXCLUDED.rows_count,
    source_hash = EXCLUDED.source_hash,
    raw_json = EXCLUDED.raw_json,
    updated_at = NOW();

INSERT INTO public.pams_asset_exposure_relation (
    asset_id, relation_type, relation_key, relation_name, relation_level,
    relation_family, weight, as_of, source, confidence, classification_scope,
    evidence, raw_json
)
SELECT
    m.asset_id,
    'industry',
    concat_ws(':', m.industry_system, m.classification_scope, m.industry_bucket, m.industry_sub_bucket, m.industry_leaf),
    COALESCE(m.industry_leaf, m.industry_sub_bucket, m.industry_bucket),
    CASE WHEN m.industry_leaf IS NOT NULL THEN 'leaf' WHEN m.industry_sub_bucket IS NOT NULL THEN 'sub_bucket' ELSE 'bucket' END,
    m.industry_system,
    CASE WHEN m.is_primary THEN 1 ELSE NULL END,
    m.as_of,
    m.source,
    m.confidence,
    m.classification_scope,
    m.evidence,
    jsonb_build_object(
        'source_table', 'pams_asset_industry_map',
        'industry_bucket', m.industry_bucket,
        'industry_sub_bucket', m.industry_sub_bucket,
        'industry_leaf', m.industry_leaf,
        'is_primary', m.is_primary
    )
FROM public.pams_asset_industry_map m
WHERE to_regclass('public.pams_asset_industry_map') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_asset_exposure_relation (
    asset_id, relation_type, relation_key, relation_name, relation_level,
    relation_family, relation_role, score, as_of, source, confidence,
    evidence, raw_json
)
SELECT
    m.asset_id,
    'theme',
    m.theme_id,
    m.theme_name,
    m.theme_level,
    m.family,
    m.role,
    m.score,
    m.as_of,
    m.source,
    m.confidence,
    m.evidence,
    jsonb_build_object(
        'source_table', 'pams_asset_theme_map',
        'parent_theme_id', m.parent_theme_id
    )
FROM public.pams_asset_theme_map m
WHERE to_regclass('public.pams_asset_theme_map') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_asset_exposure_relation (
    asset_id, relation_type, related_asset_id, relation_key, relation_name,
    relation_level, weight, as_of, source, confidence, coverage_type, raw_json
)
SELECT
    p.fund_asset_id,
    'component',
    p.underlying_asset_id,
    COALESCE(p.underlying_asset_id, p.underlying_symbol, p.underlying_name, 'unknown'),
    p.underlying_name,
    p.underlying_asset_type,
    p.underlying_weight,
    p.as_of,
    p.source,
    p.confidence,
    p.coverage_type,
    jsonb_build_object(
        'source_table', 'pams_fund_underlying_position',
        'underlying_symbol', p.underlying_symbol,
        'effective_source', p.effective_source,
        'raw_json', p.raw_json
    )
FROM public.pams_fund_underlying_position p
WHERE to_regclass('public.pams_fund_underlying_position') IS NOT NULL
ON CONFLICT DO NOTHING;

DO $optional$
BEGIN
    IF to_regclass('public.pams_cn_etf_daily') IS NOT NULL THEN
        EXECUTE $statement$
INSERT INTO public.pams_asset_market_daily (
    as_of, source, source_key, symbol, market, currency,
    open_price, high_price, low_price, close_price, volume, turnover, raw_json
)
SELECT
    d.trade_date,
    COALESCE(NULLIF(d.source, ''), 'pams_cn_etf_daily'),
    'pams_cn_etf_daily:' || COALESCE(d.ts_code, d.symbol, '') || ':' || d.trade_date::text,
    COALESCE(d.ts_code, d.symbol),
    'CN_ETF',
    'CNY',
    d.open::numeric,
    d.high::numeric,
    d.low::numeric,
    d.close::numeric,
    d.volume::numeric,
    d.turnover::numeric,
    to_jsonb(d)
FROM public.pams_cn_etf_daily d
WHERE to_regclass('public.pams_cn_etf_daily') IS NOT NULL
ON CONFLICT DO NOTHING;
$statement$;
    END IF;
END
$optional$;

DO $optional$
BEGIN
    IF to_regclass('public.pams_fund_nav_estimates') IS NOT NULL THEN
        EXECUTE $statement$
INSERT INTO public.pams_asset_market_daily (
    as_of, source, source_key, symbol, name, market, currency,
    unit_nav, estimated_unit_nav, estimated_market_value, confidence, raw_json
)
SELECT
    e.as_of,
    'pams_fund_nav_estimates',
    'pams_fund_nav_estimates:' || e.id::text,
    e.symbol,
    e.name,
    'FUND_NAV',
    'CNY',
    e.confirmed_unit_nav,
    e.estimated_unit_nav,
    e.estimated_market_value,
    CASE WHEN e.status = 'confirmed' THEN 'high' ELSE 'medium' END,
    to_jsonb(e)
FROM public.pams_fund_nav_estimates e
WHERE to_regclass('public.pams_fund_nav_estimates') IS NOT NULL
ON CONFLICT DO NOTHING;
$statement$;
    END IF;
END
$optional$;

INSERT INTO public.pams_source_row (
    source_system, source_table, source_id, source_uid, account_code, event_date,
    source_file, amount, currency, raw_json, imported_at
)
SELECT
    'bank_statement_import',
    'pams_bank_statement_imports',
    i.id::text,
    i.profile_code || ':' || i.id::text,
    i.account_code,
    i.statement_end,
    i.source_file,
    i.statement_balance,
    NULL,
    to_jsonb(i),
    i.imported_at
FROM public.pams_bank_statement_imports i
WHERE to_regclass('public.pams_bank_statement_imports') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_source_row (
    source_system, source_table, source_id, source_uid, account_code, event_date,
    row_hash, amount, currency, raw_json, imported_at
)
SELECT
    'bank_statement_row',
    'pams_bank_statement_rows',
    r.id::text,
    r.source_uid,
    r.account_code,
    r.transaction_date,
    r.source_uid,
    r.amount,
    r.currency,
    to_jsonb(r),
    r.created_at
FROM public.pams_bank_statement_rows r
WHERE to_regclass('public.pams_bank_statement_rows') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_reconciliation_case (
    case_type, relation_code, from_account_code, to_account_code,
    source_ref_table, source_ref_id, target_ref_table, target_ref_id,
    amount, currency, case_date, confidence, status, note, raw_json,
    created_at, updated_at
)
SELECT
    m.match_type,
    m.relation_code,
    m.from_account_code,
    m.to_account_code,
    'pams_bank_statement_rows',
    m.source_row_id::text,
    'pams_bank_statement_rows',
    m.target_row_id::text,
    m.amount,
    m.currency,
    m.match_date,
    m.confidence,
    m.status,
    m.note,
    to_jsonb(m),
    m.created_at,
    m.updated_at
FROM public.pams_bank_transfer_matches m
WHERE to_regclass('public.pams_bank_transfer_matches') IS NOT NULL;

INSERT INTO public.pams_reconciliation_case (
    case_type, source_ref_table, source_ref_id, amount, currency, case_date,
    status, review_action, reviewed_by, reviewed_at, note, raw_json, created_at, updated_at
)
SELECT
    'account_review',
    'pams_account_reconciliation_reviews',
    r.id::text,
    r.reviewed_balance,
    r.currency,
    r.balance_as_of,
    COALESCE(r.source_status, 'reviewed'),
    r.review_action,
    r.reviewed_by,
    r.reviewed_at,
    r.note,
    to_jsonb(r),
    r.reviewed_at,
    r.reviewed_at
FROM public.pams_account_reconciliation_reviews r
WHERE to_regclass('public.pams_account_reconciliation_reviews') IS NOT NULL;

INSERT INTO public.pams_external_transaction_source (
    source_system, source_table, source_id, source_key, account_code, trade_date,
    trade_time, symbol, name, side, quantity, price, gross_amount, fee_amount,
    net_cash, realized_pnl, currency, raw_json, created_at
)
SELECT
    'ibkr',
    'pams_ibkr_trades',
    t.id::text,
    t.source_trade_key,
    t.account_code,
    t.trade_date,
    t.trade_date::timestamptz,
    t.symbol,
    t.name,
    t.side,
    t.quantity,
    t.price,
    t.gross_amount,
    t.commission,
    t.net_cash,
    t.realized_pnl,
    t.currency,
    to_jsonb(t),
    t.created_at
FROM public.pams_ibkr_trades t
WHERE to_regclass('public.pams_ibkr_trades') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_external_transaction_source (
    source_system, source_table, source_id, source_key, account_code, trade_date,
    trade_time, symbol, name, side, quantity, price, gross_amount, fee_amount,
    currency, raw_json, created_at
)
SELECT
    'tradingview',
    'pams_tradingview_transactions',
    t.id::text,
    t.row_hash,
    t.account_code,
    t.closing_time::date,
    t.closing_time::timestamptz,
    COALESCE(t.normalized_symbol, t.symbol),
    t.symbol,
    t.side,
    t.quantity,
    t.fill_price,
    ABS(COALESCE(t.quantity, 0) * COALESCE(t.fill_price, 0)),
    t.commission,
    t.currency,
    to_jsonb(t),
    t.created_at::timestamptz
FROM public.pams_tradingview_transactions t
WHERE to_regclass('public.pams_tradingview_transactions') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_external_transaction_source (
    source_system, source_table, source_id, source_key, account_code, trade_date,
    trade_time, symbol, name, side, quantity, price, gross_amount, fee_amount,
    currency, raw_json, created_at
)
SELECT
    COALESCE(NULLIF(t.source, ''), 'manual_legacy'),
    'pams_investment_transactions',
    t.id::text,
    t.source_key,
    t.account_code,
    t.trade_date,
    t.trade_time,
    t.symbol,
    t.name,
    t.side,
    t.quantity,
    t.price,
    t.gross_amount,
    t.fees,
    t.currency,
    to_jsonb(t),
    t.created_at
FROM public.pams_investment_transactions t
WHERE to_regclass('public.pams_investment_transactions') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.pams_data_job_run (
    job_key, job_type, source_table, source_id, asset_scope, target_date,
    status, started_at, finished_at, rows_affected, summary_json, error_text, created_at
)
SELECT
    r.sync_key,
    'data_sync',
    'pams_data_sync_runs',
    r.id::text,
    r.asset_scope,
    r.target_trade_date,
    r.status,
    r.started_at,
    r.finished_at,
    r.rows_upserted,
    COALESCE(r.summary_json, '{}'::jsonb),
    r.error_text,
    r.created_at
FROM public.pams_data_sync_runs r
WHERE to_regclass('public.pams_data_sync_runs') IS NOT NULL
ON CONFLICT DO NOTHING;

DO $optional$
BEGIN
    IF to_regclass('public.pams_data_version') IS NOT NULL THEN
        EXECUTE $statement$
INSERT INTO public.pams_data_job_run (
    job_key, job_type, source_table, source_id, data_as_of, target_date,
    status, rows_affected, checksum, summary_json, created_at
)
SELECT
    'data_version',
    'data_version',
    'pams_data_version',
    v.id::text,
    v.as_of_date,
    v.max_trade_date,
    'recorded',
    COALESCE(v.rows_ohlcv, 0) + COALESCE(v.rows_index, 0) + COALESCE(v.rows_security, 0),
    v.checksum,
    jsonb_build_object(
        'rows_ohlcv', v.rows_ohlcv,
        'rows_index', v.rows_index,
        'rows_security', v.rows_security,
        'ingest_seconds', v.ingest_seconds,
        'notes', v.notes
    ),
    v.created_at
FROM public.pams_data_version v
WHERE to_regclass('public.pams_data_version') IS NOT NULL
ON CONFLICT DO NOTHING;
$statement$;
    END IF;
END
$optional$;

-- ---------------------------------------------------------------------------
-- Schema isolation with compatibility views
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION pg_temp.move_table_with_compat_view(table_name TEXT, target_schema TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    relkind_value "char";
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', target_schema);

    SELECT c.relkind
    INTO relkind_value
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname = table_name;

    IF relkind_value = 'r' THEN
        IF to_regclass(format('%I.%I', target_schema, table_name)) IS NULL THEN
            EXECUTE format('ALTER TABLE public.%I SET SCHEMA %I', table_name, target_schema);
        END IF;
    ELSIF relkind_value = 'v' THEN
        RETURN;
    END IF;

    IF to_regclass(format('%I.%I', target_schema, table_name)) IS NOT NULL THEN
        EXECUTE format('DROP VIEW IF EXISTS public.%I CASCADE', table_name);
        EXECUTE format('CREATE VIEW public.%I AS SELECT * FROM %I.%I', table_name, target_schema, table_name);
    END IF;
END;
$$;

-- Legacy/source tables replaced by target P0/P1 tables.
SELECT pg_temp.move_table_with_compat_view(name, 'pams_legacy')
FROM (
    VALUES
        ('pams_account_reconciliation_reviews'),
        ('pams_asset_categories'),
        ('pams_asset_class_map'),
        ('pams_asset_industry_map'),
        ('pams_asset_map'),
        ('pams_asset_theme_map'),
        ('pams_bank_statement_imports'),
        ('pams_bank_statement_rows'),
        ('pams_bank_transfer_matches'),
        ('pams_category_rules'),
        ('pams_cn_etf_daily'),
        ('pams_data_sync_runs'),
        ('pams_fund_nav_estimates'),
        ('pams_fund_underlying_position'),
        ('pams_ibkr_position_history'),
        ('pams_ibkr_trades'),
        ('pams_import_batches'),
        ('pams_investment_transactions'),
        ('pams_nav_daily'),
        ('pams_raw_rows'),
        ('pams_tradingview_transactions'),
        ('pams_transactions')
) AS t(name);

-- Optional modules kept out of the PAMS core schema.
SELECT pg_temp.move_table_with_compat_view(name, 'pams_module')
FROM (
    VALUES
        ('pams_asset_fixed_income_profile'),
        ('pams_asset_real_asset_profile'),
        ('pams_budgets'),
        ('pams_event_monitor'),
        ('pams_fundamental_snapshot'),
        ('pams_news_items'),
        ('pams_notifications'),
        ('pams_order_drafts'),
        ('pams_policy'),
        ('pams_position_cooldown'),
        ('pams_risk_events'),
        ('pams_risk_snapshot'),
        ('pams_sentiment_state'),
        ('pams_sim_accounts'),
        ('pams_sim_cash_ledger'),
        ('pams_sim_holdings_snapshots'),
        ('pams_sim_reconcile_summaries'),
        ('pams_sim_sync_logs'),
        ('pams_strategy_blacklist'),
        ('pams_strategy_candidates'),
        ('pams_strategy_state'),
        ('pams_task_queue'),
        ('pams_watchlist'),
        ('pams_watchlist_log')
) AS t(name);

-- External OHLCV system: exclude from PAMS audit while preserving public views.
SELECT pg_temp.move_table_with_compat_view(name, 'pams_external_market')
FROM (
    VALUES
        ('pams_ohlcv_daily'),
        ('pams_ohlcv_daily_v2'),
        ('pams_ohlcv_daily_v2_audit_daily'),
        ('pams_ohlcv_daily_v2_exceptions')
) AS t(name);

-- Document the target-state intent in the database itself.
COMMENT ON SCHEMA pams_legacy IS 'Legacy PAMS tables retained behind public compatibility views after target architecture refactor.';
COMMENT ON SCHEMA pams_module IS 'Optional PAMS module tables retained outside the core audit scope.';
COMMENT ON SCHEMA pams_external_market IS 'External OHLCV market-data system tables excluded from PAMS audit scope.';

COMMENT ON TABLE public.pams_asset_exposure_relation IS 'Unified asset exposure relationship table for industry, theme, and component/lookthrough relations.';
COMMENT ON TABLE public.pams_asset_market_daily IS 'PAMS valuation-oriented market daily table, excluding external OHLCV system tables.';
COMMENT ON TABLE public.pams_source_row IS 'Unified source-row ledger for imported non-fact source records.';
COMMENT ON TABLE public.pams_reconciliation_case IS 'Unified reconciliation and review case table.';
COMMENT ON TABLE public.pams_external_transaction_source IS 'Unified external/manual transaction source record table feeding investment trade facts.';
COMMENT ON TABLE public.pams_data_job_run IS 'Unified data job run and data-version audit table.';
COMMENT ON TABLE public.pams_investment_trade_snapshot IS 'Precomputed investment trade activity snapshot generated from pams_investment_trade_fact.';
