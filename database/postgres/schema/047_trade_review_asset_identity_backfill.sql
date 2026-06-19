-- Backfill asset identity for trade-review attribution gaps.
-- This is intentionally idempotent: it can be re-run after fresh imports.

DELETE FROM pams_asset_alias
WHERE asset_id = 'ETF_QQQ'
  AND alias_value IN (
    '广发纳斯达克100ETF联接人民币(QDII)A',
    '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I',
    '建信纳斯达克100指数(QDII)A人民币',
    '建信纳斯达克100指数(QDII)C人民币'
  );

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
    status,
    source,
    confidence,
    raw_json,
    last_verified_at,
    updated_at
)
VALUES
    (
        'ASSET_270042',
        '广发纳斯达克100ETF联接人民币(QDII)A',
        '广发纳斯达克100ETF联接人民币(QDII)A',
        '270042',
        'ETF / 基金',
        'QDII / 海外股票',
        '基金',
        '中国大陆',
        '美国',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"270042","source_note":"Guangfa Nasdaq 100 ETF feeder QDII RMB A"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_022664',
        '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I',
        '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I',
        '022664',
        'ETF / 基金',
        'QDII / 海外股票',
        '基金',
        '中国大陆',
        '美国',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"022664","source_note":"Huatai-PineBridge Nasdaq 100 ETF feeder QDII I"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_539001',
        '建信纳斯达克100指数(QDII)A人民币',
        '建信纳斯达克100指数(QDII)A人民币',
        '539001',
        'ETF / 基金',
        'QDII / 海外股票',
        '基金',
        '中国大陆',
        '美国',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"539001","source_note":"CCB Principal Nasdaq 100 QDII A RMB"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_012752',
        '建信纳斯达克100指数(QDII)C人民币',
        '建信纳斯达克100指数(QDII)C人民币',
        '012752',
        'ETF / 基金',
        'QDII / 海外股票',
        '基金',
        '中国大陆',
        '美国',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"012752","source_note":"CCB Principal Nasdaq 100 QDII C RMB"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_161226',
        '国投瑞银白银期货(LOF)A',
        '国投瑞银白银期货(LOF)A',
        '161226',
        '黄金 / 商品',
        '白银基金',
        '基金',
        '中国大陆',
        '中国大陆',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"161226","source_note":"UBS SDIC Silver Futures LOF A"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_161015',
        '富国天盈债券C',
        '富国天盈债券C',
        '161015',
        '债券 / 货币基金',
        '债券基金',
        '货币基金 / 债券',
        '中国大陆',
        '中国大陆',
        'CNY',
        'nav_or_market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"fund_code":"161015","source_note":"Fullgoal Tianying Bond C"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_USO',
        'USO - UNITED STATES OIL FUND LP',
        'USO - UNITED STATES OIL FUND LP',
        'USO',
        'ETF / 基金',
        '商品ETF',
        '美股',
        '美国',
        '美国',
        'USD',
        'market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"ticker":"USO","source_note":"Yahoo Finance listed commodity ETF"}'::jsonb,
        now(),
        now()
    ),
    (
        'ASSET_000660_KS',
        '000660.KS - SK HYNIX INC',
        '000660.KS - SK HYNIX INC',
        '000660.KS',
        '海外个股',
        '韩国股票',
        '韩股',
        '韩国',
        '韩国',
        'KRW',
        'market_price',
        'active',
        'manual_trade_review_backfill_20260614',
        'high',
        '{"ticker":"000660.KS","source_note":"Yahoo Finance listed Korean equity"}'::jsonb,
        now(),
        now()
    )
