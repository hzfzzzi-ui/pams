-- Backfill Shenzhen social insurance benefit accounts from the user's 2025
-- personal rights PDF and 2026 base-salary estimate. Pension and medical rows
-- are personal-account assets. Unemployment, work-injury and maternity are
-- kept as non-NAV coverage subaccounts because they do not create an accessible
-- personal balance.

BEGIN;

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
    updated_at
)
VALUES
    ('pension_insurance', '深圳养老保险', 'CNY', TRUE,  TRUE, 'benefit', '深圳社保', 'CN', 1, 'benefit', 'active', 270, 'migration_063_social_insurance_benefit', '{"parent_account_code":"hpf","benefit_kind":"pension_personal_account"}'::jsonb, NOW()),
    ('unemployment_insurance', '深圳失业保险', 'CNY', FALSE, TRUE, 'benefit', '深圳社保', 'CN', 1, 'benefit', 'active', 272, 'migration_063_social_insurance_benefit', '{"parent_account_code":"hpf","benefit_kind":"coverage_only","include_in_nav":false}'::jsonb, NOW()),
    ('work_injury_insurance', '深圳工伤保险', 'CNY', FALSE, TRUE, 'benefit', '深圳社保', 'CN', 1, 'benefit', 'active', 273, 'migration_063_social_insurance_benefit', '{"parent_account_code":"hpf","benefit_kind":"coverage_only","include_in_nav":false}'::jsonb, NOW()),
    ('maternity_insurance', '深圳生育保险', 'CNY', FALSE, TRUE, 'benefit', '深圳医保', 'CN', 1, 'benefit', 'active', 274, 'migration_063_social_insurance_benefit', '{"parent_account_code":"hpf","benefit_kind":"coverage_only","include_in_nav":false}'::jsonb, NOW())
ON CONFLICT (account_code) DO UPDATE
SET display_name = EXCLUDED.display_name,
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
    raw_json = pams_accounts.raw_json || EXCLUDED.raw_json,
    updated_at = NOW();

WITH assets AS (
    SELECT *
    FROM (
        VALUES
            ('ASSET_BENEFIT_PENSION_SHENZHEN', '深圳养老保险个人账户', 'PENSION_SHENZHEN', '养老保险个人账户', 'official_statement'),
            ('ASSET_BENEFIT_MEDICAL_SHENZHEN', '深圳医保个人账户', 'MEDICAL_SHENZHEN', '医保个人账户', 'official_statement')
    ) AS t(asset_id, display_name, primary_symbol, subtype, valuation_method)
)
INSERT INTO pams_asset_master (
    asset_id,
    canonical_name,
    display_name,
    primary_symbol,
    asset_type,
    asset_subtype,
    market,
    listing_region,
    economic_region,
    currency,
    valuation_method,
    liquidity_profile,
    income_type,
    exposure_policy,
    status,
    source,
    confidence,
    raw_json,
    last_verified_at,
    created_at,
    updated_at
)
SELECT
    asset_id,
    display_name,
    display_name,
    primary_symbol,
    '现金',
    subtype,
    'CN',
    'CN',
    'CN',
    'CNY',
    valuation_method,
    'restricted_benefit_account',
    'benefit_accrual',
    'cash_like_benefit',
    'active',
    'migration_063_social_insurance_benefit',
    'high',
    jsonb_build_object(
        'migration', '063_social_insurance_benefit_backfill',
        'note', 'Benefit personal-account balance from official statement or 2026 base estimate.'
    ),
    NOW(),
    NOW(),
    NOW()
FROM assets
ON CONFLICT (asset_id) DO UPDATE
SET canonical_name = EXCLUDED.canonical_name,
    display_name = EXCLUDED.display_name,
    primary_symbol = EXCLUDED.primary_symbol,
    asset_type = EXCLUDED.asset_type,
    asset_subtype = EXCLUDED.asset_subtype,
    market = EXCLUDED.market,
    listing_region = EXCLUDED.listing_region,
    economic_region = EXCLUDED.economic_region,
    currency = EXCLUDED.currency,
    valuation_method = EXCLUDED.valuation_method,
    liquidity_profile = EXCLUDED.liquidity_profile,
    income_type = EXCLUDED.income_type,
    exposure_policy = EXCLUDED.exposure_policy,
    status = EXCLUDED.status,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    raw_json = pams_asset_master.raw_json || EXCLUDED.raw_json,
    last_verified_at = NOW(),
    updated_at = NOW();

