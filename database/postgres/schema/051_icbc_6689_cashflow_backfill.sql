-- Backfill ICBC 6689 screenshot-confirmed cashflows and account labels.
-- Source: user-provided ICBC mobile screenshots on 2026-06-21.

BEGIN;

UPDATE pams_accounts
SET display_name = '工行6689人民币储蓄卡',
    institution = '工商银行',
    account_type = 'bank',
    account_role = 'cash',
    updated_at = now()
WHERE account_code = 'icbc_6689_cny';

UPDATE pams_accounts
SET display_name = '工行6689美元储蓄卡',
    institution = '工商银行',
    account_type = 'bank',
    account_role = 'cash',
    updated_at = now()
WHERE account_code = 'icbc_6689_usd';

WITH manual_facts (
    source_id,
    event_date,
    account_code,
    counter_account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    transfer_group_id,
    confidence
) AS (
    VALUES
        (
            '20251211_icbc_to_ccb_50000',
            DATE '2025-12-11',
            'icbc_6689_cny',
            'ccb_7332',
            -50000.0000,
            'CNY',
            1.0000000000,
            -50000.0000,
            'internal_transfer',
            '内部划转',
            '银行账户转账',
            '黄展峰',
            '跨行汇款',
            '跨行汇款 黄展峰 6217003370010267332 手机银行；工商银行 App 截图补录',
            FALSE,
            FALSE,
            FALSE,
            TRUE,
            'account_transfer:icbc_to_ccb_7332:manual-20251211-50000:876:50000.00',
            0.9800
        ),
        (
            '20251211_icbc_to_ccb_10729_43',
            DATE '2025-12-11',
            'icbc_6689_cny',
            'ccb_7332',
            -10729.4300,
            'CNY',
            1.0000000000,
            -10729.4300,
            'internal_transfer',
            '内部划转',
            '银行账户转账',
            '黄展峰',
            '跨行汇款',
            '跨行汇款 黄展峰；工商银行 App 截图补录',
            FALSE,
            FALSE,
            FALSE,
            TRUE,
            'account_transfer:icbc_to_ccb_7332:manual-20251211-10729.43:877:10729.43',
            0.9800
        ),
        (
            '20251211_icbc_fx_settlement_cny_36553_42',
            DATE '2025-12-11',
            'icbc_6689_cny',
            NULL,
            36553.4200,
            'CNY',
            1.0000000000,
            36553.4200,
            'fx_conversion',
            '内部划转',
            '换汇转账',
            '手机银行',
            '个人结汇',
            '个人结汇 外币结汇入人民币；截图标注不计入，作为账户内换汇处理',
            FALSE,
            FALSE,
            FALSE,
            TRUE,
            'fx_conversion:icbc_6689:20251211:36553.42',
            0.9500
        ),
        (
            '20251211_icbc_to_ccb_36553_42',
            DATE '2025-12-11',
            'icbc_6689_cny',
            'ccb_7332',
            -36553.4200,
            'CNY',
            1.0000000000,
            -36553.4200,
            'internal_transfer',
            '内部划转',
            '银行账户转账',
            '黄展峰',
            '跨行汇款',
            '跨行汇款 黄展峰；工商银行 App 截图补录',
            FALSE,
            FALSE,
            FALSE,
            TRUE,
            'account_transfer:icbc_to_ccb_7332:manual-20251211-36553.42:878:36553.42',
            0.9800
        ),
        (
            '20251221_icbc_interest_6_83',
            DATE '2025-12-21',
            'icbc_6689_cny',
            NULL,
            6.8300,
            'CNY',
            1.0000000000,
            6.8300,
            'investment_return_cash',
            '投资收益',
            '现金利息',
            NULL,
            '利息',
            '利息 批量业务；工商银行 App 截图补录',
            FALSE,
            TRUE,
            FALSE,
            FALSE,
            NULL,
            0.9800
        ),
        (
            '20251222_licaitong_redeem_19186_93',
            DATE '2025-12-22',
            'icbc_6689_cny',
            'tencent_licaitong',
            19186.9300,
            'CNY',
            1.0000000000,
            19186.9300,
            'investment_withdrawal',
            '内部划转',
            '投资出金',
            '财付通',
            '理财通赎回',
            '理财通赎回 财付通；工商银行 App 截图补录',
            FALSE,
            FALSE,
            TRUE,
            FALSE,
            'investment_withdrawal:tencent_licaitong_to_icbc_6689:manual-20251222:19186.93',
            0.9800
        ),
        (
            '20251223_licaitong_purchase_1000',
            DATE '2025-12-23',
            'icbc_6689_cny',
            'tencent_licaitong',
            -1000.0000,
            'CNY',
            1.0000000000,
            -1000.0000,
            'investment_funding',
            '内部划转',
            '投资账户转账',
            '财付通-理财通-腾安基金销售',
            '金融付款',
            '金融付款 财付通-理财通-腾安基金销售；工商银行 App 截图补录',
            FALSE,
            FALSE,
            TRUE,
            FALSE,
            'investment_funding:icbc_to_tencent_licaitong:manual-20251223:none:1000.00',
            0.9800
        ),
        (
            '20251230_licaitong_purchase_1000',
            DATE '2025-12-30',
            'icbc_6689_cny',
            'tencent_licaitong',
            -1000.0000,
            'CNY',
            1.0000000000,
            -1000.0000,
            'investment_funding',
            '内部划转',
            '投资账户转账',
            '财付通-理财通-腾安基金销售',
            '理财',
            '理财 财付通-理财通-腾安基金销售；工商银行 App 截图补录',
            FALSE,
            FALSE,
            TRUE,
            FALSE,
            'investment_funding:icbc_to_tencent_licaitong:manual-20251230:none:1000.00',
            0.9800
        )
)
INSERT INTO pams_cashflow_fact (
    event_date,
    posting_date,
    account_code,
    counter_account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    transfer_group_id,
    source_table,
    source_id,
    source_uid,
    source_hash,
    raw_json,
    classification_source,
    confidence,
    review_status
)
SELECT
    event_date,
    event_date,
    account_code,
    counter_account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    transfer_group_id,
    'manual_icbc_6689_screenshot',
    source_id,
    source_id,
    md5('manual_icbc_6689_screenshot:' || source_id) || md5('manual_icbc_6689_screenshot:' || source_id || ':v1'),
    jsonb_build_object(
        'source', 'user_screenshot',
        'account_tail', '6689',
        'captured_at', '2026-06-21',
        'note', '工商银行 App 截图人工补录'
    ),
    'manual_screenshot',
    confidence,
    'confirmed'
