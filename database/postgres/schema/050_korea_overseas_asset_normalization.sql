-- Normalize Korean listed equities so asset type, market, region, and currency
-- stay orthogonal. Older migrations used generic overseas / US buckets.

INSERT INTO pams_asset_master (
    asset_id,
    canonical_name,
    display_name,
    primary_symbol,
    exchange,
    asset_type,
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
) VALUES
(
    'ASSET_005930_KS',
    '005930.KS - SAMSUNG ELECTRONICS CO LTD',
    'Samsung Electronics',
    '005930.KS',
    'KRX',
    '韩股个股',
    '韩股',
    '韩国',
    '韩国',
    'KRW',
    'market_price',
    'listed_equity',
    'direct',
    'active',
    'manual',
    'high',
    '{"reason": "Korean equity should not be attributed to US stock asset type"}'::jsonb,
    NOW()
),
(
    'ASSET_000660_KS',
    '000660.KS - SK HYNIX INC',
    'SK Hynix',
    '000660.KS',
    'KRX',
    '韩股个股',
    '韩股',
    '韩国',
    '韩国',
    'KRW',
    'market_price',
    'listed_equity',
    'direct',
    'active',
    'manual',
    'high',
    '{"reason": "Korean equity should not use generic overseas asset type"}'::jsonb,
    NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    display_name = EXCLUDED.display_name,
    primary_symbol = EXCLUDED.primary_symbol,
    exchange = EXCLUDED.exchange,
    asset_type = EXCLUDED.asset_type,
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
    last_verified_at = EXCLUDED.last_verified_at,
    updated_at = NOW();

INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority)
VALUES
    ('ASSET_005930_KS', 'symbol', '005930.KS', 'manual_korea_normalization_20260619', 1),
    ('ASSET_005930_KS', 'symbol', '005930', 'manual_korea_normalization_20260619', 5),
    ('ASSET_005930_KS', 'ibkr_symbol', '005930.KS', 'manual_korea_normalization_20260619', 1),
    ('ASSET_000660_KS', 'symbol', '000660.KS', 'manual_korea_normalization_20260619', 1),
    ('ASSET_000660_KS', 'symbol', '000660', 'manual_korea_normalization_20260619', 5),
    ('ASSET_000660_KS', 'ibkr_symbol', '000660.KS', 'manual_korea_normalization_20260619', 1)
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);

UPDATE pams_holdings_snapshot
SET asset_id = CASE
        WHEN UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930') THEN 'ASSET_005930_KS'
        WHEN UPPER(COALESCE(symbol, '')) IN ('000660.KS', '000660') THEN 'ASSET_000660_KS'
        ELSE asset_id
    END,
    minor_category = '韩股个股',
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930', '000660.KS', '000660');

UPDATE pams_investment_trade_fact
SET asset_id = CASE
        WHEN UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930') THEN 'ASSET_005930_KS'
        WHEN UPPER(COALESCE(symbol, '')) IN ('000660.KS', '000660') THEN 'ASSET_000660_KS'
        ELSE asset_id
    END,
    market = '韩股',
    currency = 'KRW',
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930', '000660.KS', '000660');

UPDATE pams_asset_market_daily
SET asset_id = CASE
        WHEN UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930') THEN 'ASSET_005930_KS'
        WHEN UPPER(COALESCE(symbol, '')) IN ('000660.KS', '000660') THEN 'ASSET_000660_KS'
        ELSE asset_id
    END,
    market = '韩股',
    currency = 'KRW'
WHERE UPPER(COALESCE(symbol, '')) IN ('005930.KS', '005930', '000660.KS', '000660');
