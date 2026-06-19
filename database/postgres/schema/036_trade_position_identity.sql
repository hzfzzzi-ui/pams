-- Extend trade review with position-identity and identity-transition tracking.
-- These fields are user annotations and behavior-audit metadata; they do not
-- change imported trade facts, amounts, quantities, costs, or source hashes.

ALTER TABLE public.pams_investment_trade_fact
    ADD COLUMN IF NOT EXISTS initial_position_identity VARCHAR(80),
    ADD COLUMN IF NOT EXISTS current_position_identity VARCHAR(80),
    ADD COLUMN IF NOT EXISTS identity_confidence VARCHAR(40),
    ADD COLUMN IF NOT EXISTS intended_holding_period VARCHAR(120),
    ADD COLUMN IF NOT EXISTS buy_logic TEXT,
    ADD COLUMN IF NOT EXISTS add_condition TEXT,
    ADD COLUMN IF NOT EXISTS reduce_condition TEXT,
    ADD COLUMN IF NOT EXISTS sell_condition TEXT,
    ADD COLUMN IF NOT EXISTS max_acceptable_loss_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS max_position_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS next_review_date DATE,
    ADD COLUMN IF NOT EXISTS sell_rule_type VARCHAR(80);

ALTER TABLE public.pams_investment_trade_fact
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_buy_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_sell_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_review_tags,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_initial_identity,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_current_identity,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_identity_confidence,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_sell_rule_type;

-- Re-map older review reason enums to the slimmer reason system requested for
-- attribution. Subjective/emotional labels move to review_tags instead of
-- remaining primary reasons.
UPDATE public.pams_investment_trade_fact
SET buy_reason = CASE buy_reason
    WHEN 'plan_dca' THEN 'plan_execution'
    WHEN 'allocation' THEN 'allocation_adjustment'
    WHEN 'value_pullback' THEN 'fundamental_valuation'
    WHEN 'thesis' THEN 'fundamental_valuation'
    WHEN 'trend' THEN 'trend_signal'
    WHEN 'event' THEN 'event_driven'
    WHEN 'cash_defense' THEN 'cash_management'
    WHEN 'arbitrage' THEN 'arbitrage_structure'
    WHEN 'product_replacement' THEN 'allocation_adjustment'
    WHEN 'subjective_bullish' THEN 'uncertain_review'
    WHEN 'emotional_trade' THEN 'uncertain_review'
    ELSE buy_reason
END
WHERE buy_reason IS NOT NULL;

UPDATE public.pams_investment_trade_fact
SET sell_reason = CASE sell_reason
    WHEN 'take_profit' THEN 'planned_take_profit'
    WHEN 'stop_loss' THEN 'risk_control_stop_loss'
    WHEN 'thesis_invalidated' THEN 'thesis_invalidated'
    WHEN 'risk_rebalance' THEN 'risk_reduction_rebalance'
    WHEN 'product_replacement' THEN 'replacement_better_asset'
    WHEN 'liquidity_need' THEN 'liquidity_need'
    WHEN 'event_done' THEN 'event_completed'
    WHEN 'underperform' THEN 'thesis_invalidated'
    WHEN 'subjective_exit' THEN 'planned_take_profit'
    WHEN 'emotional_sell' THEN 'risk_control_stop_loss'
    WHEN 'correction' THEN 'correction'
    ELSE sell_reason
END
WHERE sell_reason IS NOT NULL;

