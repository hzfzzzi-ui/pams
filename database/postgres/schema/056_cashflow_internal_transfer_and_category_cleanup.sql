BEGIN;

-- 1) Santander -> IBKR 的单边资金划转：修复历史编码污染，不直接当作消费。
UPDATE pams_cashflow_fact
SET
    category = '内部划转',
    subcategory = '投资入金',
    counter_account_code = COALESCE(counter_account_code, 'ibkr'),
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    classification_source = COALESCE(NULLIF(classification_source, ''), 'audit_cleanup'),
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: santander_ibkr_internal_transfer'),
    updated_at = NOW()
WHERE account_code = 'santander'
  AND cashflow_type IN ('internal_transfer', 'investment_funding')
  AND (
      description ILIKE '%INTERACTIVE BROKERS%'
      OR counterparty ILIKE '%INTERACTIVE BROKERS%'
      OR summary ILIKE '%INTERACTIVE BROKERS%'
  );

UPDATE pams_bank_statement_rows
SET
    category = '内部划转',
    subcategory = '投资入金',
    auto_classified = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: santander_ibkr_internal_transfer')
WHERE account_code = 'santander'
  AND (
      description ILIKE '%INTERACTIVE BROKERS%'
      OR counterparty ILIKE '%INTERACTIVE BROKERS%'
      OR summary ILIKE '%INTERACTIVE BROKERS%'
  );

-- 2) 支付通道去重：银行/信用卡资金侧只是扣款映射，不进入消费统计。
WITH proxy_source_rows AS (
    SELECT DISTINCT source.cashflow_id
    FROM pams_cashflow_fact source
    JOIN pams_cashflow_fact target
      ON source.cashflow_id <> target.cashflow_id
     AND source.event_date = target.event_date
     AND source.amount_cny < 0
     AND target.amount_cny < 0
     AND ABS(ABS(source.amount_cny) - ABS(target.amount_cny)) <= 0.02
     AND target.account_code IN ('wechat', 'alipay')
     AND source.account_code NOT IN ('wechat', 'alipay')
    WHERE (
        source.description ILIKE '%财付通%'
        OR source.description ILIKE '%支付宝%'
        OR source.description ILIKE '%微信支付%'
        OR source.description ILIKE '%微信转账%'
        OR source.description ILIKE '%扫二维码付款%'
        OR target.description ILIKE '%建设银行%'
        OR target.description ILIKE '%广发银行%'
        OR target.description ILIKE '%信用卡%'
    )
)
UPDATE pams_cashflow_fact cf
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '支付通道扣款映射',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = COALESCE(NULLIF(transfer_group_id, ''), 'payment_proxy:dedup:' || cf.cashflow_id::text),
    classification_source = COALESCE(NULLIF(classification_source, ''), 'audit_cleanup'),
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: payment_proxy_source_dedup'),
    updated_at = NOW()
FROM proxy_source_rows p
WHERE cf.cashflow_id = p.cashflow_id;

-- 已经是 internal_transfer 但分类仍为未分类/餐饮的支付通道资金侧，补齐为去重口径。
UPDATE pams_cashflow_fact
SET
    category = '内部划转',
    subcategory = '支付通道扣款映射',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = COALESCE(NULLIF(transfer_group_id, ''), 'payment_proxy:dedup:' || cashflow_id::text),
    classification_source = COALESCE(NULLIF(classification_source, ''), 'audit_cleanup'),
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: payment_proxy_source_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'internal_transfer'
  AND account_code NOT IN ('wechat', 'alipay', 'santander')
  AND (
      category IS DISTINCT FROM '内部划转'
      OR subcategory IS DISTINCT FROM '支付通道扣款映射'
  )
  AND (
      description ILIKE '%财付通%'
      OR description ILIKE '%支付宝%'
      OR description ILIKE '%微信支付%'
      OR description ILIKE '%微信转账%'
      OR description ILIKE '%扫二维码付款%'
  );

UPDATE pams_bank_statement_rows br
SET
    category = cf.category,
    subcategory = cf.subcategory,
    auto_classified = TRUE,
    raw_json = COALESCE(br.raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: sync_cashflow_category')
FROM pams_cashflow_fact cf
WHERE cf.source_table = 'pams_bank_statement_rows'
  AND cf.source_id ~ '^[0-9]+$'
  AND br.id = cf.source_id::integer
  AND cf.category = '内部划转'
  AND cf.subcategory IN ('支付通道扣款映射', '投资入金');

-- 3) 非消费现金流的历史粗分类清理。
UPDATE pams_cashflow_fact
SET
    category = '内部划转',
    subcategory = '投资账户转账',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = TRUE,
    is_internal_transfer = FALSE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: investment_funding_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'investment_funding'
  AND category IS DISTINCT FROM '内部划转';

