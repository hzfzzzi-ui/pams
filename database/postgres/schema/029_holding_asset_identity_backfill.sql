-- 029_holding_asset_identity_backfill.sql
--
-- Complete asset identity coverage for holdings snapshots:
-- - seed missing assets/aliases used by current holdings
-- - retire a misclassified duplicate for 110020 so the fund asset is preferred
-- - add and backfill pams_holdings_snapshot.asset_id
-- - harden the asset reference with a validated foreign key

BEGIN;

ALTER TABLE pams_holdings_snapshot
    ADD COLUMN IF NOT EXISTS asset_id VARCHAR;

-- 110020 is a fund holding. A_110020 was generated as an A-share fallback and
-- must not participate in symbol/alias resolution.
UPDATE pams_asset_master
SET primary_symbol = NULL,
    status = 'inactive',
    source = 'migration_029_holding_asset_identity',
    raw_json = raw_json || jsonb_build_object(
        'deprecated_by_migration', '029_holding_asset_identity_backfill',
        'replacement_asset_id', 'ETF_110020',
        'deprecated_reason', 'misclassified fund code as A-share stock'
    ),
    updated_at = now()
WHERE asset_id = 'A_110020'
  AND asset_type = 'A股个股';

UPDATE pams_asset_alias
SET alias_value = concat('__deprecated_A_110020__', alias_type, '__', alias_value),
    source = 'migration_029_holding_asset_identity'
WHERE asset_id = 'A_110020'
  AND alias_value IN ('110020', '易沪深300');

-- NFLX is a listed equity. ETF_NFLX is a duplicate fallback row generated from
-- lookthrough input and must not participate in active symbol/alias resolution.
UPDATE pams_asset_master
SET primary_symbol = NULL,
    status = 'inactive',
    source = 'migration_029_holding_asset_identity',
    raw_json = raw_json || jsonb_build_object(
        'deprecated_by_migration', '029_holding_asset_identity_backfill',
        'replacement_asset_id', 'ASSET_NFLX',
        'deprecated_reason', 'misclassified listed equity as fund asset'
    ),
    updated_at = now()
WHERE asset_id = 'ETF_NFLX'
  AND asset_type = 'ETF / 基金';

UPDATE pams_asset_alias
SET alias_value = concat('__deprecated_ETF_NFLX__', alias_type, '__', alias_value),
    source = 'migration_029_holding_asset_identity'
WHERE asset_id = 'ETF_NFLX'
  AND alias_value IN ('NFLX', 'NETFLIX, INC.');

