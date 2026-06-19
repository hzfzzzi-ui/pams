-- Backfill confirmed fees and inferred units for CCB 易沪深300 trades.
-- User confirmed on 2026-06-07 that every 易沪深300 transaction fee/tax total is CNY 11.86.
-- Quantity is inferred from the existing holdings snapshot unit deltas so the trade facts
-- continue to reconcile to the current holdings quantity.

BEGIN;

WITH daily_quantity AS (
    SELECT
        as_of,
        quantity
    FROM public.pams_holdings_snapshot
    WHERE account_code = 'ccb_yhs300_fund'
      AND symbol = '110020'
      AND quantity IS NOT NULL
),
quantity_deltas AS (
    SELECT
        as_of,
        quantity,
        quantity - LAG(quantity) OVER (ORDER BY as_of) AS delta_quantity
    FROM daily_quantity
),
trade_quantity AS (
    SELECT DISTINCT ON (f.trade_id)
        f.trade_id,
        CASE
            WHEN q.delta_quantity IS NULL THEN q.quantity
            ELSE q.delta_quantity
        END AS corrected_quantity
    FROM public.pams_investment_trade_fact f
    JOIN quantity_deltas q
      ON q.as_of = f.trade_date
    WHERE f.account_code = 'ccb_yhs300_fund'
      AND f.symbol = '110020'
      AND f.side = 'buy'
      AND f.source_table = 'pams_investment_transactions'
      AND (q.delta_quantity IS NULL OR ABS(q.delta_quantity) > 0.000001)
    ORDER BY f.trade_id, q.as_of
),
corrections AS (
    SELECT
        f.trade_id,
        tq.corrected_quantity,
        ROUND((ABS(f.net_cash) - 11.86)::numeric, 10) AS corrected_gross_amount,
        ROUND(((ABS(f.net_cash) - 11.86) / NULLIF(tq.corrected_quantity, 0))::numeric, 10) AS corrected_price
    FROM public.pams_investment_trade_fact f
    JOIN trade_quantity tq
      ON tq.trade_id = f.trade_id
    WHERE f.account_code = 'ccb_yhs300_fund'
      AND f.symbol = '110020'
      AND f.side = 'buy'
      AND f.source_table = 'pams_investment_transactions'
      AND tq.corrected_quantity IS NOT NULL
      AND tq.corrected_quantity > 0
      AND ABS(f.net_cash) >= 11.86
)
UPDATE public.pams_investment_trade_fact f
SET
    quantity = c.corrected_quantity,
    price = c.corrected_price,
    gross_amount = c.corrected_gross_amount,
    fee_amount = 11.86,
    tax_amount = 0,
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_correction_yhs300_20260607',
            jsonb_build_object(
                'confirmed_fee_total_cny', 11.86,
                'quantity_source', 'pams_holdings_snapshot unit delta',
                'gross_amount_rule', 'ABS(net_cash) - confirmed_fee_total',
                'confirmed_by', 'user',
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM corrections c
WHERE f.trade_id = c.trade_id;

COMMIT;
