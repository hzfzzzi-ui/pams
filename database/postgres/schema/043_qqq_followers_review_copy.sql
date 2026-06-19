-- Copy user-confirmed QQQ follower trade review annotations across the four
-- Nasdaq QDII feeder funds. Source templates are the manually reviewed
-- Huatai-PineBridge Nasdaq 100 feeder trades: buy trade_id=100, sell trade_id=161.

BEGIN;

WITH template AS (
    SELECT
        buy_reason,
        sell_reason,
        review_tags,
        review_note,
        review_due_date,
        initial_position_identity,
        current_position_identity,
        identity_confidence,
        intended_holding_period,
        buy_logic,
        add_condition,
        reduce_condition,
        sell_condition,
        max_acceptable_loss_pct,
        max_position_pct,
        next_review_date,
        sell_rule_type
    FROM public.pams_investment_trade_fact
    WHERE trade_id = 100
      AND trade_type = 'buy'
      AND buy_reason IS NOT NULL
)
UPDATE public.pams_investment_trade_fact f
SET
    buy_reason = t.buy_reason,
    sell_reason = t.sell_reason,
    review_tags = COALESCE(t.review_tags, ARRAY[]::text[]),
    review_note = t.review_note,
    review_due_date = t.review_due_date,
    initial_position_identity = t.initial_position_identity,
    current_position_identity = t.current_position_identity,
    identity_confidence = t.identity_confidence,
    intended_holding_period = t.intended_holding_period,
    buy_logic = t.buy_logic,
    add_condition = t.add_condition,
    reduce_condition = t.reduce_condition,
    sell_condition = t.sell_condition,
    max_acceptable_loss_pct = t.max_acceptable_loss_pct,
    max_position_pct = t.max_position_pct,
    next_review_date = t.next_review_date,
    sell_rule_type = t.sell_rule_type,
    review_status = 'reviewed',
    reviewed_at = COALESCE(f.reviewed_at, now()),
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_review_copy_qqq_followers_20260608',
            jsonb_build_object(
                'source_trade_id', 100,
                'direction', 'buy',
                'reason', 'same QQQ follower buy review as Huatai-PineBridge Nasdaq 100 feeder',
                'confirmed_by', 'user',
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM template t
WHERE f.trade_id = ANY(ARRAY[
    41, 42, 43, 44,
    54, 55, 57, 58,
    63, 64, 65, 67,
    69, 70, 71, 75,
    77, 78, 79, 81,
    85, 87, 88, 90,
    92, 94, 95, 97,
    100, 102, 103, 104,
    108, 109, 110, 112,
    118, 119, 120, 121,
    124, 125, 127, 129,
    131, 132, 133, 134,
    135, 138, 140, 142,
    144, 145, 146, 147, 148, 149,
    152, 153, 154, 155
])
  AND f.trade_type = 'buy'
  AND NOT (COALESCE(f.raw_json, '{}'::jsonb) ? 'manual_review_copy_qqq_followers_20260608');

WITH template AS (
    SELECT
        buy_reason,
        sell_reason,
        review_tags,
        review_note,
        review_due_date,
        initial_position_identity,
        current_position_identity,
        identity_confidence,
        intended_holding_period,
        buy_logic,
        add_condition,
        reduce_condition,
        sell_condition,
        max_acceptable_loss_pct,
        max_position_pct,
        next_review_date,
        sell_rule_type
    FROM public.pams_investment_trade_fact
    WHERE trade_id = 161
      AND trade_type = 'sell'
      AND sell_reason IS NOT NULL
)
UPDATE public.pams_investment_trade_fact f
SET
    buy_reason = t.buy_reason,
    sell_reason = t.sell_reason,
    review_tags = COALESCE(t.review_tags, ARRAY[]::text[]),
    review_note = t.review_note,
    review_due_date = t.review_due_date,
    initial_position_identity = t.initial_position_identity,
    current_position_identity = t.current_position_identity,
    identity_confidence = t.identity_confidence,
    intended_holding_period = t.intended_holding_period,
    buy_logic = t.buy_logic,
    add_condition = t.add_condition,
    reduce_condition = t.reduce_condition,
    sell_condition = t.sell_condition,
    max_acceptable_loss_pct = t.max_acceptable_loss_pct,
    max_position_pct = t.max_position_pct,
    next_review_date = t.next_review_date,
    sell_rule_type = t.sell_rule_type,
    review_status = 'reviewed',
    reviewed_at = COALESCE(f.reviewed_at, now()),
    raw_json = COALESCE(f.raw_json, '{}'::jsonb)
        || jsonb_build_object(
            'manual_review_copy_qqq_followers_20260608',
            jsonb_build_object(
                'source_trade_id', 161,
                'direction', 'sell',
                'reason', 'same QQQ follower sell review as Huatai-PineBridge Nasdaq 100 feeder',
                'confirmed_by', 'user',
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM template t
WHERE f.trade_id = ANY(ARRAY[161, 162, 163, 164])
  AND f.trade_type = 'sell'
  AND NOT (COALESCE(f.raw_json, '{}'::jsonb) ? 'manual_review_copy_qqq_followers_20260608');

INSERT INTO public.pams_trade_identity_transition_event (
    trade_id,
    from_identity,
    to_identity,
    transition_type,
    transition_triggers,
    evidence_note,
    new_sell_condition,
    new_max_position_pct,
    new_review_date,
    transition_quality
)
SELECT
    target.trade_id,
    'allocation',
    'closed',
    'other',
    ARRAY['replacement_better_asset']::text[],
    'manual_review_copy_qqq_followers_20260608: user confirmed same QQQ follower sell review as Huatai-PineBridge Nasdaq 100 feeder',
    '仓位已关闭。',
    NULL::numeric,
    NULL::date,
    'reasonable'
FROM (VALUES (161), (162), (163), (164)) AS target(trade_id)
WHERE NOT EXISTS (
    SELECT 1
    FROM public.pams_trade_identity_transition_event e
    WHERE e.trade_id = target.trade_id
      AND e.from_identity = 'allocation'
      AND e.to_identity = 'closed'
      AND e.transition_triggers @> ARRAY['replacement_better_asset']::text[]
      AND e.evidence_note LIKE '%manual_review_copy_qqq_followers_20260608%'
);

COMMIT;
