-- Match the remaining Santander transfer to the ICBC 6689 USD receipt.
-- Source: user confirmation on 2026-06-22.
--
-- Evidence:
-- - cashflow_id=428: Santander outbound, 2026-02-16, -10,000 MXN.
-- - cashflow_id=476: ICBC 6689 USD inbound, 2026-02-25, +555.38 USD.
-- Both sides are the user's own accounts, so this must not be treated as
-- operating expense or income.

BEGIN;

WITH matched_cashflows (
    cashflow_id,
    counter_account_code,
    transfer_group_id
) AS (
    VALUES
        (
            428::bigint,
            'icbc_6689_usd',
            'fx_transfer:santander_to_icbc_6689_usd:20260216_20260225:mxn10000_usd555_38'
        ),
        (
            476::bigint,
            'santander',
            'fx_transfer:santander_to_icbc_6689_usd:20260216_20260225:mxn10000_usd555_38'
        )
)
UPDATE pams_cashflow_fact cf
SET
    cashflow_type = 'internal_transfer',
    category = U&'\5185\90E8\5212\8F6C',
    subcategory = U&'\6362\6C47\8F6C\8D26',
    counter_account_code = m.counter_account_code,
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    transfer_group_id = m.transfer_group_id,
    classification_source = 'manual_pair',
    confidence = 0.9500,
    review_status = 'confirmed',
    raw_json = COALESCE(cf.raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pair_058',
        jsonb_build_object(
            'confirmed_at', '2026-06-22',
            'confirmed_by', 'user',
            'rule', 'user confirmed Santander MXN outbound was transferred to ICBC 6689 USD',
            'paired_cashflow_ids', jsonb_build_array(428, 476),
            'mxn_out', 10000,
            'usd_in', 555.38,
            'target_account', 'icbc_6689_usd'
        )
    ),
    updated_at = NOW()
FROM matched_cashflows m
WHERE cf.cashflow_id = m.cashflow_id;

UPDATE pams_bank_statement_rows
SET
    category = U&'\5185\90E8\5212\8F6C',
    subcategory = U&'\6362\6C47\8F6C\8D26',
    auto_classified = TRUE,
    raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pair_058',
        'santander_to_icbc_6689_usd'
    )
WHERE id IN (773, 814)
   OR (
       account_code = 'santander'
       AND transaction_date = DATE '2026-02-16'
       AND amount = -10000.0000
       AND currency = 'MXN'
       AND summary ILIKE '%Huangzhanfeng%'
   )
   OR (
       account_code = 'icbc_6689_usd'
       AND transaction_date = DATE '2026-02-25'
       AND amount = 555.3800
       AND currency = 'USD'
   );

COMMIT;
