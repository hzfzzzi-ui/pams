"""Write user trade reviews separately from immutable source facts."""
from psycopg2 import sql

FIELDS = frozenset({'buy_reason', 'sell_reason', 'review_tags', 'trade_intent', 'trade_plan',
    'review_note', 'review_due_date', 'initial_position_identity', 'current_position_identity',
    'identity_confidence', 'intended_holding_period', 'buy_logic', 'add_condition', 'reduce_condition',
    'sell_condition', 'max_acceptable_loss_pct', 'max_position_pct', 'next_review_date', 'sell_rule_type'})
STATUSES = frozenset({'auto', 'needs_review', 'reviewed', 'ignored'})


def _transition_quality(old, new, kind, triggers, evidence):
    triggers = set(triggers or [])
    if kind == 'data_correction' or 'data_correction' in triggers:
        return 'data_correction'
    if kind in {'loss_rationalization', 'position_out_of_control'}:
        return 'high_risk_drift'
    if old == 'trend' and new == 'value':
        return 'needs_review' if evidence and triggers & {'new_fundamental_evidence', 'valuation_change', 'research_added'} else 'suspected_stop_avoidance'
    if old == 'trial' and new in {'value', 'trend', 'allocation'}:
        return 'reasonable' if evidence and triggers else 'needs_review'
    if old == 'allocation' and new == 'trend':
        return 'high_risk_drift'
    return 'reasonable' if evidence and triggers else 'needs_review'


def write_review(cur, *, trade_id, changes, inferred_status, explicit_status,
                 transition_type=None, transition_triggers=None, transition_evidence=None):
    if cur.connection.autocommit or not set(changes) <= FIELDS or inferred_status not in STATUSES:
        raise ValueError('invalid review transaction or fields')
    cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE trade_id=%s FOR UPDATE', (trade_id,))
    if not cur.fetchone():
        raise LookupError('trade not found')
    cur.execute('SELECT * FROM investment_trade_review WHERE trade_id=%s FOR UPDATE', (trade_id,))
    previous = cur.fetchone()
    before = dict(previous) if previous else {}
    cur.execute('INSERT INTO investment_trade_review(trade_id) VALUES (%s) ON CONFLICT DO NOTHING', (trade_id,))
    values = dict(changes)
    if explicit_status or not previous:
        values['review_status'] = inferred_status
        values['reviewed_at'] = sql.SQL('now()') if inferred_status == 'reviewed' else None
    old_identity = before.get('current_position_identity')
    new_identity = changes.get('current_position_identity')
    if old_identity and new_identity and old_identity != new_identity:
        kind = transition_type or 'other'
        values.update(identity_transition_type=kind, identity_transition_triggers=transition_triggers or [],
            identity_transition_evidence=transition_evidence,
            identity_transition_quality=_transition_quality(old_identity, new_identity, kind, transition_triggers, transition_evidence),
            identity_transition_at=sql.SQL('now()'))
    assignments, params = [], []
    for name, value in values.items():
        if isinstance(value, sql.SQL):
            assignments.append(sql.SQL('{}={}').format(sql.Identifier(name), value))
        else:
            assignments.append(sql.SQL('{}=%s').format(sql.Identifier(name)))
            params.append(value)
    assignments.append(sql.SQL('updated_at=now()'))
    cur.execute(sql.SQL('UPDATE investment_trade_review SET {} WHERE trade_id=%s RETURNING *').format(
        sql.SQL(',').join(assignments)), [*params, trade_id])
    return dict(cur.fetchone())


def write_auto_review(cur, *, trade_id, updates):
    """Fill only empty annotations under lock; never overwrite a manual decision."""
    cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE trade_id=%s FOR UPDATE', (trade_id,))
    if not cur.fetchone():
        return False
    cur.execute('SELECT * FROM investment_trade_review WHERE trade_id=%s FOR UPDATE', (trade_id,))
    existing = cur.fetchone()
    before = dict(existing) if existing else {}
    status = before.get('review_status') or 'auto'
    if status in {'reviewed', 'ignored'}:
        return False
    allowed = {'buy_reason', 'sell_reason', 'review_tags', 'initial_position_identity',
               'current_position_identity', 'identity_confidence', 'sell_rule_type'}
    changes = {key: value for key, value in updates.items() if key in allowed and not before.get(key)}
    next_status = 'needs_review' if status == 'auto' and updates.get('review_status') == 'needs_review' else status
    if not changes and next_status == status:
        return False
    write_review(cur, trade_id=trade_id, changes=changes, inferred_status=next_status,
                 explicit_status=next_status != status)
    return True