WITH aliases AS (
    SELECT *
    FROM (
        VALUES
            ('ASSET_BENEFIT_PENSION_SHENZHEN', 'account_code', 'pension_insurance', 100),
            ('ASSET_BENEFIT_PENSION_SHENZHEN', 'name', '深圳养老保险', 90),
            ('ASSET_BENEFIT_PENSION_SHENZHEN', 'name', '养老保险个人账户', 80),
            ('ASSET_BENEFIT_MEDICAL_SHENZHEN', 'account_code', 'medical_insurance', 100),
            ('ASSET_BENEFIT_MEDICAL_SHENZHEN', 'name', '深圳医保', 90),
            ('ASSET_BENEFIT_MEDICAL_SHENZHEN', 'name', '医保个人账户', 80)
    ) AS t(asset_id, alias_type, alias_value, priority)
)
INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority, created_at)
SELECT asset_id, alias_type, alias_value, 'migration_063_social_insurance_benefit', priority, NOW()
FROM aliases
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE
SET source = EXCLUDED.source,
    priority = EXCLUDED.priority;

WITH industry_rows AS (
    SELECT *
    FROM (
        VALUES
            ('ASSET_BENEFIT_PENSION_SHENZHEN', 'pams_internal', '养老保险', '福利账户', '现金/短久期', 'cash_like', 'migration_063_social_insurance_benefit'),
            ('ASSET_BENEFIT_MEDICAL_SHENZHEN', 'pams_internal', '医保个人账户', '福利账户', '现金/短久期', 'cash_like', 'migration_063_social_insurance_benefit')
    ) AS t(asset_id, industry_system, industry_leaf, industry_sub_bucket, industry_bucket, classification_scope, source)
)
INSERT INTO pams_asset_industry_map (
    asset_id,
    industry_system,
    industry_leaf,
    industry_sub_bucket,
    industry_bucket,
    classification_scope,
    source,
    confidence,
    evidence,
    as_of,
    is_primary,
    created_at,
    updated_at
)
SELECT
    asset_id,
    industry_system,
    industry_leaf,
    industry_sub_bucket,
    industry_bucket,
    classification_scope,
    source,
    'high',
    'Benefit account classification for social-insurance personal-account assets.',
    DATE '2026-06-22',
    TRUE,
    NOW(),
    NOW()
FROM industry_rows ir
WHERE NOT EXISTS (
    SELECT 1
    FROM pams_asset_industry_map m
    WHERE m.asset_id = ir.asset_id
      AND m.industry_system = ir.industry_system
      AND COALESCE(m.is_primary, FALSE) IS TRUE
);

