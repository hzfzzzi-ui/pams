-- Expand region/currency mapping for Swedish holdings imported from IBKR.
-- SIVE is traded in SEK and should not be attributed to US equity exposure.

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
) VALUES (
    'ASSET_SIVE',
    'SIVE - SIVERS SEMICONDUCTORS AB',
    'SIVE - SIVERS SEMICONDUCTORS AB',
    'SIVE',
    'Nasdaq Stockholm',
    '欧股个股',
    '欧股',
    '瑞典',
    '瑞典',
    'SEK',
    'market_price',
    'listed_equity',
    'direct',
    'active',
    'manual',
    'high',
    '{"reason": "IBKR Swedish holding; prevent US attribution fallback"}'::jsonb,
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
    ('ASSET_SIVE', 'symbol', 'SIVE', 'manual', 1),
    ('ASSET_SIVE', 'ibkr_symbol', 'SIVE', 'manual', 1)
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);

INSERT INTO pams_asset_master (
    asset_id,
    canonical_name,
    display_name,
    primary_symbol,
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
) VALUES (
    'ASSET_CASH_SEK',
    'Cash SEK',
    'Cash SEK',
    'CASH_SEK',
    '现金',
    '现金',
    '瑞典',
    '瑞典',
    'SEK',
    'cash_balance',
    'cash',
    'direct',
    'active',
    'manual',
    'high',
    '{"reason": "IBKR SEK cash balance"}'::jsonb,
    NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    display_name = EXCLUDED.display_name,
    primary_symbol = EXCLUDED.primary_symbol,
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
    ('ASSET_CASH_SEK', 'symbol', 'CASH_SEK', 'manual', 1),
    ('ASSET_CASH_SEK', 'ibkr_cash_symbol', 'CASH_SEK', 'manual', 1)
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);

UPDATE pams_holdings_snapshot
SET asset_id = 'ASSET_SIVE',
    minor_category = COALESCE(NULLIF(minor_category, ''), '欧股个股'),
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) = 'SIVE';

UPDATE pams_holdings_snapshot
SET asset_id = 'ASSET_CASH_SEK',
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) = 'CASH_SEK';

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_SIVE',
    market = '欧股',
    currency = 'SEK',
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) = 'SIVE';

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_CASH_SEK',
    market = '现金',
    currency = 'SEK',
    updated_at = NOW()
WHERE UPPER(COALESCE(symbol, '')) = 'CASH_SEK';

UPDATE pams_asset_market_daily
SET asset_id = 'ASSET_SIVE',
    market = '欧股',
    currency = 'SEK'
WHERE UPPER(COALESCE(symbol, '')) = 'SIVE';
