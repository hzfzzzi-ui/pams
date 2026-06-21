-- Reclassify An Jingjing pass-through cashflows.
-- Source: user confirmation on 2026-06-21.
--
-- These CCB 7332 rows are same-counterparty temporary pass-through flows:
-- - 2026-03-15 +50,000 then -50,000
-- - 2026-03-15 +40,000 then 2026-03-16 -40,000
--
-- They should not be treated as operating income or spending.

BEGIN;

WITH pass_through_rows (
    cashflow_id,
    transfer_group_id
) AS (
    VALUES
        (550::bigint, 'pass_through:anjingjing:ccb_7332:20260315:50000'),
        (551::bigint, 'pass_through:anjingjing:ccb_7332:20260315:50000'),
        (552::bigint, 'pass_through:anjingjing:ccb_7332:20260315_20260316:40000'),
        (558::bigint, 'pass_through:anjingjing:ccb_7332:20260315_20260316:40000')
)
UPDATE pams_cashflow_fact cf
SET cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '资金划转',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = p.transfer_group_id,
    classification_source = 'manual',
    review_status = 'confirmed',
    raw_json = COALESCE(cf.raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_override',
        jsonb_build_object(
            'reason', '用户确认安晶晶转账为先转入后转出的临时资金划转，不计入收入或消费',
            'confirmed_at', '2026-06-21',
            'cashflow_type', 'internal_transfer'
        )
    ),
    updated_at = now()
FROM pass_through_rows p
WHERE cf.cashflow_id = p.cashflow_id
  AND cf.source_table = 'pams_bank_statement_rows'
  AND cf.source_id IN ('1080', '1081', '1082', '1083');

COMMIT;
