-- Copy the user-confirmed planned DCA review setup across all 易沪深300 buys.
-- Source template is trade_id=195, manually reviewed by the user on 2026-06-07.

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
    WHERE trade_id = 195
      AND account_code = 'ccb_yhs300_fund'
      AND symbol = '110020'
      AND side = 'buy'
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
            'manual_review_copy_yhs300_dca_20260607',
            jsonb_build_object(
                'source_trade_id', 195,
                'reason', 'planned recurring CSI 300 DCA buy',
                'confirmed_by', 'user',
                'applied_at', now()
            )
        ),
    updated_at = now()
FROM template t
WHERE f.account_code = 'ccb_yhs300_fund'
  AND f.symbol = '110020'
  AND f.side = 'buy'
  AND f.source_table = 'pams_investment_transactions';

COMMIT;