UPDATE pams_cashflow_fact
SET
    category = '内部划转',
    subcategory = '信用卡还款',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = FALSE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: debt_repayment_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'debt_repayment'
  AND category IS DISTINCT FROM '内部划转';

UPDATE pams_cashflow_fact
SET
    category = '费用',
    subcategory = '手续费',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: fee_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'fee'
  AND category IS DISTINCT FROM '费用';

UPDATE pams_cashflow_fact
SET
    category = '税费',
    subcategory = '税费',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: tax_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'tax'
  AND category IS DISTINCT FROM '税费';

UPDATE pams_cashflow_fact
SET
    category = '内部划转',
    subcategory = '余额调整',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: balance_adjustment_category_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'balance_adjustment'
  AND category IS DISTINCT FROM '内部划转';

-- 4) 微信/支付宝真实消费侧：从“支付通道/日常消费/未分类”收敛到常规消费分类。
UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '机票火车', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_travel'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交通')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%携程%'
      OR description ILIKE '%12306%'
      OR description ILIKE '%中铁网络%'
      OR description ILIKE '%华程%'
      OR description ILIKE '%赫程%'
      OR description ILIKE '%旅行社%'
      OR counterparty ILIKE '%携程%'
      OR counterparty ILIKE '%12306%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '酒店住宿', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_hotel'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交通')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%酒店%'
      OR description ILIKE '%Smartcom%'
      OR description ILIKE '%差旅酒店%'
      OR description ILIKE '%夢想旅行%'
      OR counterparty ILIKE '%酒店%'
      OR counterparty ILIKE '%Smartcom%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '打车', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_ride'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交通')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%Didi%'
      OR description ILIKE '%UBER RIDE%'
      OR description ILIKE '%打车%'
      OR description ILIKE '%Suica%'
      OR counterparty ILIKE '%Didi%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '加油停车', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_parking_fuel'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交通')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%停车%'
      OR description ILIKE '%ETCP%'
      OR description ILIKE '%加油%'
      OR description ILIKE '%团油%'
      OR counterparty ILIKE '%停车%'
      OR counterparty ILIKE '%团油%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '物流快递', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_logistics'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交通')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%顺丰%'
      OR description ILIKE '%物流%'
      OR description ILIKE '%货拉拉%'
      OR description ILIKE '%墨速达%'
      OR counterparty ILIKE '%顺丰%'
      OR counterparty ILIKE '%物流%'
      OR counterparty ILIKE '%货拉拉%'
  );

UPDATE pams_cashflow_fact
SET category = '通讯网络', subcategory = '话费流量', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_telecom'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '通讯', '话费', '居住')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%Telcel%'
      OR description ILIKE '%Wallyt%'
      OR description ILIKE '%手机充值%'
      OR description ILIKE '%话费%'
      OR description ILIKE '%中国电信%'
      OR description ILIKE '%IZZI%'
      OR description ILIKE '%通讯%'
      OR counterparty ILIKE '%Telcel%'
      OR counterparty ILIKE '%Wallyt%'
  );

UPDATE pams_cashflow_fact
SET category = '工具软件', subcategory = '工具订阅', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_tools'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%Tushare%'
      OR description ILIKE '%OPENAI%'
      OR description ILIKE '%MEITOU TORONTO%'
      OR description ILIKE '%八码%'
      OR description ILIKE '%LetsGo Network%'
      OR counterparty ILIKE '%Tushare%'
      OR counterparty ILIKE '%MEITOU%'
      OR counterparty ILIKE '%八码%'
  );

UPDATE pams_cashflow_fact
SET category = '工具软件', subcategory = '数字服务', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_digital_service'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%Apple%'
      OR description ILIKE '%apple.com/bill%'
      OR description ILIKE '%iCloud%'
      OR counterparty ILIKE '%Apple%'
  );