WITH asset_seed (
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
    raw_json
) AS (
    VALUES
        ('ASSET_020640', '广发中证半导体材料设备ETF发起式联接C', '广发中证半导体材料设备ETF发起式联接C', '020640', 'ETF / 基金', '股票ETF / 基金', '基金', '中国大陆', '中国大陆', 'CNY', 'nav_or_market_price', 'daily_liquid', 'accumulating', 'fund_lookthrough', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('ASSET_020839', '南方中证半导体产业指数发起A', '南方中证半导体产业指数发起A', '020839', 'ETF / 基金', '股票ETF / 基金', '基金', '中国大陆', '中国大陆', 'CNY', 'nav_or_market_price', 'daily_liquid', 'accumulating', 'fund_lookthrough', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('ASSET_009033', '建信上海金ETF联接A', '建信上海金ETF联接A', '009033', '黄金 / 商品', '黄金基金', '基金', '中国大陆', '中国大陆', 'CNY', 'nav_or_market_price', 'daily_liquid', 'accumulating', 'commodity', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('ASSET_004419', '汇添富美元债债券（QDII）人民币A', '汇添富美元债债券（QDII）人民币A', '004419', '债券 / 货币基金', '美元债 / QDII', '货币基金 / 债券', '中国大陆', '美国', 'CNY', 'nav_or_market_price', 'daily_liquid', 'income', 'fixed_income', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('ASSET_011035', '嘉实中证稀土ETF联接A', '嘉实中证稀土ETF联接A', '011035', 'ETF / 基金', '股票ETF / 基金', '基金', '中国大陆', '中国大陆', 'CNY', 'nav_or_market_price', 'daily_liquid', 'accumulating', 'fund_lookthrough', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('A_002185', '华天科技', '华天科技', '002185', 'A股个股', NULL, 'A股', '中国大陆', '中国大陆', 'CNY', 'market_price', 'daily_liquid', NULL, 'listed_equity', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"missing holding asset identity"}'::jsonb),
        ('ASSET_CASH_HKD', 'Cash HKD', 'Cash HKD', 'CASH_HKD', '现金', NULL, '现金', NULL, '香港', 'HKD', 'cash_balance', 'cash', NULL, 'cash', 'active', 'migration_029_holding_asset_identity', 'high', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"foreign currency cash balance"}'::jsonb),
        ('ASSET_CASH_KRW', 'Cash KRW', 'Cash KRW', 'CASH_KRW', '现金', NULL, '现金', NULL, '韩国', 'KRW', 'cash_balance', 'cash', NULL, 'cash', 'active', 'migration_029_holding_asset_identity', 'high', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"foreign currency cash balance"}'::jsonb),
        ('ASSET_LOAN_OUT_CCB_20260531', '他人外借待归还', '他人外借待归还', 'LOAN_OUT_CCB_20260531', '应收款', '他人借款', '应收款', '中国大陆', '中国大陆', 'CNY', 'manual', 'illiquid', NULL, 'receivable', 'active', 'migration_029_holding_asset_identity', 'high', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"dated receivable holding"}'::jsonb),
        ('ASSET_RECEIVABLE_LOAN_CNY', '他人借款', '他人借款', 'RECEIVABLE_LOAN_CNY', '应收款', '他人借款', '应收款', '中国大陆', '中国大陆', 'CNY', 'manual', 'illiquid', NULL, 'receivable', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"generic receivable holding without symbol"}'::jsonb),
        ('ASSET_WECHAT_BALANCE', '微信零钱', '微信零钱', 'WECHAT_BALANCE', '现金', NULL, '现金', '中国大陆', '中国大陆', 'CNY', 'cash_balance', 'cash', NULL, 'cash', 'active', 'migration_029_holding_asset_identity', 'high', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"wallet cash balance"}'::jsonb),
        ('ASSET_WECHAT_LQT', '微信零钱通', '微信零钱通', 'WECHAT_LQT', '债券 / 货币基金', '短久期 / 货币', '货币基金 / 债券', '中国大陆', '中国大陆', 'CNY', 'amortized_cost_or_nav', 'daily_liquid', 'income', 'fixed_income', 'active', 'migration_029_holding_asset_identity', 'high', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"wallet money-market balance"}'::jsonb),
        ('ASSET_TENCENT_HUOQI_PLUS', '腾讯理财通活期+', '活期+', 'TENCENT_HUOQI_PLUS', '债券 / 货币基金', '短久期 / 货币', '货币基金 / 债券', '中国大陆', '中国大陆', 'CNY', 'amortized_cost_or_nav', 'daily_liquid', 'income', 'fixed_income', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"cash-like money-market product without symbol"}'::jsonb),
        ('ASSET_CREDIT_CARD_CNY', '信用卡负债 CNY', '信用卡负债 CNY', 'CREDIT_CARD_CNY', '信用负债', '信用卡', '负债', '中国大陆', '中国大陆', 'CNY', 'liability_balance', 'current_liability', NULL, 'liability', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"credit card liability without symbol"}'::jsonb),
        ('ASSET_CREDIT_CARD_USD', '信用卡负债 USD', '信用卡负债 USD', 'CREDIT_CARD_USD', '信用负债', '信用卡', '负债', '中国大陆', '中国大陆', 'USD', 'liability_balance', 'current_liability', NULL, 'liability', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"credit card liability without symbol"}'::jsonb),
        ('ASSET_JYLC_WXGSJX', '交银理财稳享固收精选', '交银理财稳享固收精选', 'JYLC_WXGSJX', '债券 / 货币基金', '固收理财', '理财', '中国大陆', '中国大陆', 'CNY', 'amortized_cost_or_nav', 'limited_liquidity', 'income', 'fixed_income', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"wealth-management holding without symbol"}'::jsonb),
        ('ASSET_CCB_PHYSICAL_GOLD', '建行黄金实物', '建行黄金实物', 'CCB_PHYSICAL_GOLD', '黄金 / 商品', '黄金实物', '黄金 / 商品', '中国大陆', '中国大陆', 'CNY', 'manual', 'illiquid', NULL, 'commodity', 'active', 'migration_029_holding_asset_identity', 'medium', '{"migration":"029_holding_asset_identity_backfill","source":"pams_holdings_snapshot","reason":"physical gold holding without symbol"}'::jsonb)
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
    now(),
    now(),
    now()
FROM asset_seed
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
    liquidity_profile = EXCLUDED.liquidity_profile,
    income_type = EXCLUDED.income_type,
    exposure_policy = EXCLUDED.exposure_policy,
    status = EXCLUDED.status,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    raw_json = EXCLUDED.raw_json,
    last_verified_at = now(),
    updated_at = now()
WHERE pams_asset_master.source = 'migration_029_holding_asset_identity';

WITH alias_seed(asset_id, alias_type, alias_value, priority) AS (
    VALUES
        ('ASSET_020640', 'symbol', '020640', 10),
        ('ASSET_020640', 'canonical_symbol', '020640', 15),
        ('ASSET_020640', 'name', '广发中证半导体材料设备ETF发起式联接C', 20),
        ('ASSET_020640', 'display_name', '广发中证半导体材料设备ETF发起式联接C', 25),
        ('ASSET_020839', 'symbol', '020839', 10),
        ('ASSET_020839', 'canonical_symbol', '020839', 15),
        ('ASSET_020839', 'name', '南方中证半导体产业指数发起A', 20),
        ('ASSET_020839', 'display_name', '南方中证半导体产业指数发起A', 25),
        ('ASSET_009033', 'symbol', '009033', 10),
        ('ASSET_009033', 'canonical_symbol', '009033', 15),
        ('ASSET_009033', 'name', '建信上海金ETF联接A', 20),
        ('ASSET_009033', 'display_name', '建信上海金ETF联接A', 25),
        ('ASSET_004419', 'symbol', '004419', 10),
        ('ASSET_004419', 'canonical_symbol', '004419', 15),
        ('ASSET_004419', 'name', '汇添富美元债债券（QDII）人民币A', 20),
        ('ASSET_004419', 'display_name', '汇添富美元债债券（QDII）人民币A', 25),
        ('ASSET_011035', 'symbol', '011035', 10),
        ('ASSET_011035', 'canonical_symbol', '011035', 15),
        ('ASSET_011035', 'name', '嘉实中证稀土ETF联接A', 20),
        ('ASSET_011035', 'display_name', '嘉实中证稀土ETF联接A', 25),
        ('A_002185', 'symbol', '002185', 10),
        ('A_002185', 'canonical_symbol', '002185', 15),
        ('A_002185', 'name', '华天科技', 20),
        ('A_002185', 'display_name', '华天科技', 25),
        ('ASSET_CASH_HKD', 'symbol', 'CASH_HKD', 10),
        ('ASSET_CASH_HKD', 'name', 'Cash HKD', 20),
        ('ASSET_CASH_KRW', 'symbol', 'CASH_KRW', 10),
        ('ASSET_CASH_KRW', 'name', 'Cash KRW', 20),
        ('ASSET_LOAN_OUT_CCB_20260531', 'symbol', 'LOAN_OUT_CCB_20260531', 10),
        ('ASSET_LOAN_OUT_CCB_20260531', 'name', '他人外借待归还', 20),
        ('ASSET_RECEIVABLE_LOAN_CNY', 'symbol', 'RECEIVABLE_LOAN_CNY', 10),
        ('ASSET_RECEIVABLE_LOAN_CNY', 'name', '他人借款', 20),
        ('ASSET_WECHAT_BALANCE', 'symbol', 'WECHAT_BALANCE', 10),
        ('ASSET_WECHAT_BALANCE', 'name', '微信零钱', 20),
        ('ASSET_WECHAT_LQT', 'symbol', 'WECHAT_LQT', 10),
        ('ASSET_WECHAT_LQT', 'name', '微信零钱通', 20),
        ('ASSET_TENCENT_HUOQI_PLUS', 'symbol', 'TENCENT_HUOQI_PLUS', 10),
        ('ASSET_TENCENT_HUOQI_PLUS', 'name', '活期+', 20),
        ('ASSET_CREDIT_CARD_CNY', 'symbol', 'CREDIT_CARD_CNY', 10),
        ('ASSET_CREDIT_CARD_CNY', 'name', '建行0453信用卡', 20),
        ('ASSET_CREDIT_CARD_CNY', 'name', '广发7443信用卡', 20),
        ('ASSET_CREDIT_CARD_USD', 'symbol', 'CREDIT_CARD_USD', 10),
        ('ASSET_CREDIT_CARD_USD', 'name', '广发9711信用卡', 20),
        ('ASSET_JYLC_WXGSJX', 'symbol', 'JYLC_WXGSJX', 10),
        ('ASSET_JYLC_WXGSJX', 'name', '交银理财稳享固收精选', 20),
        ('ASSET_CCB_PHYSICAL_GOLD', 'symbol', 'CCB_PHYSICAL_GOLD', 10),
        ('ASSET_CCB_PHYSICAL_GOLD', 'name', '建行黄金实物', 20),
        ('ASSET_CASH_CNY', 'name', 'cgb_cny现金', 20),
        ('ASSET_CASH_CNY', 'name', '建行7332现金', 20),
        ('ASSET_CASH_CNY', 'name', '建行现金', 20)
)
INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority)
SELECT asset_id, alias_type, alias_value, 'migration_029_holding_asset_identity', priority
FROM alias_seed
ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
    source = EXCLUDED.source,
    priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority);

-- Exact overrides for holdings that have no symbol or need a known correction.
WITH holding_asset_map(symbol, name, account_code, currency, asset_id) AS (
    VALUES
        ('020640', NULL, NULL, NULL, 'ASSET_020640'),
        ('020839', NULL, NULL, NULL, 'ASSET_020839'),
        ('009033', NULL, NULL, NULL, 'ASSET_009033'),
        ('004419', NULL, NULL, NULL, 'ASSET_004419'),
        ('011035', NULL, NULL, NULL, 'ASSET_011035'),
        ('002185', NULL, NULL, NULL, 'A_002185'),
        ('CASH_HKD', NULL, NULL, NULL, 'ASSET_CASH_HKD'),
        ('CASH_KRW', NULL, NULL, NULL, 'ASSET_CASH_KRW'),
        ('LOAN_OUT_CCB_20260531', NULL, NULL, NULL, 'ASSET_LOAN_OUT_CCB_20260531'),
        ('WECHAT_BALANCE', NULL, NULL, NULL, 'ASSET_WECHAT_BALANCE'),
        ('WECHAT_LQT', NULL, NULL, NULL, 'ASSET_WECHAT_LQT'),
        ('110020', NULL, NULL, NULL, 'ETF_110020'),
        (NULL, 'cgb_cny现金', 'cgb_cny', 'CNY', 'ASSET_CASH_CNY'),
        (NULL, '交银理财稳享固收精选', 'ccb_cny', 'CNY', 'ASSET_JYLC_WXGSJX'),
        (NULL, '他人借款', 'loan', 'CNY', 'ASSET_RECEIVABLE_LOAN_CNY'),
        (NULL, '公积金', 'hpf', 'CNY', 'ASSET_NAME_7BC494786287AD4A'),
        (NULL, '广发7443信用卡', 'cgb_cny', 'CNY', 'ASSET_CREDIT_CARD_CNY'),
        (NULL, '广发9711信用卡', 'cgb_cny', 'USD', 'ASSET_CREDIT_CARD_USD'),
        (NULL, '建信上海金ETF联接A', 'ccb_cny', 'CNY', 'ASSET_009033'),
        (NULL, '建行0453信用卡', 'ccb_cny', 'CNY', 'ASSET_CREDIT_CARD_CNY'),
        (NULL, '建行7332现金', 'ccb_cny', 'CNY', 'ASSET_CASH_CNY'),
        (NULL, '建行现金', 'jianhang', 'CNY', 'ASSET_CASH_CNY'),
        (NULL, '建行黄金实物', 'ccb_cny', 'CNY', 'ASSET_CCB_PHYSICAL_GOLD'),
        (NULL, '比亚迪', 'tonghuashun', 'CNY', 'A_002594'),
        (NULL, '活期+', 'tencent_licaitong', 'CNY', 'ASSET_TENCENT_HUOQI_PLUS'),
        (NULL, '深圳公积金账户', 'hpf', 'CNY', 'ASSET_NAME_7BC494786287AD4A'),
        (NULL, '现金', 'santander', 'MXN', 'ASSET_CASH_MXN'),
        (NULL, '现金', 'wechat', 'CNY', 'ASSET_WECHAT_BALANCE'),
        (NULL, '紫金矿业', 'tonghuashun', 'CNY', 'A_601899'),
        (NULL, '罗 牛 山', 'tonghuashun', 'CNY', 'A_000735'),
        (NULL, '铜陵有色', 'tonghuashun', 'CNY', 'A_000630')
)
UPDATE pams_holdings_snapshot h
SET asset_id = m.asset_id,
    updated_at = now()
FROM holding_asset_map m
WHERE (m.symbol IS NULL OR h.symbol = m.symbol)
  AND (m.name IS NULL OR h.name = m.name)
  AND (m.account_code IS NULL OR h.account_code = m.account_code)
  AND (m.currency IS NULL OR h.currency = m.currency);

WITH primary_match AS (
    SELECT h.id, am.asset_id
    FROM pams_holdings_snapshot h
    JOIN pams_asset_master am
      ON am.primary_symbol = h.symbol
     AND am.status = 'active'
    WHERE h.asset_id IS NULL
      AND h.symbol IS NOT NULL
)
UPDATE pams_holdings_snapshot h
SET asset_id = pm.asset_id,
    updated_at = now()
FROM primary_match pm
WHERE h.id = pm.id;

WITH alias_match AS (
    SELECT DISTINCT ON (h.id) h.id, aa.asset_id
    FROM pams_holdings_snapshot h
    JOIN pams_asset_alias aa
      ON aa.alias_value = COALESCE(h.symbol, h.name)
    JOIN pams_asset_master am
      ON am.asset_id = aa.asset_id
     AND am.status = 'active'
    WHERE h.asset_id IS NULL
    ORDER BY h.id, aa.priority ASC, aa.asset_id ASC
)
UPDATE pams_holdings_snapshot h
SET asset_id = am.asset_id,
    updated_at = now()
FROM alias_match am
WHERE h.id = am.id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_holdings_snapshot_asset_id'
    ) THEN
        ALTER TABLE pams_holdings_snapshot
            ADD CONSTRAINT fk_pams_holdings_snapshot_asset_id
            FOREIGN KEY (asset_id)
            REFERENCES pams_asset_master(asset_id)
            ON UPDATE CASCADE
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END $$;

ALTER TABLE pams_holdings_snapshot VALIDATE CONSTRAINT fk_pams_holdings_snapshot_asset_id;

CREATE INDEX IF NOT EXISTS idx_pams_holdings_snapshot_asset_id
    ON pams_holdings_snapshot(asset_id);

COMMIT;
