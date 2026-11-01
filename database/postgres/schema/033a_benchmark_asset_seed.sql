-- Define benchmark-only assets; no holding or price-history writes.
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
        '{"role":"benchmark","is_holding":false,"proxy_for":["CCB_GOLD_ACCUM","ASSET_CCB_GOLD_ACCUM"],"price_source":"market_observations:518880.SH"}'::jsonb,
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
