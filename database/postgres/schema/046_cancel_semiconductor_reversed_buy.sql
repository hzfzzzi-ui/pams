-- Neutralize the user-confirmed cancelled subscription pair for 020839 on 2026-01-22.
-- The source rows are retained for audit, but neither row should affect trade, cash,
-- position, cost basis, return attribution, or review backlog calculations.

WITH target AS (
    SELECT
        f.trade_id,
        CASE WHEN f.trade_id = 59 THEN 60 ELSE 59 END AS paired_trade_id,
        f.source_id,
        f.source_key,
        f.trade_type AS original_trade_type,
        f.side AS original_side,
        f.quantity AS original_quantity,
        f.price AS original_price,
        f.gross_amount AS original_gross_amount,
        f.fee_amount AS original_fee_amount,
        f.tax_amount AS original_tax_amount,
        f.net_cash AS original_net_cash,
        f.net_cash_cny AS original_net_cash_cny
    FROM public.pams_investment_trade_fact f
    WHERE f.trade_id IN (59, 60)
      AND f.source_table = 'pams_investment_transactions'
      AND f.symbol = '020839'
      AND f.trade_date = DATE '2026-01-22'
      AND f.source_id IN ('182', '183')
      AND EXISTS (
          SELECT 1
          FROM public.pams_investment_trade_fact buy_row
          WHERE buy_row.trade_id = 59
            AND buy_row.source_table = 'pams_investment_transactions'
            AND buy_row.source_id = '182'
            AND buy_row.source_key = 'ccb_bank_statement:ccb_fund_row_950'
            AND buy_row.symbol = '020839'
            AND buy_row.trade_date = DATE '2026-01-22'
            AND buy_row.trade_type = 'buy'
            AND buy_row.side = 'buy'
            AND buy_row.gross_amount = 30000
            AND buy_row.net_cash = -30000
      )
      AND EXISTS (
          SELECT 1
          FROM public.pams_investment_trade_fact cancel_row
          WHERE cancel_row.trade_id = 60
            AND cancel_row.source_table = 'pams_investment_transactions'
            AND cancel_row.source_id = '183'
            AND cancel_row.source_key = 'ccb_bank_statement:ccb_fund_row_951'
            AND cancel_row.symbol = '020839'
            AND cancel_row.trade_date = DATE '2026-01-22'
            AND cancel_row.trade_type = 'unknown'
            AND cancel_row.side = 'cancel'
            AND cancel_row.gross_amount = 30000
      )
      AND NOT (COALESCE(f.raw_json, '{}'::jsonb) ? 'manual_correction_20260608_semiconductor_cancel_pair')
)
UPDATE public.pams_investment_trade_fact f
SET
    trade_type = 'unknown',
    side = 'cancel',
    quantity = NULL,
    price = NULL,
    gross_amount = NULL,
    fee_amount = 0,
    tax_amount = 0,
    net_cash = NULL,
    net_cash_cny = NULL,
    affects_position = FALSE,
    affects_cost_basis = FALSE,
    affects_realized_pnl = FALSE,
    affects_cash = FALSE,
    realized_pnl = NULL,
    realized_pnl_cny = NULL,
    review_status = 'ignored',
    notes = CASE
        WHEN COALESCE(f.notes, '') LIKE '%manual_correction_20260608_semiconductor_cancel_pair%' THEN f.notes
        WHEN COALESCE(f.notes, '') = '' THEN 'manual_correction_20260608_semiconductor_cancel_pair: user confirmed the 2026-01-22 020839 subscription was cancelled and was not a real buy.'
        ELSE f.notes || E'\nmanual_correction_20260608_semiconductor_cancel_pair: user confirmed the 2026-01-22 020839 subscription was cancelled and was not a real buy.'
    END,
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_correction_20260608_semiconductor_cancel_pair',
            jsonb_build_object(
                'confirmed_by', 'user',
                'correction', 'cancelled_subscription_pair',
                'paired_trade_id', t.paired_trade_id,
                'original_trade_type', t.original_trade_type,
                'original_side', t.original_side,
                'original_quantity', t.original_quantity,
                'original_price', t.original_price,
                'original_gross_amount', t.original_gross_amount,
                'original_fee_amount', t.original_fee_amount,
                'original_tax_amount', t.original_tax_amount,
                'original_net_cash', t.original_net_cash,
                'original_net_cash_cny', t.original_net_cash_cny,
                'source_id_preserved', t.source_id,
                'source_key_preserved', t.source_key,
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM target t
WHERE f.trade_id = t.trade_id;
