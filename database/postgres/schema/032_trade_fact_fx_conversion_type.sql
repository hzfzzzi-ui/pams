-- Classify currency-pair executions as FX conversion instead of buy/sell.
-- FX conversions move cash between currencies and must not inflate security
-- buy/sell turnover, position quantity, cost basis, or realized security PnL.

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
                    'fx_conversion',
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
SET trade_type = 'fx_conversion',
    side = 'fx_conversion',
    affects_position = FALSE,
    affects_cost_basis = FALSE,
    affects_realized_pnl = FALSE,
    affects_cash = TRUE,
    updated_at = NOW()
WHERE trade_type IN ('buy', 'sell')
  AND (
      UPPER(COALESCE(symbol, '')) ~ '^[A-Z]{3}[./][A-Z]{3}$'
      OR UPPER(COALESCE(name, '')) ~ '^[A-Z]{3}[./][A-Z]{3}$'
  );