UPDATE pams_cashflow_fact
SET category = '娱乐休闲', subcategory = '影音游戏', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_entertainment'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%银河互联网电视%'
      OR description ILIKE '%小米影视%'
      OR description ILIKE '%Valve%'
      OR description ILIKE '%Steam%'
      OR counterparty ILIKE '%Valve%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '电商购物', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_online_shopping'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%淘宝%'
      OR description ILIKE '%京东%'
      OR description ILIKE '%天猫%'
      OR description ILIKE '%电商%'
      OR counterparty ILIKE '%淘宝%'
      OR counterparty ILIKE '%京东%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '服饰运动', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_clothes_sport'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%滔搏%'
      OR description ILIKE '%UNIQLO%'
      OR description ILIKE '%优衣库%'
      OR description ILIKE '%ZARA%'
      OR description ILIKE '%ABERCROMBIE%'
      OR description ILIKE '%SEARS%'
      OR counterparty ILIKE '%滔搏%'
      OR counterparty ILIKE '%UNIQLO%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '商超便利', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_retail'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%超市%'
      OR description ILIKE '%便利%'
      OR description ILIKE '%CITY MARKET%'
      OR description ILIKE '%HOME DEPOT%'
      OR description ILIKE '%AEON%'
      OR description ILIKE '%MINISO%'
      OR description ILIKE '%MUJI%'
      OR description ILIKE '%无印良品%'
      OR description ILIKE '%机场%'
      OR description ILIKE '%NARITA AIRPORT%'
      OR counterparty ILIKE '%超市%'
      OR counterparty ILIKE '%CITY MARKET%'
  );

UPDATE pams_cashflow_fact
SET category = '医疗健康', subcategory = '药品医疗', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_health'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%药%'
      OR description ILIKE '%医%'
      OR counterparty ILIKE '%药%'
      OR counterparty ILIKE '%医%'
  );

UPDATE pams_cashflow_fact
SET category = '餐饮', subcategory = '日常餐饮', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_food'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '餐饮', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%满客%'
      OR description ILIKE '%霸王茶姬%'
      OR description ILIKE '%星巴克%'
      OR description ILIKE '%咖啡%'
      OR description ILIKE '%饭%'
      OR description ILIKE '%餐%'
      OR description ILIKE '%米粉%'
      OR description ILIKE '%粉馆%'
      OR description ILIKE '%馄饨%'
      OR description ILIKE '%甜宝%'
      OR description ILIKE '%砂锅%'
      OR description ILIKE '%鸡王%'
      OR description ILIKE '%昭州一品%'
      OR description ILIKE '%FOGO%'
      OR description ILIKE '%MAN XIANG%'
      OR description ILIKE '%WINGSTOP%'
      OR description ILIKE '%COM RAP%'
      OR description ILIKE '%PICKUP COFFEE%'
      OR description ILIKE '%SUSHIRO%'
      OR description ILIKE '%CLIP MX%'
      OR description ILIKE '%OXXO%'
      OR counterparty ILIKE '%满客%'
      OR counterparty ILIKE '%餐%'
      OR counterparty ILIKE '%咖啡%'
      OR counterparty ILIKE '%粉%'
  );

UPDATE pams_cashflow_fact
SET category = '生活服务', subcategory = '便民服务', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_life_service'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '其他')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%充电宝%'
      OR description ILIKE '%美团 免押%'
      OR description ILIKE '%5ASEC%'
      OR counterparty ILIKE '%充电宝%'
  );

UPDATE pams_cashflow_fact
SET category = '人情往来', subcategory = '转账支出', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_peer_transfer'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('支付通道', '日常消费', '未分类', '交际')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%微信转账%'
      OR description ILIKE '%转账备注%'
      OR description ILIKE '%群收款%'
      OR description ILIKE '%二维码收款%'
      OR description ILIKE '%扫二维码付款%'
      OR description ILIKE '%微信红包%'
      OR description ILIKE '%红包%'
      OR description ILIKE '%TRANSFERENCIA A%'
      OR summary ILIKE '%转账%'
      OR summary ILIKE '%红包%'
  );

UPDATE pams_cashflow_fact
SET category = '居住', subcategory = '房租', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: expense_category_rent'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      category IN ('房租', '未分类', '日常消费')
      OR subcategory IS NULL
  )
  AND (
      description ILIKE '%房租%'
      OR description ILIKE '%RENTA%'
      OR summary ILIKE '%房租%'
  );

-- ATM 取现是资产形态变化，不应算消费。
UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '现金取现',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: atm_cash_withdrawal_internal_transfer'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      description ILIKE '%ATM%'
      OR summary ILIKE '%取现%'
      OR description ILIKE '%取现%'
  );

-- 5) 第二轮细分：修正历史粗桶、费用和资产申购误入消费的问题。
UPDATE pams_cashflow_fact
SET
    cashflow_type = 'investment_funding',
    category = '内部划转',
    subcategory = '投资申购',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = TRUE,
    is_internal_transfer = FALSE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: gold_accumulation_purchase'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      description ILIKE '%积存金买入%'
      OR summary ILIKE '%积存金买入%'
  );

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '微信零钱充值',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: wechat_balance_topup'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND description ILIKE '%微信零钱充值账户%';

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '账户间划转',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: explicit_internal_transfer'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '内部划转';

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'fee',
    category = '费用',
    subcategory = '违约金',
    affects_spending = FALSE,
    affects_nav = TRUE,
    affects_investment_capital = FALSE,
    is_internal_transfer = FALSE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: credit_card_penalty_fee'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      description ILIKE '%违约金%'
      OR description ILIKE '%信用卡违约%'
      OR summary ILIKE '%违约%'
  );

