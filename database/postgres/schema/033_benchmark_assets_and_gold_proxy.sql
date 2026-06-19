-- Seed benchmark-only assets and normalize the local gold proxy data.
-- Gold ETF 518880 and SSE Composite are comparison instruments, not holdings.

INSERT INTO pams_asset_master (
    asset_id,
    canonical_name,
    display_name,
    primary_symbol,
    exchange,
    asset_type,
    asset_subtype,
    market,
    listing_region,
    economic_region,
    currency,
    valuation_method,
    liquidity_profile,
    exposure_policy,
    status,
    source,
    confidence,
    raw_json,
    last_verified_at
)
VALUES
    (
        'BENCH_GOLD_ETF_518880',
        '华安黄金ETF',
        '黄金价格对照(518880)',
        '518880.SH',
        'SSE',
        'benchmark',
        'gold_etf_proxy',
        'CN_A',
        '中国大陆',
        '黄金 / 商品',
        'CNY',
        'close_price',
        'benchmark_only',
        'benchmark_only',
        'active',
        'migration_033_benchmark_assets',
        'high',
        '{"role":"benchmark","is_holding":false,"proxy_for":["CCB_GOLD_ACCUM","ASSET_CCB_GOLD_ACCUM"],"price_source":"pams_asset_market_daily:518880.SH"}'::jsonb,
        NOW()
    ),
    (
        'BENCH_SSE_COMPOSITE',
        '上证指数',
        '上证指数对照',
        '000001.SH',
        'SSE',
        'benchmark',
        'equity_index',
        'CN_INDEX',
        '中国大陆',
        '中国大陆',
        'CNY',
        'index_close',
        'benchmark_only',
        'benchmark_only',
        'active',
        'migration_033_benchmark_assets',
        'high',
        '{"role":"benchmark","is_holding":false,"price_source":"pit_index_daily:000001.SH"}'::jsonb,
        NOW()
    )
ON CONFLICT (asset_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    display_name = EXCLUDED.display_name,
    primary_symbol = EXCLUDED.primary_symbol,
    exchange = EXCLUDED.exchange,
    asset_type = EXCLUDED.asset_type,
    asset_subtype = EXCLUDED.asset_subtype,
    market = EXCLUDED.market,
    listing_region = EXCLUDED.listing_region,
    economic_region = EXCLUDED.economic_region,
    currency = EXCLUDED.currency,
    valuation_method = EXCLUDED.valuation_method,
    liquidity_profile = EXCLUDED.liquidity_profile,
    exposure_policy = EXCLUDED.exposure_policy,
    status = EXCLUDED.status,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    raw_json = pams_asset_master.raw_json || EXCLUDED.raw_json,
    last_verified_at = NOW(),
    updated_at = NOW();

INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority)
VALUES
    ('BENCH_GOLD_ETF_518880', 'symbol', '518880.SH', 'migration_033_benchmark_assets', 5),
    ('BENCH_GOLD_ETF_518880', 'symbol', 'sh518880', 'migration_033_benchmark_assets', 5),
    ('BENCH_GOLD_ETF_518880', 'name', '华安黄金ETF', 'migration_033_benchmark_assets', 10),
    ('BENCH_GOLD_ETF_518880', 'name', '黄金价格', 'migration_033_benchmark_assets', 20),
    ('BENCH_GOLD_ETF_518880', 'role', 'gold_price_benchmark', 'migration_033_benchmark_assets', 30),
    ('BENCH_SSE_COMPOSITE', 'symbol', '000001.SH', 'migration_033_benchmark_assets', 5),
    ('BENCH_SSE_COMPOSITE', 'symbol', 'sh000001', 'migration_033_benchmark_assets', 5),
    ('BENCH_SSE_COMPOSITE', 'name', '上证指数', 'migration_033_benchmark_assets', 10),
    ('BENCH_SSE_COMPOSITE', 'name', 'SSE Composite', 'migration_033_benchmark_assets', 20)
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);

UPDATE pams_asset_market_daily
SET asset_id = 'BENCH_GOLD_ETF_518880',
    symbol = '518880.SH',
    name = '华安黄金ETF',
    market = 'CN_A',
    currency = 'CNY',
    confidence = CASE WHEN confidence IN ('high', 'medium') THEN confidence ELSE 'medium' END,
    raw_json = raw_json || '{"role":"benchmark","is_holding":false,"proxy_for":"local_gold_holdings"}'::jsonb,
    updated_at = NOW()
WHERE symbol IN ('518880.SH', 'sh518880')
   OR source_key ILIKE '%518880%';

