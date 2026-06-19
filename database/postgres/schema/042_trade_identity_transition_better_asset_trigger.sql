-- Add "better asset switch" as an allowed identity-transition trigger.

ALTER TABLE public.pams_trade_identity_transition_event
    DROP CONSTRAINT IF EXISTS ck_pams_trade_identity_transition_triggers,
    ADD CONSTRAINT ck_pams_trade_identity_transition_triggers CHECK (
        transition_triggers <@ ARRAY[
            'new_fundamental_evidence', 'valuation_change', 'trend_signal_change',
            'risk_budget_exceeded', 'position_too_large', 'theme_concentration',
            'event_realized', 'event_failed', 'price_down', 'price_up',
            'replacement_better_asset', 'research_added', 'emotion_influence',
            'data_correction'
        ]::TEXT[]
    );