UPDATE pams_cashflow_fact
SET category = '餐饮', subcategory = '外卖餐饮', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: ubereats_food'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND (
      description ILIKE '%UBER%EATS%'
      OR description ILIKE '%UBEREATS%'
      OR description ILIKE '%Uber eats%'
      OR description ILIKE '%Uber Eats%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '打车', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: legacy_traffic_to_ride'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '交通';

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '公共交通', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: public_transit'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%深圳市地铁%'
      OR description ILIKE '%JR EAST BUS%'
      OR description ILIKE '%HANEDAKUUKOU%'
      OR description ILIKE '%AIRPORT%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '酒店住宿', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: hotel_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%HILTON%'
      OR description ILIKE '%FIESTA INN%'
      OR description ILIKE '%HOTEL%'
  );

UPDATE pams_cashflow_fact
SET category = '交通出行', subcategory = '加油停车', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: gas_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%中海油%'
      OR description ILIKE '%GAS%'
  );

UPDATE pams_cashflow_fact
SET category = '工具软件', subcategory = 'AI订阅', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: ai_subscription'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%CLAUDE%'
      OR description ILIKE '%CHATGPT%'
      OR description ILIKE '%OPENAI%'
  );

UPDATE pams_cashflow_fact
SET category = '娱乐休闲', subcategory = '影音会员', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: media_subscription'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%爱奇艺%'
      OR description ILIKE '%银河互联网电视%'
      OR description ILIKE '%Nuvei%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '商超便利', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: retail_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%MINISUPER%'
      OR description ILIKE '%CITY MARKET%'
      OR description ILIKE '%HOME DEPOT%'
      OR description ILIKE '%SEARS%'
      OR description ILIKE '%MISCELANEA%'
      OR description ILIKE '%STEREN SHOP%'
      OR description ILIKE '%超市%'
      OR description ILIKE '%便利%'
      OR description ILIKE '%MERPAGO*EASTSUPER%'
      OR description ILIKE '%MERPAGO*JINGYULIU%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '服饰运动', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: apparel_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%UNIQLO%'
      OR description ILIKE '%优衣库%'
      OR description ILIKE '%ZARA%'
      OR description ILIKE '%ABERCROMBIE%'
  );

UPDATE pams_cashflow_fact
SET category = '购物消费', subcategory = '日用品', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: household_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category = '日常消费'
  AND (
      description ILIKE '%无印良品%'
      OR description ILIKE '%MUJI%'
  );

UPDATE pams_cashflow_fact
SET category = '餐饮', subcategory = '日常餐饮', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: food_legacy'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%邕味%'
      OR description ILIKE '%牛杂%'
      OR description ILIKE '%饮食%'
      OR description ILIKE '%卷筒粉%'
      OR description ILIKE '%鲜肉%'
      OR description ILIKE '%HOTARU%'
      OR description ILIKE '%KERICO%'
      OR description ILIKE '%LA ESKINA%'
      OR description ILIKE '%CARNESFINAS%'
      OR description ILIKE '%MIRAI%'
      OR description ILIKE '%ZTL*OPERADORA%'
      OR description ILIKE '%KAPELLI%'
      OR description ILIKE '%ABTS G100%'
      OR description ILIKE '%COM RAP%'
  );

UPDATE pams_cashflow_fact
SET category = '人情往来', subcategory = '转账支出', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail', '2026-06-21: peer_group_collection'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      summary ILIKE '%群收款%'
      OR description ILIKE '%群收款%'
      OR summary ILIKE '%转账%'
      OR description ILIKE '%转账备注%'
      OR description ILIKE '%微信红包%'
  );

-- 仍未命中的老“支付通道”不再作为类别展示，降级为待确认支出，方便人工复核。
UPDATE pams_cashflow_fact
SET
    category = '待确认支出',
    subcategory = '手动确认',
    review_status = CASE WHEN review_status = 'confirmed' THEN review_status ELSE 'needs_review' END,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: payment_channel_remaining_review'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('支付通道', '未分类');