FROM manual_facts
ON CONFLICT (source_table, source_id) DO UPDATE SET
    event_date = EXCLUDED.event_date,
    posting_date = EXCLUDED.posting_date,
    account_code = EXCLUDED.account_code,
    counter_account_code = EXCLUDED.counter_account_code,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    amount_cny = EXCLUDED.amount_cny,
    cashflow_type = EXCLUDED.cashflow_type,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    counterparty = EXCLUDED.counterparty,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    affects_spending = EXCLUDED.affects_spending,
    affects_nav = EXCLUDED.affects_nav,
    affects_investment_capital = EXCLUDED.affects_investment_capital,
    is_internal_transfer = EXCLUDED.is_internal_transfer,
    transfer_group_id = EXCLUDED.transfer_group_id,
    source_uid = EXCLUDED.source_uid,
    raw_json = pams_cashflow_fact.raw_json || EXCLUDED.raw_json,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    updated_at = now();

UPDATE pams_cashflow_fact
SET cashflow_type = 'internal_transfer',
    counter_account_code = 'icbc_6689_cny',
    category = '内部划转',
    subcategory = '银行账户转账',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    classification_source = 'manual_screenshot',
    review_status = 'confirmed',
    transfer_group_id = CASE source_id
        WHEN '876' THEN 'account_transfer:icbc_to_ccb_7332:manual-20251211-50000:876:50000.00'
        WHEN '877' THEN 'account_transfer:icbc_to_ccb_7332:manual-20251211-10729.43:877:10729.43'
        WHEN '878' THEN 'account_transfer:icbc_to_ccb_7332:manual-20251211-36553.42:878:36553.42'
        ELSE transfer_group_id
    END,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pairing',
        jsonb_build_object(
            'source', 'user_screenshot',
            'paired_account', 'icbc_6689_cny',
            'updated_at', now()
        )
    ),
    updated_at = now()
WHERE cashflow_id IN (11, 12, 13);

