-- Backfill the missing CCB fixed-income wealth product into the formal
-- investment account ledger.  The bank cashflows existed, but the matching
-- platform holding/trade facts were missing from ccb_yhs300_fund, which made
-- the beginning NAV understate by roughly 50k CNY.

BEGIN;

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
    last_verified_at
)
VALUES (
    'ASSET_JYLC_WXGSJX',
    '交银理财稳享固收精选',
    '交银理财稳享固收精选',
    'JYLC_WXGSJX',
    '债券 / 货币基金',
    '固收理财',
    '理财',
    '中国大陆',
    '中国大陆',
    'CNY',
    'amortized_cost_or_nav',
    'daily_liquid_after_holding_period',
    'interest_like_return',
    'fixed_income',
    'active',
    'migration_061_ccb_wealth_fixed_income_nav_backfill',
    'high',
    jsonb_build_object(
        'migration', '061_ccb_wealth_fixed_income_nav_backfill',
        'product_code', 'JY581122514900000',
        'reason', 'CCB bank cashflow showed subscription/redemption but ccb_yhs300_fund had no matching platform facts'
    ),
    NOW()
)
ON CONFLICT (asset_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
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
    last_verified_at = EXCLUDED.last_verified_at,
    updated_at = NOW();

UPDATE pams_cashflow_fact
SET counter_account_code = 'ccb_yhs300_fund',
    category = '内部划转',
    subcategory = CASE
        WHEN cashflow_id = 19 THEN '理财申购'
        WHEN cashflow_id = 593 THEN '理财赎回'
        ELSE subcategory
    END,
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = TRUE,
    transfer_group_id = 'ccb_yhs300_fund:JYLC_WXGSJX:2025-12-12:2026-03-24',
    classification_source = 'user_confirmed',
    confidence = 1,
    review_status = 'confirmed',
    raw_json = raw_json || jsonb_build_object(
        'migration', '061_ccb_wealth_fixed_income_nav_backfill',
        'linked_account_code', 'ccb_yhs300_fund',
        'linked_asset_id', 'ASSET_JYLC_WXGSJX',
        'linked_product_code', 'JY581122514900000'
    ),
    updated_at = NOW()
WHERE cashflow_id IN (19, 593);

WITH daily AS (
    SELECT gs.as_of::date AS as_of
    FROM generate_series(DATE '2025-12-12', DATE '2026-03-23', INTERVAL '1 day') AS gs(as_of)
),
valued AS (
    SELECT
        as_of,
        CASE
            WHEN as_of <= DATE '2026-02-19' THEN
                ROUND(
                    50000
                    + (50206.91 - 50000)
                    * ((as_of - DATE '2025-12-12')::numeric / (DATE '2026-02-19' - DATE '2025-12-12')::numeric),
                    4
                )
            ELSE
                ROUND(
                    50206.91
                    + (50322.39 - 50206.91)
                    * ((as_of - DATE '2026-02-19')::numeric / (DATE '2026-03-24' - DATE '2026-02-19')::numeric),
                    4
                )
        END AS market_value
    FROM daily
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
    'JY581122514900000',
    '交银理财稳享固收精选日开47号（90天持有）',
    '资产',
    'CNY',
    'ccb_yhs300_fund',
    '理财',
    '固收理财',
    market_value,
    50000.0000,
    as_of,
    50000.000000,
    1.000000,
    ROUND(market_value / 50000.0, 6),
    'ASSET_JYLC_WXGSJX',
    NOW(),
    NOW()
FROM valued
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

INSERT INTO pams_investment_trade_fact (
    trade_date,
    settle_date,
    account_code,
    broker,
    asset_id,
    symbol,
    name,
    market,
    currency,
    trade_type,
    side,
    quantity,
    price,
    gross_amount,
    fee_amount,
    tax_amount,
    net_cash,
    fx_rate_to_cny,
    net_cash_cny,
    affects_position,
    affects_cost_basis,
    affects_realized_pnl,
    affects_cash,
    realized_pnl,
    realized_pnl_cny,
    cashflow_id,
    source_table,
    source_id,
    source_key,
    source_hash,
    raw_json,
    review_status
)
VALUES
(
    DATE '2025-12-12',
    DATE '2025-12-12',
    'ccb_yhs300_fund',
    '建设银行',
    'ASSET_JYLC_WXGSJX',
    'JY581122514900000',
    '交银理财稳享固收精选日开47号（90天持有）',
    '理财',
    'CNY',
    'buy',
    'buy',
    50000.0000000000,
    1.0000000000,
    50000.0000000000,
    0,
    0,
    -50000.0000000000,
    1,
    -50000.0000000000,
    TRUE,
    TRUE,
    FALSE,
    TRUE,
    NULL,
    NULL,
    19,
    'pams_cashflow_fact',
    '19',
    'ccb_yhs300_fund:JYLC_WXGSJX:subscription:2025-12-12',
    md5('migration_061:ccb_yhs300_fund:JYLC_WXGSJX:subscription:2025-12-12:19'),
    jsonb_build_object(
        'migration', '061_ccb_wealth_fixed_income_nav_backfill',
        'cashflow_id', 19,
        'source_note', 'CCB fixed-income wealth subscription cashflow'
    ),
    'reviewed'
),
(
    DATE '2026-03-24',
    DATE '2026-03-24',
    'ccb_yhs300_fund',
    '建设银行',
    'ASSET_JYLC_WXGSJX',
    'JY581122514900000',
    '交银理财稳享固收精选日开47号（90天持有）',
    '理财',
    'CNY',
    'sell',
    'sell',
    50000.0000000000,
    1.0064478000,
    50322.3900000000,
    0,
    0,
    50322.3900000000,
    1,
    50322.3900000000,
    TRUE,
    TRUE,
    TRUE,
    TRUE,
    322.3900000000,
    322.3900000000,
    593,
    'pams_cashflow_fact',
    '593',
    'ccb_yhs300_fund:JYLC_WXGSJX:redemption:2026-03-24',
    md5('migration_061:ccb_yhs300_fund:JYLC_WXGSJX:redemption:2026-03-24:593'),
    jsonb_build_object(
        'migration', '061_ccb_wealth_fixed_income_nav_backfill',
        'cashflow_id', 593,
        'source_note', 'CCB fixed-income wealth redemption cashflow'
    ),
    'reviewed'
)
ON CONFLICT (source_table, source_id) DO UPDATE
SET trade_date = EXCLUDED.trade_date,
    settle_date = EXCLUDED.settle_date,
    account_code = EXCLUDED.account_code,
    broker = EXCLUDED.broker,
    asset_id = EXCLUDED.asset_id,
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    market = EXCLUDED.market,
    currency = EXCLUDED.currency,
    trade_type = EXCLUDED.trade_type,
    side = EXCLUDED.side,
    quantity = EXCLUDED.quantity,
    price = EXCLUDED.price,
    gross_amount = EXCLUDED.gross_amount,
    fee_amount = EXCLUDED.fee_amount,
    tax_amount = EXCLUDED.tax_amount,
    net_cash = EXCLUDED.net_cash,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    net_cash_cny = EXCLUDED.net_cash_cny,
    affects_position = EXCLUDED.affects_position,
    affects_cost_basis = EXCLUDED.affects_cost_basis,
    affects_realized_pnl = EXCLUDED.affects_realized_pnl,
    affects_cash = EXCLUDED.affects_cash,
    realized_pnl = EXCLUDED.realized_pnl,
    realized_pnl_cny = EXCLUDED.realized_pnl_cny,
    cashflow_id = EXCLUDED.cashflow_id,
    source_key = EXCLUDED.source_key,
    source_hash = EXCLUDED.source_hash,
    raw_json = pams_investment_trade_fact.raw_json || EXCLUDED.raw_json,
    review_status = EXCLUDED.review_status,
    updated_at = NOW();

COMMIT;
