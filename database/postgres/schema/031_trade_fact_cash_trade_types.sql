-- Add explicit cash movement trade types to the investment trade fact contract.
-- Deposits and withdrawals affect cash, but must not affect position quantity,
-- cost basis, or realized trade PnL.

DO $$
BEGIN
    IF to_regclass('public.pams_investment_trade_fact') IS NOT NULL THEN
        ALTER TABLE public.pams_investment_trade_fact
            DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_type;

        ALTER TABLE public.pams_investment_trade_fact
            ADD CONSTRAINT ck_pams_investment_trade_fact_type CHECK (
                trade_type IN (
                    'buy',
                    'sell',
                    'deposit',
                    'withdraw',
                    'fund_subscription',
                    'fund_redemption',
                    'dividend',
                    'interest',
                    'coupon',
                    'fee',
                    'tax',
                    'corporate_action',
                    'split',
                    'transfer_in_kind',
                    'transfer_out_kind',
                    'position_adjustment',
                    'unknown'
                )
            );
    END IF;
END $$;

UPDATE public.pams_investment_trade_fact
SET trade_type = CASE
        WHEN LOWER(COALESCE(side, '')) IN ('deposit', 'cash_deposit', 'transfer_in') THEN 'deposit'
        WHEN LOWER(COALESCE(side, '')) IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN 'withdraw'
        ELSE trade_type
    END,
    side = CASE
        WHEN LOWER(COALESCE(side, '')) IN ('deposit', 'cash_deposit', 'transfer_in') THEN 'deposit'
        WHEN LOWER(COALESCE(side, '')) IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN 'withdraw'
        ELSE side
    END,
    net_cash = COALESCE(
        net_cash,
        CASE
            WHEN LOWER(COALESCE(side, '')) IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out')
                THEN -ABS(COALESCE(gross_amount, quantity, 0))
            ELSE ABS(COALESCE(gross_amount, quantity, 0))
        END
    ),
    net_cash_cny = COALESCE(
        net_cash_cny,
        CASE
            WHEN LOWER(COALESCE(side, '')) IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out')
                THEN -ABS(COALESCE(gross_amount, quantity, 0)) * COALESCE(fx_rate_to_cny, 1)
            ELSE ABS(COALESCE(gross_amount, quantity, 0)) * COALESCE(fx_rate_to_cny, 1)
        END
    ),
    affects_position = FALSE,
    affects_cost_basis = FALSE,
    affects_realized_pnl = FALSE,
    affects_cash = TRUE,
    updated_at = NOW()
WHERE trade_type = 'unknown'
  AND LOWER(COALESCE(side, '')) IN (
      'deposit',
      'cash_deposit',
      'transfer_in',
      'withdraw',
      'withdrawal',
      'cash_withdrawal',
      'transfer_out'
  );

UPDATE public.pams_investment_trade_fact
SET affects_position = FALSE,
    affects_cost_basis = FALSE,
    affects_realized_pnl = FALSE,
    affects_cash = FALSE,
    net_cash = NULL,
    net_cash_cny = NULL,
    updated_at = NOW()
WHERE trade_type = 'unknown'
  AND LOWER(COALESCE(side, '')) IN ('cancel', 'canceled', 'cancelled', 'void');