UPDATE public.pams_investment_trade_fact
SET review_tags = COALESCE((
    SELECT ARRAY_AGG(DISTINCT mapped.tag ORDER BY mapped.tag)
    FROM (
        SELECT CASE raw_tag
            WHEN 'early_entry' THEN 'early_bottom'
            WHEN 'late_entry' THEN 'late_confirmation'
            WHEN 'under_sized' THEN 'undersized'
            WHEN 'ignored_stop' THEN 'ignored_stop_loss'
            ELSE raw_tag
        END AS tag
        FROM UNNEST(review_tags) AS raw_tag
    ) mapped
    WHERE mapped.tag = ANY (ARRAY[
        'chase_high', 'early_bottom', 'late_confirmation', 'rushed_buy',
        'insufficient_batch', 'sold_too_early', 'sold_too_late', 'oversized',
        'undersized', 'single_trade_too_heavy', 'theme_concentration',
        'ignored_stop_loss', 'ignored_take_profit',
        'plan_followed', 'early_execution', 'delayed_execution',
        'good_discipline', 'bad_discipline', 'fomo', 'panic', 'anchored_cost',
        'overconfidence', 'hesitation', 'impulsive_subjective',
        'loss_rationalization', 'profit_take_too_early',
        'data_issue', 'liquidity_slippage', 'operation_error',
        'external_need', 'account_restriction',
        'thesis_correct', 'thesis_wrong', 'right_reason_poor_execution',
        'right_result_weak_reason', 'lucky_result', 'timely_stop_loss',
        'delayed_stop_loss', 'correct_rebalance', 'wrong_rebalance',
        'identity_correct', 'identity_wrong', 'identity_change_reasonable',
        'identity_drift'
    ]::TEXT[])
), ARRAY[]::TEXT[])
WHERE review_tags IS NOT NULL;

ALTER TABLE public.pams_investment_trade_fact
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_buy_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_sell_reason,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_review_tags,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_initial_identity,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_current_identity,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_identity_confidence,
    DROP CONSTRAINT IF EXISTS ck_pams_investment_trade_fact_sell_rule_type;

