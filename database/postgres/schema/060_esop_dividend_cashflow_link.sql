-- Link the Huawei ESOP cash dividend realized through the Huawei union bank
-- transfer to the investment dividend fact.  The ledger can still display the
-- bank transfer as income, but NAV bridge must not count the same cash as both
-- external cashflow and investment PnL.

BEGIN;

WITH esop_cashflow AS (
    SELECT
        cashflow_id,
        event_date,
        amount_cny,
        raw_json #>> '{statement_row,source_uid}' AS source_uid
    FROM pams_cashflow_fact
    WHERE event_date = DATE '2026-03-31'
      AND account_code = 'ccb_7332'
      AND amount_cny = 37120
),
esop_trade AS (
    SELECT
        trade_id,
        trade_date,
        net_cash_cny,
        raw_json #>> '{source_row,raw_json,source_uid}' AS source_uid
    FROM pams_investment_trade_fact
    WHERE trade_date = DATE '2026-03-31'
      AND account_code = 'huawei_stock'
      AND symbol = 'HUAWEI_ESOP'
      AND trade_type = 'dividend'
      AND ABS(COALESCE(net_cash_cny, gross_amount * fx_rate_to_cny, 0) - 37120) < 1
),
matched AS (
    SELECT t.trade_id, c.cashflow_id
    FROM esop_trade t
    JOIN esop_cashflow c
      ON c.event_date = t.trade_date
     AND ABS(c.amount_cny - t.net_cash_cny) < 1
     AND c.source_uid = t.source_uid
)
UPDATE pams_investment_trade_fact tx
SET cashflow_id = matched.cashflow_id,
    raw_json = COALESCE(tx.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'linked_cashflow_id', matched.cashflow_id,
            'cashflow_link_reason', 'user_confirmed_huawei_union_esop_dividend'
        ),
    updated_at = now()
FROM matched
WHERE tx.trade_id = matched.trade_id
  AND tx.cashflow_id IS DISTINCT FROM matched.cashflow_id;

WITH linked AS (
    SELECT cashflow_id
    FROM pams_investment_trade_fact
    WHERE account_code = 'huawei_stock'
      AND symbol = 'HUAWEI_ESOP'
      AND trade_type = 'dividend'
      AND cashflow_id IS NOT NULL
)
UPDATE pams_cashflow_fact cf
SET raw_json = COALESCE(cf.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'linked_investment_trade', 'HUAWEI_ESOP dividend',
            'cashflow_bridge_role', 'investment_realized_cash'
        ),
    updated_at = now()
FROM linked
WHERE cf.cashflow_id = linked.cashflow_id;

COMMIT;