WITH pension_rows AS (
    SELECT *
    FROM (
        VALUES
            (DATE '2025-01-31', '202501', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 26840.00::numeric, TRUE),
            (DATE '2025-02-28', '202502', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 28560.00::numeric, TRUE),
            (DATE '2025-03-31', '202503', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 30280.00::numeric, TRUE),
            (DATE '2025-04-30', '202504', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 32000.00::numeric, TRUE),
            (DATE '2025-05-31', '202505', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 33720.00::numeric, TRUE),
            (DATE '2025-06-30', '202506', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 35440.00::numeric, TRUE),
            (DATE '2025-07-31', '202507', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 37160.00::numeric, TRUE),
            (DATE '2025-08-31', '202508', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 38880.00::numeric, TRUE),
            (DATE '2025-09-30', '202509', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 40600.00::numeric, TRUE),
            (DATE '2025-10-31', '202510', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 42320.00::numeric, TRUE),
            (DATE '2025-11-30', '202511', 21500.00::numeric, 3440.00::numeric, 1720.00::numeric, 44040.00::numeric, TRUE),
            (DATE '2025-12-31', '202512', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 45920.00::numeric, TRUE),
            (DATE '2026-01-31', '202601', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 47800.00::numeric, FALSE),
            (DATE '2026-02-28', '202602', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 49680.00::numeric, FALSE),
            (DATE '2026-03-31', '202603', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 51560.00::numeric, FALSE),
            (DATE '2026-04-30', '202604', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 53440.00::numeric, FALSE),
            (DATE '2026-05-31', '202605', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 55320.00::numeric, FALSE),
            (DATE '2026-06-22', '202606', 23500.00::numeric, 3760.00::numeric, 1880.00::numeric, 57200.00::numeric, FALSE)
    ) AS t(event_date, period_id, contribution_base, employer_pension, employee_pension, account_balance, is_official_2025)
),
pension_prepared AS (
    SELECT
        event_date,
        period_id,
        contribution_base,
        employer_pension,
        employee_pension,
        account_balance,
        is_official_2025,
        CASE
            WHEN is_official_2025 THEN 'user_pdf_shenzhen_social_insurance_2025'
            ELSE 'estimated_social_insurance_2026_base23500'
        END::varchar AS source_table,
        'pension_' || period_id AS source_id
    FROM pension_rows
)
INSERT INTO pams_cashflow_fact (
    event_date,
    posting_date,
    account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    source_table,
    source_id,
    source_uid,
    source_hash,
    raw_json,
    classification_source,
    confidence,
    review_status,
    source_channel,
    auto_classified
)
SELECT
    event_date,
    event_date,
    'pension_insurance',
    employee_pension,
    'CNY',
    1,
    employee_pension,
    'operating_income',
    '福利资产',
    '养老保险个人账户',
    '深圳市社会保险基金管理局',
    '养老保险个人账户入账',
    '本月进入养老保险个人账户本金；缴费结构：个人' || employee_pension::text || '，单位统筹' || employer_pension::text || '，缴存基数' || contribution_base::text,
    TRUE,
    TRUE,
    FALSE,
    FALSE,
    source_table,
    source_id,
    source_id,
    md5('migration_063:pension:' || period_id),
    jsonb_build_object(
        'migration', '063_social_insurance_benefit_backfill',
        'source', CASE WHEN is_official_2025 THEN '深圳市2025年个人权益记录单.pdf' ELSE 'estimated_from_base_23500' END,
        'period', period_id,
        'contribution_base', contribution_base,
        'employee_pension', employee_pension,
        'employer_pension', employer_pension,
        'balance', account_balance,
        'statement_row', jsonb_build_object(
            'balance', account_balance,
            'summary', '养老保险个人账户入账',
            'amount', employee_pension,
            'currency', 'CNY'
        )
    ),
    CASE WHEN is_official_2025 THEN 'official_statement_pdf' ELSE 'base_estimate' END,
    CASE WHEN is_official_2025 THEN 1 ELSE 0.85 END,
    CASE WHEN is_official_2025 THEN 'confirmed' ELSE 'auto' END,
    CASE WHEN is_official_2025 THEN 'official_social_insurance_pdf' ELSE 'estimated_social_insurance' END,
    FALSE
FROM pension_prepared
ON CONFLICT (source_table, source_id) DO UPDATE
SET event_date = EXCLUDED.event_date,
    posting_date = EXCLUDED.posting_date,
    account_code = EXCLUDED.account_code,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    amount_cny = EXCLUDED.amount_cny,
    cashflow_type = EXCLUDED.cashflow_type,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    counterparty = EXCLUDED.counterparty,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    affects_spending = EXCLUDED.affects_spending,
    affects_nav = EXCLUDED.affects_nav,
    affects_investment_capital = EXCLUDED.affects_investment_capital,
    is_internal_transfer = EXCLUDED.is_internal_transfer,
    source_uid = EXCLUDED.source_uid,
    source_hash = EXCLUDED.source_hash,
    raw_json = pams_cashflow_fact.raw_json || EXCLUDED.raw_json,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    source_channel = EXCLUDED.source_channel,
    auto_classified = EXCLUDED.auto_classified,
    updated_at = NOW();

WITH medical_estimate AS (
    SELECT
        DATE '2026-06-22' AS event_date,
        '202606' AS period_id,
        23500.00::numeric AS contribution_base,
        470.00::numeric AS employee_medical_personal_account,
        1410.00::numeric AS employer_basic_medical,
        117.50::numeric AS employer_maternity,
        14476.62::numeric AS account_balance
)
INSERT INTO pams_cashflow_fact (
    event_date,
    posting_date,
    account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    source_table,
    source_id,
    source_uid,
    source_hash,
    raw_json,
    classification_source,
    confidence,
    review_status,
    source_channel,
    auto_classified
)
SELECT
    event_date,
    event_date,
    'medical_insurance',
    employee_medical_personal_account,
    'CNY',
    1,
    employee_medical_personal_account,
    'operating_income',
    '福利资产',
    '医保缴费',
    '华为技术有限公司',
    '医保个人账户入账',
    '按缴存基数23500推算：个人医保进入账户470.00，单位基本医保1410.00，单位生育保险117.50',
    TRUE,
    TRUE,
    FALSE,
    FALSE,
    'estimated_social_insurance_2026_base23500',
    'medical_' || period_id,
    'medical_' || period_id,
    md5('migration_063:medical:' || period_id),
    jsonb_build_object(
        'migration', '063_social_insurance_benefit_backfill',
        'source', 'estimated_from_base_23500',
        'period', period_id,
        'contribution_base', contribution_base,
        'employee_medical_personal_account', employee_medical_personal_account,
        'employer_basic_medical', employer_basic_medical,
        'employer_maternity', employer_maternity,
        'balance', account_balance,
        'statement_row', jsonb_build_object(
            'balance', account_balance,
            'summary', '医保个人账户入账',
            'amount', employee_medical_personal_account,
            'currency', 'CNY'
        )
    ),
    'base_estimate',
    0.85,
    'auto',
    'estimated_social_insurance',
    FALSE
