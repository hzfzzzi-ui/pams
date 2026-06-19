-- Add the SGE Au99.99 spot gold benchmark used to value local gold accumulation.
-- This is a comparison/valuation instrument only; it must not appear as a holding.

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
VALUES (
    'BENCH_GOLD_SPOT_AU9999',
    '上海黄金Au99.99',
    '黄金现货对照(Au99.99)',
    'Au99.99.SGE',
    'SGE',
    'benchmark',
    'gold_spot',
    'SGE',
    '中国大陆',
    '黄金 / 商品',
    'CNY',
    'spot_close',
    'benchmark_only',
    'benchmark_only',
    'active',
    'migration_034_gold_spot_benchmark',
    'high',
    '{"role":"benchmark","is_holding":false,"proxy_for":["CCB_GOLD_ACCUM","ASSET_CCB_GOLD_ACCUM"],"price_source":"akshare_sge_daily:Au99.99"}'::jsonb,
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
    ('BENCH_GOLD_SPOT_AU9999', 'symbol', 'Au99.99.SGE', 'migration_034_gold_spot_benchmark', 5),
    ('BENCH_GOLD_SPOT_AU9999', 'symbol', 'Au99.99', 'migration_034_gold_spot_benchmark', 5),
    ('BENCH_GOLD_SPOT_AU9999', 'name', '上海黄金Au99.99', 'migration_034_gold_spot_benchmark', 10),
    ('BENCH_GOLD_SPOT_AU9999', 'name', '黄金现货', 'migration_034_gold_spot_benchmark', 20),
    ('BENCH_GOLD_SPOT_AU9999', 'role', 'gold_price_benchmark', 'migration_034_gold_spot_benchmark', 30)
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);