ALTER TABLE public.pams_investment_trade_fact
    ADD CONSTRAINT ck_pams_investment_trade_fact_buy_reason CHECK (
        buy_reason IS NULL OR buy_reason IN (
            'plan_execution', 'allocation_adjustment', 'fundamental_valuation',
            'trend_signal', 'event_driven', 'arbitrage_structure',
            'trial_observation', 'cash_management', 'uncertain_review', 'other'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_sell_reason CHECK (
        sell_reason IS NULL OR sell_reason IN (
            'planned_take_profit', 'risk_control_stop_loss', 'thesis_invalidated',
            'trend_signal_exit', 'risk_reduction_rebalance', 'event_completed',
            'replacement_better_asset', 'liquidity_need', 'correction', 'other'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_review_tags CHECK (
        review_tags <@ ARRAY[
            'chase_high', 'early_bottom', 'late_confirmation', 'rushed_buy',
            'insufficient_batch', 'sold_too_early', 'sold_too_late', 'oversized',
            'undersized', 'single_trade_too_heavy', 'theme_concentration',
            'ignored_stop_loss', 'ignored_take_profit',
            'plan_followed', 'early_execution', 'delayed_execution',
            'good_discipline', 'bad_discipline', 'fomo', 'panic', 'anchored_cost',
            'overconfidence', 'hesitation', 'impulsive_subjective',
            'loss_rationalization', 'profit_take_too_early',
            'data_issue', 'liquidity_slippage', 'operation_error',
            'external_need', 'account_restriction',
            'thesis_correct', 'thesis_wrong', 'right_reason_poor_execution',
            'right_result_weak_reason', 'lucky_result', 'timely_stop_loss',
            'delayed_stop_loss', 'correct_rebalance', 'wrong_rebalance',
            'identity_correct', 'identity_wrong', 'identity_change_reasonable',
            'identity_drift'
        ]::TEXT[]
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_initial_identity CHECK (
        initial_position_identity IS NULL OR initial_position_identity IN (
            'allocation', 'value', 'trend', 'event', 'trial',
            'cash_management', 'uncertain'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_current_identity CHECK (
        current_position_identity IS NULL OR current_position_identity IN (
            'allocation', 'value', 'trend', 'event', 'trial',
            'cash_management', 'uncertain', 'closed'
        )
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_identity_confidence CHECK (
        identity_confidence IS NULL OR identity_confidence IN ('high', 'medium', 'low', 'unknown')
    ),
    ADD CONSTRAINT ck_pams_investment_trade_fact_sell_rule_type CHECK (
        sell_rule_type IS NULL OR sell_rule_type IN (
            'allocation_rebalance', 'value_thesis', 'trend_exit',
            'event_resolution', 'trial_review', 'cash_need',
            'uncertain_reclassify', 'closed', 'other'
        )
    );

CREATE TABLE IF NOT EXISTS public.pams_trade_identity_transition_event (
    event_id BIGSERIAL PRIMARY KEY,
    trade_id BIGINT NOT NULL REFERENCES public.pams_investment_trade_fact(trade_id) ON DELETE CASCADE,
    from_identity VARCHAR(80),
    to_identity VARCHAR(80) NOT NULL,
    transition_type VARCHAR(80) NOT NULL DEFAULT 'other',
    transition_triggers TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    evidence_note TEXT,
    new_sell_condition TEXT,
    new_max_position_pct NUMERIC(10,4),
    new_review_date DATE,
    transition_quality VARCHAR(80) NOT NULL DEFAULT 'needs_review',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_pams_trade_identity_transition_from CHECK (
        from_identity IS NULL OR from_identity IN (
            'allocation', 'value', 'trend', 'event', 'trial',
            'cash_management', 'uncertain', 'closed'
        )
    ),
    CONSTRAINT ck_pams_trade_identity_transition_to CHECK (
        to_identity IN (
            'allocation', 'value', 'trend', 'event', 'trial',
            'cash_management', 'uncertain', 'closed'
        )
    ),
    CONSTRAINT ck_pams_trade_identity_transition_type CHECK (
        transition_type IN (
            'planned_upgrade', 'planned_downgrade', 'research_reclassify',
            'risk_downgrade', 'post_stop_review', 'loss_rationalization',
            'position_out_of_control', 'data_correction', 'other'
        )
    ),
    CONSTRAINT ck_pams_trade_identity_transition_triggers CHECK (
        transition_triggers <@ ARRAY[
            'new_fundamental_evidence', 'valuation_change', 'trend_signal_change',
            'risk_budget_exceeded', 'position_too_large', 'theme_concentration',
            'event_realized', 'event_failed', 'price_down', 'price_up',
            'replacement_better_asset', 'research_added', 'emotion_influence',
            'data_correction'
        ]::TEXT[]
    ),
    CONSTRAINT ck_pams_trade_identity_transition_quality CHECK (
        transition_quality IN (
            'reasonable', 'needs_review', 'high_risk_drift',
            'suspected_stop_avoidance', 'data_correction'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_trade_fact_position_identity
    ON public.pams_investment_trade_fact (initial_position_identity, current_position_identity, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_trade_fact_next_review_date
    ON public.pams_investment_trade_fact (next_review_date, trade_date DESC)
    WHERE next_review_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trade_identity_transition_trade
    ON public.pams_trade_identity_transition_event (trade_id, created_at DESC);

COMMENT ON COLUMN public.pams_investment_trade_fact.initial_position_identity IS 'Initial user-declared lot/event position identity at buy time.';
COMMENT ON COLUMN public.pams_investment_trade_fact.current_position_identity IS 'Current user-declared position identity; changes must be recorded in pams_trade_identity_transition_event.';
COMMENT ON COLUMN public.pams_investment_trade_fact.identity_confidence IS 'User confidence in position identity: high, medium, low, unknown.';
COMMENT ON COLUMN public.pams_investment_trade_fact.intended_holding_period IS 'Planned holding period memo for the initial position identity.';
COMMENT ON COLUMN public.pams_investment_trade_fact.buy_logic IS 'Structured/free-form buy logic or uncertainty reason.';
COMMENT ON COLUMN public.pams_investment_trade_fact.add_condition IS 'Condition under which adding to the position is allowed.';
COMMENT ON COLUMN public.pams_investment_trade_fact.reduce_condition IS 'Condition under which reducing the position is required.';
COMMENT ON COLUMN public.pams_investment_trade_fact.sell_condition IS 'Identity-specific sell or invalidation rule.';
COMMENT ON COLUMN public.pams_investment_trade_fact.max_acceptable_loss_pct IS 'Maximum acceptable loss percentage for this trade/lot annotation.';
COMMENT ON COLUMN public.pams_investment_trade_fact.max_position_pct IS 'Maximum allowed portfolio weight percentage for this trade/lot annotation.';
COMMENT ON COLUMN public.pams_investment_trade_fact.next_review_date IS 'Next required review date for uncertain/trial positions.';
COMMENT ON COLUMN public.pams_investment_trade_fact.sell_rule_type IS 'Risk-control rule family implied by the current position identity.';
COMMENT ON TABLE public.pams_trade_identity_transition_event IS 'Audit trail for user-declared position identity transitions on investment trade facts.';
