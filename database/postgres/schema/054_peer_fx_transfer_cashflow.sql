-- Reconcile peer FX transfer candidates that require two-sided evidence.
-- Source: user confirmation on 2026-06-21.
--
-- Rule:
-- - A personal peer transfer can be internal only when an opposite-currency
--   receipt/payment is present within a short date window and the implied FX
--   amount is close.
-- - Name-only matches remain reviewable operating cashflows.

BEGIN;

WITH peer_pair (
    cashflow_id,
    counter_account_code,
    transfer_group_id
) AS (
    VALUES
        (
            429::bigint,
            'alipay',
            'peer_fx_transfer:santander_to_alipay:20260216_20260217:mxn40000_cny16100'
        ),
        (
            446::bigint,
            'santander',
            'peer_fx_transfer:santander_to_alipay:20260216_20260217:mxn40000_cny16100'
        )
)
UPDATE pams_cashflow_fact cf
SET cashflow_type = 'internal_transfer',
    category = U&'\5185\90E8\5212\8F6C',
    subcategory = U&'\6362\6C47\8F6C\8D26',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    counter_account_code = p.counter_account_code,
    transfer_group_id = p.transfer_group_id,
    classification_source = 'manual_pair',
    confidence = 0.9500,
    review_status = 'confirmed',
    raw_json = COALESCE(cf.raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pair',
        jsonb_build_object(
            'confirmed_at', '2026-06-21',
            'rule', 'opposite currency cashflows with close implied FX amount',
            'paired_cashflow_ids', jsonb_build_array(429, 446),
            'mxn_out', 40000,
            'cny_in', 16100,
            'implied_rate', 0.4025
        )
    ),
    updated_at = now()
FROM peer_pair p
WHERE cf.cashflow_id = p.cashflow_id
  AND cf.source_uid IN (
      '55ba77e0899c6ec65519d15ee5b7426b6728dde69e1e8ccf4f1b156346100f9b',
      '2ca2492e2926491741519a94176c9327a5cb5be577d2dfdcaa9da0acab645c02'
  );

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
) VALUES (
    DATE '2026-01-08',
    DATE '2026-01-08',
    'santander',
    'ccb_7332',
    40000.0000,
    'MXN',
    0.38869475,
    15547.7900,
    'internal_transfer',
    U&'\5185\90E8\5212\8F6C',
    U&'\6362\6C47\8F6C\8D26',
    U&'\9EC4\661F\5B87',
    'TRANSFER FROM HUANG XINGYU',
    'User-confirmed Santander receipt paired with CCB Huang Xingyu CNY transfer',
    FALSE,
    FALSE,
    FALSE,
    TRUE,
    'peer_fx_transfer:ccb_7332_to_santander:20260108_20260109:cny15547_79_mxn40000',
    'manual_cashflow_correction',
    'peer_fx_transfer_20260108_santander_huangxingyu_40000_mxn',
    'peer_fx_transfer_20260108_santander_huangxingyu_40000_mxn',
    'c4483a8acc17009b70bd6ba0522cc37dc6be047e97c7f84ac68309b3abb00bca',
    jsonb_build_object(
        'manual_pair',
        jsonb_build_object(
            'confirmed_at', '2026-06-21',
            'confirmed_by', 'user',
            'rule', 'opposite currency cashflows with close implied FX amount',
            'paired_cashflow_ids', jsonb_build_array(96),
            'cny_out', 15547.79,
            'mxn_in', 40000,
            'implied_rate', 0.38869475,
            'source_note', 'User stated this was a 2026-01-08 transfer into Santander for 40,000 MXN'
        )
    ),
    'manual_pair',
    0.9500,
    'confirmed'
)
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
    raw_json = EXCLUDED.raw_json,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    updated_at = now();

UPDATE pams_cashflow_fact cf
SET cashflow_type = 'internal_transfer',
    category = U&'\5185\90E8\5212\8F6C',
    subcategory = U&'\6362\6C47\8F6C\8D26',
    affects_spending = FALSE,
    affects_nav = FALSE,
    affects_investment_capital = FALSE,
    is_internal_transfer = TRUE,
    counter_account_code = 'santander',
    transfer_group_id = 'peer_fx_transfer:ccb_7332_to_santander:20260108_20260109:cny15547_79_mxn40000',
    classification_source = 'manual_pair',
    confidence = 0.9500,
    review_status = 'confirmed',
    raw_json = COALESCE(cf.raw_json, '{}'::jsonb) || jsonb_build_object(
        'manual_pair',
        jsonb_build_object(
            'confirmed_at', '2026-06-21',
            'confirmed_by', 'user',
            'rule', 'opposite currency cashflows with close implied FX amount',
            'paired_source_id', 'peer_fx_transfer_20260108_santander_huangxingyu_40000_mxn',
            'cny_out', 15547.79,
            'mxn_in', 40000,
            'implied_rate', 0.38869475
        )
    ),
    updated_at = now()
WHERE cf.cashflow_id = 96
  AND cf.source_uid = '95bc65f59f57e81fb796fdf67a2b0c5a77d0356e31f753f16865c2cb23a538ed';

COMMIT;