UPDATE pams_cashflow_fact
SET cashflow_type = 'internal_transfer',
    counter_account_code = 'santander',
    category = '内部划转',
    subcategory = '换汇转账',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    classification_source = 'manual_screenshot',
    review_status = 'confirmed',
    transfer_group_id = 'fx_transfer:santander_to_icbc_6689_usd:missing:814:555.38',
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pairing',
        jsonb_build_object(
            'source', 'user_screenshot',
            'reason', '本人外币资金转入工行 6689 美元子账户，未找到 Santander 侧成对流水',
            'updated_at', now()
        )
    ),
    updated_at = now()
WHERE cashflow_id = 476;

WITH cases (
    case_type,
    relation_code,
    from_account_code,
    to_account_code,
    source_ref_table,
    source_ref_id,
    target_ref_table,
    target_ref_id,
    amount,
    currency,
    case_date,
    confidence,
    status,
    note
) AS (
    VALUES
        ('account_transfer', 'icbc_to_ccb_7332', 'icbc_6689_cny', 'ccb_7332', 'manual_icbc_6689_screenshot', '20251211_icbc_to_ccb_50000', 'pams_bank_statement_rows', '876', 50000.0000, 'CNY', DATE '2025-12-11', 0.9800, 'confirmed', '截图补录：工行 6689 转建行 7332'),
        ('account_transfer', 'icbc_to_ccb_7332', 'icbc_6689_cny', 'ccb_7332', 'manual_icbc_6689_screenshot', '20251211_icbc_to_ccb_10729_43', 'pams_bank_statement_rows', '877', 10729.4300, 'CNY', DATE '2025-12-11', 0.9800, 'confirmed', '截图补录：工行 6689 转建行 7332'),
        ('account_transfer', 'icbc_to_ccb_7332', 'icbc_6689_cny', 'ccb_7332', 'manual_icbc_6689_screenshot', '20251211_icbc_to_ccb_36553_42', 'pams_bank_statement_rows', '878', 36553.4200, 'CNY', DATE '2025-12-11', 0.9800, 'confirmed', '截图补录：工行 6689 转建行 7332'),
        ('internal_transfer', 'tencent_licaitong_to_icbc_6689', 'tencent_licaitong', 'icbc_6689_cny', 'manual_icbc_6689_screenshot', '20251222_licaitong_redeem_19186_93', NULL, NULL, 19186.9300, 'CNY', DATE '2025-12-22', 0.9800, 'confirmed', '截图补录：理财通赎回至工行 6689'),
        ('investment_funding', 'icbc_to_tencent_licaitong', 'icbc_6689_cny', 'tencent_licaitong', 'manual_icbc_6689_screenshot', '20251223_licaitong_purchase_1000', NULL, NULL, 1000.0000, 'CNY', DATE '2025-12-23', 0.9800, 'confirmed', '截图补录：工行 6689 申购理财通'),
        ('investment_funding', 'icbc_to_tencent_licaitong', 'icbc_6689_cny', 'tencent_licaitong', 'manual_icbc_6689_screenshot', '20251230_licaitong_purchase_1000', NULL, NULL, 1000.0000, 'CNY', DATE '2025-12-30', 0.9800, 'confirmed', '截图补录：工行 6689 申购理财通'),
        ('internal_transfer', 'santander_to_icbc_6689_usd', 'santander', 'icbc_6689_usd', NULL, NULL, 'pams_bank_statement_rows', '814', 555.3800, 'USD', DATE '2026-02-25', 0.8500, 'open', '截图确认工行美元入账；当前未找到 Santander 侧成对流水')
)
INSERT INTO pams_reconciliation_case (
    case_type,
    relation_code,
    from_account_code,
    to_account_code,
    source_ref_table,
    source_ref_id,
    target_ref_table,
    target_ref_id,
    amount,
    currency,
    case_date,
    confidence,
    status,
    note,
    raw_json
)
SELECT
    case_type,
    relation_code,
    from_account_code,
    to_account_code,
    source_ref_table,
    source_ref_id,
    target_ref_table,
    target_ref_id,
    amount,
    currency,
    case_date,
    confidence,
    status,
    note,
    jsonb_build_object('source', 'manual_icbc_6689_screenshot', 'captured_at', '2026-06-21')
FROM cases c
WHERE NOT EXISTS (
    SELECT 1
    FROM pams_reconciliation_case existing
    WHERE existing.case_type = c.case_type
      AND existing.relation_code = c.relation_code
      AND existing.from_account_code = c.from_account_code
      AND existing.to_account_code = c.to_account_code
      AND existing.case_date = c.case_date
      AND existing.amount = c.amount
      AND existing.currency = c.currency
);

COMMIT;
