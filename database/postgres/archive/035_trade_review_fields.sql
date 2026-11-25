-- Empty-database definition; no historical data migration.
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
        review_status IN ('auto', 'needs_review', 'reviewed', 'ignored', 'confirmed')
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
