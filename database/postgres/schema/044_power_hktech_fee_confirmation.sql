-- Confirm user-provided fund fee amounts on 2026-06-08.
-- Keep net_cash unchanged:
-- - Guangfa CSI Electric Utilities ETF Feeder C buy fee is 0.
-- - Guangfa Hang Seng HK Stock Connect Tech ETF Feeder A buy fee is fixed at CNY 2.50.

WITH target AS (
    SELECT
        f.trade_id,
        CASE
            WHEN f.trade_id = 209 THEN 0::numeric
            WHEN f.trade_id = 212 THEN 2.50::numeric
        END AS fee_amount_new,
        CASE
            WHEN f.trade_id = 209 THEN '广发中证电力公用事业ETF联接C 买入手续费确认 0'
            WHEN f.trade_id = 212 THEN '广发恒生港股通科技主题ETF发起式联接A 买入手续费确认 2.50 元'
        END AS note
    FROM public.pams_investment_trade_fact f
    WHERE f.trade_id IN (209, 212)
      AND f.source_table = 'pams_investment_transactions'
      AND f.trade_type = 'buy'
      AND (
        (f.trade_id = 209 AND f.symbol = '016186')
        OR (f.trade_id = 212 AND f.symbol = '025197')
      )
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
        WHEN COALESCE(f.notes, '') LIKE '%manual_fee_confirmation_20260608_power_hktech%' THEN f.notes
        WHEN COALESCE(f.notes, '') = '' THEN 'manual_fee_confirmation_20260608_power_hktech: ' || t.note
        ELSE f.notes || E'\nmanual_fee_confirmation_20260608_power_hktech: ' || t.note
    END,
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_fee_confirmation_20260608_power_hktech',
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
  AND NOT (COALESCE(f.raw_json, '{}'::jsonb) ? 'manual_fee_confirmation_20260608_power_hktech');

UPDATE public.pams_investment_trade_fact
SET
    notes = btrim(replace(
        replace(
            COALESCE(notes, ''),
            E'\nmanual_fee_backfill_20260607_fund_rules: 广发中证电力公用事业ETF联接C 买入手续费确认 0',
            ''
        ),
        E'\nmanual_fee_backfill_20260607_fund_rules: 广发恒生港股通科技主题ETF发起式联接A 买入手续费 0.05%',
        ''
    )),
    updated_at = now()
WHERE trade_id IN (209, 212)
  AND notes LIKE '%manual_fee_backfill_20260607_fund_rules:%';