-- 同步可追溯源行分类，避免未来重建 fact 时回退。
UPDATE pams_bank_statement_rows br
SET
    category = cf.category,
    subcategory = cf.subcategory,
    auto_classified = TRUE,
    raw_json = COALESCE(br.raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup', '2026-06-21: sync_cashflow_category_final')
FROM pams_cashflow_fact cf
WHERE cf.source_table = 'pams_bank_statement_rows'
  AND cf.source_id ~ '^[0-9]+$'
  AND br.id = cf.source_id::integer
  AND cf.raw_json ? 'audit_cleanup';

-- 7) 最后一组收入/剩余消费收敛：提现是内部流动，红包/朋友转账收入不再挂“日常消费”。
UPDATE pams_cashflow_fact
SET category = '娱乐休闲', subcategory = '影音会员', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: media_subscription_remaining'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%银河互联网%'
      OR description ILIKE '%爱奇艺%'
      OR description ILIKE '%Nuvei%'
  );

UPDATE pams_cashflow_fact
SET category = '医疗健康', subcategory = '药品医疗', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: pharmacy_remaining'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%PHARMACY%'
      OR description ILIKE '%TOMOS%'
      OR counterparty ILIKE '%トモズ%'
  );

UPDATE pams_cashflow_fact
SET category = '通讯网络', subcategory = '话费流量', raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: mobile_reload_remaining'), updated_at = NOW()
WHERE cashflow_type = 'operating_expense'
  AND category IN ('日常消费', '待确认支出')
  AND (
      description ILIKE '%Mobile Reload%'
      OR counterparty ILIKE '%码上跃动%'
  );

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '微信零钱提现',
    counter_account_code = COALESCE(counter_account_code, 'wechat'),
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: wechat_balance_withdrawal'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND (
      description ILIKE '%微信零钱提现%'
      OR description ILIKE '%零钱通提现%'
  );

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '账户间划转',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: income_internal_transfer'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND category = '内部划转';

UPDATE pams_cashflow_fact
SET
    category = '消费退款',
    subcategory = '转账退款',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: transfer_refund_income'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND (
      description ILIKE '%退款%'
      OR summary ILIKE '%退款%'
  );

UPDATE pams_cashflow_fact
SET
    category = '人情往来',
    subcategory = CASE
        WHEN description ILIKE '%红包%' OR summary ILIKE '%红包%' THEN '红包收入'
        ELSE '转账收入'
    END,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: peer_income_cleanup'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND category IN ('日常消费', '交际')
  AND (
      description ILIKE '%微信转账%'
      OR description ILIKE '%红包%'
      OR summary ILIKE '%转账%'
      OR summary ILIKE '%红包%'
      OR summary ILIKE '%二维码收款%'
  );

UPDATE pams_cashflow_fact
SET
    category = '收入',
    subcategory = '年终奖',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: bonus_income_normalization'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND category = '奖金'
  AND subcategory = '年终奖';

UPDATE pams_cashflow_fact
SET
    category = '待确认收入',
    subcategory = '手动确认',
    review_status = CASE WHEN review_status = 'confirmed' THEN review_status ELSE 'needs_review' END,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: remaining_income_review'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND category = '未分类';

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'balance_adjustment',
    category = '内部划转',
    subcategory = '账户验证',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: bank_account_validation_micro_amount'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND category = '日常消费'
  AND ABS(amount_cny) < 0.1
  AND (
      description ILIKE '%Validacion de cuenta%'
      OR description ~ '^[0-9]{20,}$'
  );

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'balance_adjustment',
    category = '内部划转',
    subcategory = '账户验证',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: santander_validation_micro_amount'),
    updated_at = NOW()
WHERE cashflow_type = 'operating_income'
  AND account_code = 'santander'
  AND category = '日常消费'
  AND ABS(amount_cny) < 0.1;

UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '微信零钱充值',
    counter_account_code = COALESCE(counter_account_code, 'wechat'),
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: wechat_balance_topup_status_cleanup'),
    updated_at = NOW()
WHERE description ILIKE '%微信零钱充值账户%';

UPDATE pams_cashflow_fact
SET
    review_status = 'auto',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: review_status_cleanup'),
    updated_at = NOW()
WHERE review_status = 'needs_review'
  AND category NOT IN ('待确认支出', '待确认收入');

UPDATE pams_bank_statement_rows br
SET
    category = cf.category,
    subcategory = cf.subcategory,
    auto_classified = TRUE,
    raw_json = COALESCE(br.raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_detail_2', '2026-06-21: sync_cashflow_category_final_2')
FROM pams_cashflow_fact cf
WHERE cf.source_table = 'pams_bank_statement_rows'
  AND cf.source_id ~ '^[0-9]+$'
  AND br.id = cf.source_id::integer
  AND cf.raw_json ? 'audit_cleanup_detail_2';

COMMIT;
