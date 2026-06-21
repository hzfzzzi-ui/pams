BEGIN;

-- BO QIU / qiubo：人民币转出与 Santander 比索入账在 1 天内、金额折算差异约 1.3%，
-- 归为个人换汇中转，不进入收入或消费。
UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '换汇转账',
    counter_account_code = CASE
        WHEN cashflow_id = 105 THEN 'santander'
        WHEN cashflow_id = 971 THEN 'wechat'
        ELSE counter_account_code
    END,
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = 'peer_fx:bo_qiu:2026-01-09',
    review_status = 'auto',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'bo_qiu_peer_fx_transfer'),
    updated_at = NOW()
WHERE cashflow_id IN (105, 971);

UPDATE pams_bank_statement_rows
SET
    category = '内部划转',
    subcategory = '换汇转账',
    auto_classified = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'bo_qiu_peer_fx_transfer')
WHERE id IN (51, 697);

-- 淘宝退款：微信侧保留消费退款，建行侧只是支付通道退款入账映射，避免退款收入双算。
UPDATE pams_cashflow_fact
SET
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '支付通道退款映射',
    counter_account_code = 'wechat',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = 'payment_proxy_refund:taobao:2026-01-23:' || amount::text,
    review_status = 'auto',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'taobao_refund_bank_side_dedup'),
    updated_at = NOW()
WHERE cashflow_id IN (201, 202);

UPDATE pams_bank_statement_rows
SET
    category = '内部划转',
    subcategory = '支付通道退款映射',
    auto_classified = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'taobao_refund_bank_side_dedup')
WHERE id IN (962, 963);

-- 2 月房租已有 Santander 正式账单流水，旧手工流水 pams_transactions:76 是重复记录。
UPDATE pams_cashflow_fact
SET
    cashflow_type = 'balance_adjustment',
    category = '内部划转',
    subcategory = '重复流水忽略',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    review_status = 'duplicate',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'legacy_duplicate_rent_ignored'),
    updated_at = NOW()
WHERE cashflow_id = 911
  AND source_table = 'pams_transactions'
  AND source_id = '76';

-- 已完成自动归类的项目，不应继续挂 needs_review；只有待确认收入/支出保留人工复核。
UPDATE pams_cashflow_fact
SET
    review_status = 'auto',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object('audit_cleanup_057', 'review_status_resolved'),
    updated_at = NOW()
WHERE review_status = 'needs_review'
  AND category NOT IN ('待确认支出', '待确认收入');

COMMIT;
