-- Tencent Licaitong correction confirmed by user screenshot on 2026-06-22.
-- 富国天盈债券C was bought in 2025 and redeemed in 2026, so it must be
-- treated as an opening holding for 2026 NAV instead of an unexplained gain.

BEGIN;

INSERT INTO pams_investment_trade_fact (
    trade_date,
    trade_time,
    settle_date,
    account_code,
    broker,
    portfolio_code,
    asset_id,
    symbol,
    name,
    market,
    currency,
    trade_type,
    side,
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
    source_table,
    source_id,
    source_key,
    source_hash,
    raw_json,
    review_status,
    asset_major_category,
    asset_minor_category,
    execution_status,
    notes,
    created_by
)
VALUES (
    DATE '2025-12-15',
    TIMESTAMP '2025-12-15 16:19:07',
    DATE '2025-12-15',
    'tencent_licaitong',
    '腾讯理财通',
    '活期+',
    'ASSET_161015',
    '161015',
    '富国天盈债券C',
    '债券基金',
    'CNY',
    'buy',
    'buy',
    30000.00,
    0,
    0,
    -30000.00,
    1,
    -30000.00,
    true,
    true,
    false,
    true,
    NULL,
    NULL,
    'manual_user_confirmed_tencent_licaitong',
    '20251215_161015_buy_30000',
    'tencent_licaitong:20251215:161015:buy:30000',
    '27bc8ce1670de7c1efb0d2bfdc2eab4e91a6a48c12b1f6b6644c33ad85768aac',
    jsonb_build_object(
        'source', 'user_screenshot',
        'confirmed_at', '2026-06-22',
        'screenshot_path', 'C:/Users/hzfzzzi/Documents/xwechat_files/huangzhanfeng002_ecbf/temp/RWTemp/2026-06/9e20f478899dc29eb19741386f9343c8/d2135597bd761ddd18e78810e7e37f64.jpg',
        'product_name', '富国天盈债券C',
        'platform', '腾讯腾安 / 腾讯理财通',
        'operation', '买入',
        'cash_source', '活期+',
        'amount_cny', 30000.00,
        'execution_status', '买入成功'
    ),
    'reviewed',
    '理财',
    '债券基金',
    '买入成功',
    '用户截图补充：2025-12-15 16:19:07 活期+买入富国天盈债券C 30,000.00 元。',
    'codex'
)
ON CONFLICT (source_table, source_id) DO UPDATE SET
    trade_date = EXCLUDED.trade_date,
    trade_time = EXCLUDED.trade_time,
    settle_date = EXCLUDED.settle_date,
    account_code = EXCLUDED.account_code,
    broker = EXCLUDED.broker,
    portfolio_code = EXCLUDED.portfolio_code,
    asset_id = EXCLUDED.asset_id,
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    market = EXCLUDED.market,
    currency = EXCLUDED.currency,
    trade_type = EXCLUDED.trade_type,
    side = EXCLUDED.side,
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
    raw_json = EXCLUDED.raw_json,
    review_status = EXCLUDED.review_status,
    asset_major_category = EXCLUDED.asset_major_category,
    asset_minor_category = EXCLUDED.asset_minor_category,
    execution_status = EXCLUDED.execution_status,
    notes = EXCLUDED.notes,
    updated_at = now();

UPDATE pams_investment_trade_fact
SET
    realized_pnl = 87.93,
    realized_pnl_cny = 87.93,
    notes = CASE
        WHEN notes ILIKE '%2025-12-15%30,000%' THEN notes
        ELSE concat_ws(E'\n', notes, '用户截图补充：对应 2025-12-15 买入 30,000.00 元；2026-01-21 取出 30,087.93 元，已实现收益 87.93 元。')
    END,
    raw_json = raw_json || jsonb_build_object(
        'matched_opening_buy_source_table', 'manual_user_confirmed_tencent_licaitong',
        'matched_opening_buy_source_id', '20251215_161015_buy_30000',
        'matched_opening_buy_amount_cny', 30000.00,
        'manual_realized_pnl_cny', 87.93
    ),
    updated_at = now()
WHERE account_code = 'tencent_licaitong'
  AND symbol = '161015'
  AND trade_date = DATE '2026-01-21'
  AND trade_type = 'sell';

WITH sell_nav AS (
    SELECT COALESCE(unit_nav, close_price) AS nav
    FROM pams_asset_market_daily
    WHERE symbol = '161015'
      AND as_of = DATE '2026-01-21'
    ORDER BY as_of DESC
    LIMIT 1
),
opening_position AS (
    SELECT
        (30087.93::numeric / nav) AS quantity,
        (30000.00::numeric / (30087.93::numeric / nav)) AS buy_price
    FROM sell_nav
),
snapshot_days AS (
    SELECT generate_series(DATE '2026-01-01', DATE '2026-01-20', INTERVAL '1 day')::date AS as_of
),
daily_nav AS (
    SELECT
        d.as_of,
        COALESCE(
            (
                SELECT COALESCE(m.unit_nav, m.close_price)
                FROM pams_asset_market_daily m
                WHERE m.symbol = '161015'
                  AND m.as_of <= d.as_of
                  AND COALESCE(m.unit_nav, m.close_price) IS NOT NULL
                ORDER BY m.as_of DESC
                LIMIT 1
            ),
            (
                SELECT COALESCE(m.unit_nav, m.close_price)
                FROM pams_asset_market_daily m
                WHERE m.symbol = '161015'
                  AND m.as_of >= d.as_of
                  AND COALESCE(m.unit_nav, m.close_price) IS NOT NULL
                ORDER BY m.as_of ASC
                LIMIT 1
            )
        ) AS nav
    FROM snapshot_days d
),
snapshot_rows AS (
    SELECT
        '161015'::varchar(100) AS symbol,
        '富国天盈债券C'::varchar(200) AS name,
        '资产'::varchar(10) AS asset_liability,
        'CNY'::varchar(10) AS currency,
        'tencent_licaitong'::varchar(50) AS account_code,
        '理财'::varchar(50) AS major_category,
        '债券基金'::varchar(50) AS minor_category,
        ROUND(op.quantity * dn.nav, 4) AS market_value,
        30000.00::numeric(20,4) AS cost,
        dn.as_of,
        ROUND(op.quantity, 6) AS quantity,
        ROUND(op.buy_price, 6) AS buy_price,
        ROUND(dn.nav, 6) AS current_price,
        'ASSET_161015'::varchar AS asset_id
    FROM daily_nav dn
    CROSS JOIN opening_position op
    WHERE dn.nav IS NOT NULL
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
    asset_id
)
SELECT
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
    asset_id
FROM snapshot_rows
ON CONFLICT (account_code, name, as_of) DO UPDATE SET
    symbol = EXCLUDED.symbol,
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
    updated_at = now();

COMMIT;
