-- Optional, one review per trade; the source transaction remains independent.
CREATE TABLE public.investment_trade_review (
    trade_id BIGINT PRIMARY KEY REFERENCES public.pams_investment_trade_fact(trade_id) ON DELETE RESTRICT,
    buy_reason VARCHAR(80), sell_reason VARCHAR(80),
    trade_intent TEXT, trade_plan TEXT, buy_logic TEXT,
    add_condition TEXT, reduce_condition TEXT, sell_condition TEXT,
    intended_holding_period VARCHAR(120), max_acceptable_loss_pct NUMERIC(10,4), max_position_pct NUMERIC(10,4),
    review_tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[], review_note TEXT,
    review_due_date DATE, next_review_date DATE, reviewed_at TIMESTAMPTZ,
    sell_rule_type VARCHAR(80), initial_position_identity VARCHAR(80), current_position_identity VARCHAR(80),
    identity_confidence VARCHAR(40), identity_transition_type VARCHAR(80), identity_transition_triggers TEXT[],
    identity_transition_evidence TEXT, identity_transition_quality VARCHAR(80), identity_transition_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
