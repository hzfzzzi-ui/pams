-- Backfill user-confirmed buy fee for 嘉实中证稀土ETF联接A on 2026-06-08.
-- Keep net_cash unchanged. The investment ledger "tax/fee" total is recorded in fee_amount.

WITH target AS (
    SELECT
        f.trade_id,
        495.04::numeric AS fee_amount_new,
        '嘉实中证稀土ETF联接A 买入税费确认 495.04 元' AS note
    FROM public.pams_investment_trade_fact f
    WHERE f.trade_id = 61
      AND f.source_table = 'pams_investment_transactions'
      AND f.source_id = '184'
      AND f.symbol = '011035'
      AND f.name = '嘉实中证稀土ETF联接A'
      AND f.trade_type = 'buy'
      AND f.net_cash = -50000
)
UPDATE public.pams_investment_trade_fact f
SET
    fee_amount = t.fee_amount_new,
    tax_amount = 0,
    gross_amount = abs(COALESCE(f.net_cash, f.gross_amount, 0)) - t.fee_amount_new,
    price = CASE
        WHEN f.quantity IS NOT NULL AND f.quantity <> 0
            THEN (abs(COALESCE(f.net_cash, f.gross_amount, 0)) - t.fee_amount_new) / abs(f.quantity)
        ELSE f.price
    END,
    notes = CASE
        WHEN COALESCE(f.notes, '') LIKE '%manual_fee_backfill_20260608_jiashi_rare_earth%' THEN f.notes
        WHEN COALESCE(f.notes, '') = '' THEN 'manual_fee_backfill_20260608_jiashi_rare_earth: ' || t.note
        ELSE f.notes || E'\nmanual_fee_backfill_20260608_jiashi_rare_earth: ' || t.note
    END,
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_fee_backfill_20260608_jiashi_rare_earth',
            jsonb_build_object(
                'confirmed_by', 'user',
                'fee_amount', t.fee_amount_new,
                'tax_amount', 0,
                'net_cash_preserved', f.net_cash,
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM target t
WHERE f.trade_id = t.trade_id
  AND NOT (COALESCE(f.raw_json, '{}'::jsonb) ? 'manual_fee_backfill_20260608_jiashi_rare_earth');