INSERT INTO pams_asset_market_daily (
    as_of,
    source,
    source_key,
    asset_id,
    symbol,
    name,
    market,
    currency,
    open_price,
    high_price,
    low_price,
    close_price,
    volume,
    turnover,
    confidence,
    raw_json
)
SELECT
    trade_date,
    'pit_index_daily',
    'pit_index_daily:000001.SH:' || trade_date::text,
    'BENCH_SSE_COMPOSITE',
    '000001.SH',
    '上证指数',
    'CN_INDEX',
    'CNY',
    open,
    high,
    low,
    close,
    volume,
    turnover,
    'high',
    '{"role":"benchmark","is_holding":false}'::jsonb
FROM pit_index_daily
WHERE index_code = '000001.SH'
  AND close IS NOT NULL
ON CONFLICT (as_of, source, source_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    market = EXCLUDED.market,
    currency = EXCLUDED.currency,
    open_price = EXCLUDED.open_price,
    high_price = EXCLUDED.high_price,
    low_price = EXCLUDED.low_price,
    close_price = EXCLUDED.close_price,
    volume = EXCLUDED.volume,
    turnover = EXCLUDED.turnover,
    confidence = EXCLUDED.confidence,
    raw_json = pams_asset_market_daily.raw_json || EXCLUDED.raw_json,
    updated_at = NOW();

INSERT INTO pams_asset_market_daily (
    as_of,
    source,
    source_key,
    asset_id,
    symbol,
    name,
    market,
    currency,
    open_price,
    high_price,
    low_price,
    close_price,
    volume,
    turnover,
    confidence,
    raw_json
)
SELECT
    trade_date,
    'pams_ohlcv_daily',
    'pams_ohlcv_daily:CN_INDEX:sh000001:' || trade_date::text,
    'BENCH_SSE_COMPOSITE',
    '000001.SH',
    '上证指数',
    'CN_INDEX',
    'CNY',
    open,
    high,
    low,
    close,
    volume,
    turnover,
    'high',
    '{"role":"benchmark","is_holding":false,"fallback_source":"pams_ohlcv_daily"}'::jsonb
FROM pams_ohlcv_daily o
WHERE market = 'CN_INDEX'
  AND symbol = 'sh000001'
  AND close IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM pams_asset_market_daily existing
      WHERE existing.asset_id = 'BENCH_SSE_COMPOSITE'
        AND existing.as_of = o.trade_date
  )
ON CONFLICT (as_of, source, source_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    market = EXCLUDED.market,
    currency = EXCLUDED.currency,
    open_price = EXCLUDED.open_price,
    high_price = EXCLUDED.high_price,
    low_price = EXCLUDED.low_price,
    close_price = EXCLUDED.close_price,
    volume = EXCLUDED.volume,
    turnover = EXCLUDED.turnover,
    confidence = EXCLUDED.confidence,
    raw_json = pams_asset_market_daily.raw_json || EXCLUDED.raw_json,
    updated_at = NOW();

WITH latest_holding AS (
    SELECT quantity, cost, buy_price, MIN(as_of) OVER () AS first_snapshot_date
    FROM pams_holdings_snapshot
    WHERE account_code = 'ccb_gold_accumulation'
      AND symbol = 'CCB_GOLD_ACCUM'
      AND quantity IS NOT NULL
      AND quantity <> 0
    ORDER BY as_of DESC
    LIMIT 1
), first_trade AS (
    SELECT MIN(trade_date) AS first_trade_date
    FROM pams_investment_trade_fact
    WHERE account_code = 'ccb_gold_accumulation'
      AND LOWER(COALESCE(NULLIF(side, ''), trade_type)) IN ('buy', 'subscribe', 'purchase')
), days AS (
    SELECT generate_series(
        COALESCE((SELECT first_trade_date FROM first_trade), (SELECT first_snapshot_date FROM latest_holding)),
        CURRENT_DATE,
        INTERVAL '1 day'
    )::date AS as_of
), priced AS (
    SELECT
        d.as_of,
        lh.quantity,
        lh.cost,
        lh.buy_price,
        px.close_price
    FROM days d
    CROSS JOIN latest_holding lh
    JOIN LATERAL (
        SELECT close_price
        FROM pams_asset_market_daily
        WHERE asset_id = 'BENCH_GOLD_ETF_518880'
          AND as_of <= d.as_of
          AND close_price IS NOT NULL
        ORDER BY as_of DESC
        LIMIT 1
    ) px ON TRUE
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
    'CCB_GOLD_ACCUM',
    '建行黄金积存金',
    '资产',
    'CNY',
    'ccb_gold_accumulation',
    '贵金属',
    '黄金',
    ROUND((quantity * close_price)::numeric, 4),
    cost,
    as_of,
    quantity,
    buy_price,
    close_price,
    'ASSET_CCB_GOLD_ACCUM'
FROM priced
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
    updated_at = NOW();
