-- ============================================
-- PAMS 账户标准化 - 数据库更新
-- ============================================

-- 更新交易表，添加账户和币种约束
-- 将默认币种改为 MXN，默认账户改为 Santander

-- 1. 修改默认值
ALTER TABLE pams_transactions ALTER COLUMN currency SET DEFAULT 'MXN';
ALTER TABLE pams_transactions ALTER COLUMN account SET DEFAULT 'Santander';

-- 2. 添加账户CHECK约束（标准账户列表）
ALTER TABLE pams_transactions DROP CONSTRAINT IF EXISTS chk_account_valid;
ALTER TABLE pams_transactions ADD CONSTRAINT chk_account_valid
  CHECK (account IN (
    '腾讯理财通',
    '建行',
    '华为账户',
    '公积金账户',
    '币安',
    'IBKR',
    '广发',
    'Santander',
    '同花顺',
    '借款',
    '微信'
  ));

-- 3. 添加币种CHECK约束（支持的币种）
ALTER TABLE pams_transactions DROP CONSTRAINT IF EXISTS chk_currency_valid;
ALTER TABLE pams_transactions ADD CONSTRAINT chk_currency_valid
  CHECK (currency IN ('CNY', 'USD', 'MXN'));

-- 4. 更新预算表的默认币种
ALTER TABLE pams_budgets ALTER COLUMN currency SET DEFAULT 'CNY';

-- 5. 创建账户汇率视图（用于统一换算到CNY）
CREATE OR REPLACE VIEW v_transactions_cny AS
SELECT
  t.*,
  CASE
    WHEN t.currency = 'CNY' THEN t.amount
    WHEN t.currency = 'MXN' THEN t.amount * COALESCE((
      SELECT rate FROM pams_fx_rates_daily
      WHERE pair = 'MXN/CNY'
      AND as_of <= t.date
      ORDER BY as_of DESC LIMIT 1
    ), 0.35)  -- 默认汇率 1 MXN = 0.35 CNY
    WHEN t.currency = 'USD' THEN t.amount * COALESCE((
      SELECT rate FROM pams_fx_rates_daily
      WHERE pair = 'USD/CNY'
      AND as_of <= t.date
      ORDER BY as_of DESC LIMIT 1
    ), 7.2)  -- 默认汇率 1 USD = 7.2 CNY
    ELSE t.amount
  END AS amount_cny
FROM pams_transactions t;

-- 6. 添加注释
COMMENT ON CONSTRAINT chk_account_valid ON pams_transactions IS '账户必须是资产负债表中定义的标准账户';
COMMENT ON CONSTRAINT chk_currency_valid ON pams_transactions IS '币种限制：CNY（人民币）、USD（美元）、MXN（墨西哥比索）';
COMMENT ON VIEW v_transactions_cny IS '交易记录CNY视图，自动将所有币种换算为人民币';

-- 7. 更新已有数据（可选，将旧的account值映射到新标准）
-- 如果数据库中已有数据，需要先取消约束，更新数据后再添加约束
-- UPDATE pams_transactions SET account = 'Santander' WHERE account = 'santander';
-- UPDATE pams_transactions SET account = '微信' WHERE account = 'wechat';