FROM medical_estimate
ON CONFLICT (source_table, source_id) DO UPDATE
SET event_date = EXCLUDED.event_date,
    posting_date = EXCLUDED.posting_date,
    account_code = EXCLUDED.account_code,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    amount_cny = EXCLUDED.amount_cny,
    cashflow_type = EXCLUDED.cashflow_type,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    counterparty = EXCLUDED.counterparty,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    affects_spending = EXCLUDED.affects_spending,
    affects_nav = EXCLUDED.affects_nav,
    affects_investment_capital = EXCLUDED.affects_investment_capital,
    is_internal_transfer = EXCLUDED.is_internal_transfer,
    source_uid = EXCLUDED.source_uid,
    source_hash = EXCLUDED.source_hash,
    raw_json = pams_cashflow_fact.raw_json || EXCLUDED.raw_json,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    source_channel = EXCLUDED.source_channel,
    auto_classified = EXCLUDED.auto_classified,
    updated_at = NOW();

WITH benefit_balances AS (
    SELECT *
    FROM (
        VALUES
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2024-12-31', 25120.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-01-31', 26840.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-02-28', 28560.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-03-31', 30280.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-04-30', 32000.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-05-31', 33720.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-06-30', 35440.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-07-31', 37160.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-08-31', 38880.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-09-30', 40600.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-10-31', 42320.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-11-30', 44040.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2025-12-31', 45920.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-01-31', 47800.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-02-28', 49680.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-03-31', 51560.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-04-30', 53440.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-05-31', 55320.00::numeric),
            ('pension_insurance', 'PENSION_SHENZHEN', '深圳养老保险个人账户', '养老保险', '养老保险个人账户', 'ASSET_BENEFIT_PENSION_SHENZHEN', DATE '2026-06-22', 57200.00::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-01-31', 12126.62::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-02-28', 12596.62::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-03-31', 13066.62::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-04-30', 13536.62::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-05-31', 14006.62::numeric),
            ('medical_insurance', 'MEDICAL_SHENZHEN', '深圳医保个人账户', '医保', '医保个人账户', 'ASSET_BENEFIT_MEDICAL_SHENZHEN', DATE '2026-06-22', 14476.62::numeric)
    ) AS t(account_code, symbol, name, major_category, minor_category, asset_id, as_of, balance)
)
INSERT INTO pams_holdings_snapshot (
    symbol,
    name,
    asset_liability,
    currency,
    account_code,
    major_category,
    minor_category,
    market_value,
    cost,
    as_of,
    quantity,
    buy_price,
    current_price,
    asset_id,
    created_at,
    updated_at
)
SELECT
    symbol,
    name,
    '资产',
    'CNY',
    account_code,
    major_category,
    minor_category,
    balance,
    balance,
    as_of,
    balance,
    1,
    1,
    asset_id,
    NOW(),
    NOW()
FROM benefit_balances
ON CONFLICT (account_code, name, as_of) DO UPDATE
SET symbol = EXCLUDED.symbol,
    asset_liability = EXCLUDED.asset_liability,
    currency = EXCLUDED.currency,
    major_category = EXCLUDED.major_category,
    minor_category = EXCLUDED.minor_category,
    market_value = EXCLUDED.market_value,
    cost = EXCLUDED.cost,
    quantity = EXCLUDED.quantity,
    buy_price = EXCLUDED.buy_price,
    current_price = EXCLUDED.current_price,
    asset_id = EXCLUDED.asset_id,
    updated_at = NOW();

COMMIT;
