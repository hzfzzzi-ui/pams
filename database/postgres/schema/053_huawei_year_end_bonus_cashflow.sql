-- Reclassify confirmed Huawei year-end bonus cashflows.
-- Source: user confirmation on 2026-06-21.
--
-- These CCB 7332 ordinary remittance rows should be personal income:
-- - 2026-01-04 Huawei Technologies employee payment
-- - 2026-03-31 Huawei Investment Holding union committee remittance
--
-- They should be grouped as income / bonus / year-end bonus in the ledger page.

BEGIN;

WITH bonus_rows (
    cashflow_id,
    source_id,
    confirmed_counterparty
) AS (
    VALUES
        (76::bigint, '911', '4000020329200195323/华为技术有限 公司'),
        (627::bigint, '1094', '4000020319200082565/华为投资控股 有限公司工会委员会')
)
UPDATE pams_cashflow_fact cf
SET cashflow_type = 'operating_income',
    category = '奖金',
    subcategory = '年终奖',
    counterparty = b.confirmed_counterparty,
    counter_account_code = NULL,
    affects_spending = TRUE,
    affects_nav = TRUE,
    affects_investment_capital = FALSE,
    is_internal_transfer = FALSE,
    transfer_group_id = NULL,
    classification_source = 'manual',
    auto_classified = FALSE,
    review_status = 'confirmed',
    raw_json = COALESCE(cf.raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_override',
        jsonb_build_object(
            'reason', '用户确认华为普通汇兑为年终奖收入，归入收入/奖金/年终奖',
            'confirmed_at', '2026-06-21',
            'cashflow_type', 'operating_income',
            'category', '奖金',
            'subcategory', '年终奖'
        )
    ),
    updated_at = now()
FROM bonus_rows b
WHERE cf.cashflow_id = b.cashflow_id
  AND cf.source_table = 'pams_bank_statement_rows'
  AND cf.source_id = b.source_id;

COMMIT;