ON CONFLICT (asset_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    display_name = EXCLUDED.display_name,
    primary_symbol = EXCLUDED.primary_symbol,
    asset_type = EXCLUDED.asset_type,
    asset_subtype = EXCLUDED.asset_subtype,
    market = EXCLUDED.market,
    listing_region = EXCLUDED.listing_region,
    economic_region = EXCLUDED.economic_region,
    currency = EXCLUDED.currency,
    valuation_method = EXCLUDED.valuation_method,
    status = EXCLUDED.status,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    raw_json = EXCLUDED.raw_json,
    last_verified_at = EXCLUDED.last_verified_at,
    updated_at = now();

UPDATE pams_asset_master
SET primary_symbol = '000216',
    asset_type = '黄金 / 商品',
    asset_subtype = '黄金基金',
    market = '基金',
    listing_region = '中国大陆',
    economic_region = '中国大陆',
    currency = 'CNY',
    valuation_method = 'nav_or_market_price',
    status = 'active',
    source = 'manual_trade_review_backfill_20260614',
    confidence = 'high',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || '{"fund_code":"000216","source_note":"Huaan gold ETF feeder A"}'::jsonb,
    last_verified_at = now(),
    updated_at = now()
WHERE asset_id = 'ASSET_GOLD_ETF_LINK';

WITH aliases(asset_id, alias_type, alias_value, source, priority) AS (
    VALUES
        ('ASSET_270042', 'symbol', '270042', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_270042', 'canonical_symbol', '270042', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_270042', 'name', '广发纳斯达克100ETF联接人民币(QDII)A', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_270042', 'display_name', '广发纳斯达克100ETF联接人民币(QDII)A', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_022664', 'symbol', '022664', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_022664', 'canonical_symbol', '022664', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_022664', 'name', '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_022664', 'display_name', '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_539001', 'symbol', '539001', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_539001', 'canonical_symbol', '539001', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_539001', 'name', '建信纳斯达克100指数(QDII)A人民币', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_539001', 'display_name', '建信纳斯达克100指数(QDII)A人民币', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_012752', 'symbol', '012752', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_012752', 'canonical_symbol', '012752', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_012752', 'name', '建信纳斯达克100指数(QDII)C人民币', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_012752', 'display_name', '建信纳斯达克100指数(QDII)C人民币', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_GOLD_ETF_LINK', 'symbol', '000216', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_GOLD_ETF_LINK', 'canonical_symbol', '000216', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_GOLD_ETF_LINK', 'name', '华安黄金ETF联接A', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_GOLD_ETF_LINK', 'display_name', '华安黄金ETF联接A', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_161226', 'symbol', '161226', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_161226', 'canonical_symbol', '161226', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_161226', 'name', '国投瑞银白银期货(LOF)A', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_161226', 'display_name', '国投瑞银白银期货(LOF)A', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_161015', 'symbol', '161015', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_161015', 'canonical_symbol', '161015', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_161015', 'name', '富国天盈债券C', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_161015', 'display_name', '富国天盈债券C', 'manual_trade_review_backfill_20260614', 25),
        ('ASSET_USO', 'symbol', 'USO', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_USO', 'canonical_symbol', 'USO', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_USO', 'name', 'USO - UNITED STATES OIL FUND LP', 'manual_trade_review_backfill_20260614', 20),
        ('ASSET_000660_KS', 'symbol', '000660.KS', 'manual_trade_review_backfill_20260614', 10),
        ('ASSET_000660_KS', 'canonical_symbol', '000660.KS', 'manual_trade_review_backfill_20260614', 15),
        ('ASSET_000660_KS', 'name', '000660.KS - SK HYNIX INC', 'manual_trade_review_backfill_20260614', 20)
)
INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority)
SELECT asset_id, alias_type, alias_value, source, priority
FROM aliases
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = EXCLUDED.priority;

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_270042', symbol = '270042'
WHERE name = '广发纳斯达克100ETF联接人民币(QDII)A'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_022664', symbol = '022664'
WHERE name = '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_539001', symbol = '539001'
WHERE name = '建信纳斯达克100指数(QDII)A人民币'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_012752', symbol = '012752'
WHERE name = '建信纳斯达克100指数(QDII)C人民币'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_GOLD_ETF_LINK', symbol = '000216'
WHERE name = '华安黄金ETF联接A'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_161226', symbol = '161226'
WHERE name = '国投瑞银白银期货(LOF)A'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_161015', symbol = '161015'
WHERE name = '富国天盈债券C'
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_USO', symbol = 'USO'
WHERE (symbol = 'USO' OR name = 'USO - UNITED STATES OIL FUND LP')
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_SGOV', symbol = 'SGOV'
WHERE (symbol = 'SGOV' OR name = 'SGOV - ISHARES 0-3 MONTH TREASURY B')
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_3690', symbol = '3690'
WHERE (symbol = '3690' OR symbol = '03690.HK' OR name = '3690 - MEITUAN-CLASS B')
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_000660_KS', symbol = '000660.KS'
WHERE (symbol = '000660.KS' OR name = '000660.KS - SK HYNIX INC')
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');

UPDATE pams_investment_trade_fact
SET asset_id = 'ASSET_CCB_GOLD_ACCUM', symbol = 'CCB_GOLD_ACCUM'
WHERE (symbol = 'CCB_GOLD_ACCUM' OR name = '建行黄金积存金')
  AND trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption');
