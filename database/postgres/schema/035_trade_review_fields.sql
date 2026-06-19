-- Add manual trade-review fields directly to the investment trade fact.
-- These fields are user annotations and must not change imported trade facts.

ALTER TABLE public.pams_investment_trade_fact
    ADD COLUMN IF NOT EXISTS buy_reason VARCHAR(80),
    ADD COLUMN IF NOT EXISTS sell_reason VARCHAR(80),
    ADD COLUMN IF NOT EXISTS review_tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS trade_intent TEXT,
    ADD COLUMN IF NOT EXISTS trade_plan TEXT,
    ADD COLUMN IF NOT EXISTS review_note TEXT,
    ADD COLUMN IF NOT EXISTS review_due_date DATE,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;

UPDATE public.pams_investment_trade_fact
SET review_status = 'auto'
WHERE review_status IS NULL
   OR review_status NOT IN ('auto', 'needs_review', 'reviewed', 'ignored');

UPDATE public.pams_investment_trade_fact
SET buy_reason = CASE buy_reason
    WHEN 'rebalance' THEN 'allocation'
    WHEN 'core_allocation' THEN 'allocation'
    WHEN 'fund_core_allocation' THEN 'allocation'
    WHEN 'index_beta' THEN 'allocation'
    WHEN 'fund_index_beta' THEN 'allocation'
    WHEN 'sector_theme' THEN 'thesis'
    WHEN 'fund_sector_theme' THEN 'thesis'
    WHEN 'manager_alpha' THEN 'thesis'
    WHEN 'fund_manager_alpha' THEN 'thesis'
    WHEN 'valuation' THEN 'value_pullback'
    WHEN 'pullback' THEN 'value_pullback'
    WHEN 'average_down' THEN 'value_pullback'
    WHEN 'fund_drawdown_add' THEN 'value_pullback'
    WHEN 'fundamental' THEN 'thesis'
    WHEN 'momentum' THEN 'trend'
    WHEN 'income' THEN 'cash_defense'
    WHEN 'cash_management' THEN 'cash_defense'
    WHEN 'hedge' THEN 'cash_defense'
    WHEN 'fund_cash_parking' THEN 'cash_defense'
    WHEN 'dca' THEN 'plan_dca'
    WHEN 'fund_dca' THEN 'plan_dca'
    WHEN 'discount_arbitrage' THEN 'arbitrage'
    WHEN 'fund_discount_arbitrage' THEN 'arbitrage'
    WHEN 'fund_replacement' THEN 'product_replacement'
    ELSE buy_reason
END
WHERE buy_reason IS NOT NULL;

UPDATE public.pams_investment_trade_fact
SET sell_reason = CASE sell_reason
    WHEN 'fund_take_profit' THEN 'take_profit'
    WHEN 'fund_stop_loss' THEN 'stop_loss'
    WHEN 'risk_reduce' THEN 'risk_rebalance'
    WHEN 'rebalance_trim' THEN 'risk_rebalance'
    WHEN 'fund_rebalance_trim' THEN 'risk_rebalance'
    WHEN 'fund_risk_reduce' THEN 'risk_rebalance'
    WHEN 'switch' THEN 'product_replacement'
    WHEN 'fund_switch' THEN 'product_replacement'
    WHEN 'underperform_benchmark' THEN 'underperform'
    WHEN 'fund_underperform_benchmark' THEN 'underperform'
    WHEN 'strategy_change' THEN 'thesis_invalidated'
    WHEN 'fund_manager_change' THEN 'thesis_invalidated'
    WHEN 'fund_thesis_invalidated' THEN 'thesis_invalidated'
    WHEN 'discount_arbitrage_done' THEN 'event_done'
    WHEN 'fund_discount_done' THEN 'event_done'
    WHEN 'fund_liquidity_need' THEN 'liquidity_need'
    WHEN 'data_or_liquidity' THEN 'liquidity_need'
    WHEN 'discipline' THEN 'risk_rebalance'
    ELSE sell_reason
END
WHERE sell_reason IS NOT NULL;

ALTER TABLE public.pams_investment_trade_fact
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_buy_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_sell_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_review_tags,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_review_status;

ALTER TABLE public.pams_investment_trade_fact
    ADD CONSTRAINT ck_pams_investment_trade_fact_buy_reason CHECK (
        buy_reason IS NULL OR buy_reason IN (
            'plan_dca',
            'allocation',
            'value_pullback',
            'thesis',
            'trend',
            'event',
            'cash_defense',
            'arbitrage',
            'product_replacement',
            'subjective_bullish',
            'emotional_trade',
            'other'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_sell_reason CHECK (
        sell_reason IS NULL OR sell_reason IN (
            'take_profit',
            'stop_loss',
            'thesis_invalidated',
            'risk_rebalance',
            'product_replacement',
            'liquidity_need',
            'event_done',
            'underperform',
            'subjective_exit',
            'emotional_sell',
            'correction',
            'other'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_review_tags CHECK (
        review_tags <@ ARRAY[
            'chase_high',
            'early_entry',
            'late_entry',
            'oversized',
            'under_sized',
            'ignored_stop',
            'sold_too_early',
            'sold_too_late',
            'good_discipline',
            'bad_discipline',
            'data_issue',
            'external_need'
        ]::TEXT[]
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_review_status CHECK (
        review_status IN ('auto', 'needs_review', 'reviewed', 'ignored')
    );

CREATE INDEX IF NOT EXISTS idx_trade_fact_review_due
    ON public.pams_investment_trade_fact (review_due_date, trade_date DESC)
    WHERE review_due_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trade_fact_review_status_date
    ON public.pams_investment_trade_fact (review_status, trade_date DESC);

COMMENT ON COLUMN public.pams_investment_trade_fact.buy_reason IS 'Manual primary buy-reason enum for trade review attribution.';
COMMENT ON COLUMN public.pams_investment_trade_fact.sell_reason IS 'Manual primary sell-reason enum for trade review attribution.';
COMMENT ON COLUMN public.pams_investment_trade_fact.review_tags IS 'Manual auxiliary review tags for attribution and behavior diagnostics.';
COMMENT ON COLUMN public.pams_investment_trade_fact.trade_intent IS 'Manual free-form trade memo; does not alter trade facts.';
COMMENT ON COLUMN public.pams_investment_trade_fact.trade_plan IS 'Manual free-form plan memo, including targets, stops, invalidation or observation notes.';
COMMENT ON COLUMN public.pams_investment_trade_fact.review_note IS 'Manual post-trade review notes.';
COMMENT ON COLUMN public.pams_investment_trade_fact.review_due_date IS 'Optional next review date for this trade.';
COMMENT ON COLUMN public.pams_investment_trade_fact.reviewed_at IS 'Timestamp when manual review was last marked reviewed.';
