"""Manual pairing for cashflows explicitly classified as internal transfers."""
from calendar import monthrange
from datetime import date, timedelta

from domain.imports.source_original_read import SourceOriginals
from domain.ledger.cashflow_classification import personal_loan_principal
from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.ledger.confirmed_transfers_v2 import (
    FX_PAIR, MANUAL_FUNDING_PAIR, _leg_rows, _verified_leg, is_independent_equal_pair,
    is_independent_opposite_pair, is_manual_funding_pair, occupied_cashflow_ids,
)


MANUAL_TYPES = {'internal_transfer', 'fx_conversion', 'debt_repayment',
                'investment_funding', 'investment_withdrawal'}


def is_manual_anchor(row):
    return (row.get('classification_status') == 'confirmed'
            and row.get('classification_source') == 'manual'
            and not personal_loan_principal(row)
            and row.get('cashflow_type') in MANUAL_TYPES)


def manual_pair_type(anchor, other):
    if not is_manual_anchor(anchor) or personal_loan_principal(other) or any(
        row.get('source_evidence_status') != 'verified' or not row.get('amount')
        for row in (anchor, other)
    ):
        return None
    outgoing, incoming = sorted((anchor, other), key=lambda row: row['amount'])
    left, right = outgoing['pair_evidence'], incoming['pair_evidence']
    if is_independent_equal_pair(left, right):
        return 'internal_transfer'
    from domain.ledger.cash_transfer_pairs import is_cross_currency_card_repayment
    from domain.ledger.ledger_view import _load_accounts_config

    if (left['currency'] != right['currency'] and
            is_cross_currency_card_repayment(outgoing, incoming, _load_accounts_config().get('accounts', {}))):
        return 'credit_card_payment'
    if (left['currency'] != right['currency']
            and any(row.get('cashflow_type') == FX_PAIR and is_manual_anchor(row) for row in (anchor, other))
            and is_independent_opposite_pair(left, right)):
        return FX_PAIR
    return None


def _display_row(row):
    return {key: row.get(key) for key in ('id', 'account_code', 'transaction_date', 'amount', 'currency',
                                         'amount_cny', 'summary', 'description', 'source_evidence_status',
                                         'subcategory', 'cashflow_type')}


def pending_rows(cur, *, start, end, originals=None):
    occupied = occupied_cashflow_ids(cur)
    return [_display_row(row) for row in load_cash_rows(
        cur, start=start, end=end, base_currency='CNY', originals=originals
    ) if row['id'] not in occupied and row['amount'] and is_manual_anchor(row)]


def load_leg(cur, ident, day):
    rows = load_cash_rows(cur, start=day, end=day, base_currency='CNY', fact_id=ident,
                          include_pair_evidence=True)
    return rows[0] if rows else None


def _broker_funding_ids(cur, anchor, *, trade_id=None):
    cur.execute("""SELECT f.trade_id FROM public.pams_investment_trade_fact f
        WHERE f.trade_date=%s AND f.trade_type='deposit' AND f.net_cash=%s AND f.currency=%s
          AND f.account_code<>%s AND (%s::bigint IS NULL OR f.trade_id=%s)
          AND NOT EXISTS (SELECT 1 FROM public.pams_cashflow_fact c WHERE c.source_item_id=f.source_item_id)
          AND NOT EXISTS (SELECT 1 FROM public.evidence_confirmation e
              WHERE e.confirmation_type IN ('transfer','payment_proxy') AND e.revoked_at IS NULL
                AND (e.from_trade_id=f.trade_id OR e.to_trade_id=f.trade_id))
        ORDER BY f.trade_id""",
        (anchor['transaction_date'], -anchor['amount'], anchor['currency'], anchor['account_code'], trade_id, trade_id))
    return [row['trade_id'] for row in cur.fetchall()]


def _is_funding_anchor(anchor):
    return (anchor is not None and is_manual_anchor(anchor)
            and anchor['source_evidence_status'] == 'verified'
            and anchor['cashflow_type'] == MANUAL_FUNDING_PAIR and anchor['amount'] < 0)


def validate_manual_pair(cur, left, right, anchor_id):
    if not is_independent_opposite_pair(left, right):
        return None
    if right['kind'] == 'trade':
        if left['kind'] == 'cashflow' and left['id'] == anchor_id and is_manual_funding_pair(left, right):
            anchor = load_leg(cur, anchor_id, date.fromisoformat(left['day']))
            if _is_funding_anchor(anchor) and _broker_funding_ids(cur, anchor, trade_id=right['id']):
                return MANUAL_FUNDING_PAIR
        return None
    if left['kind'] != 'cashflow' or anchor_id not in (left['id'], right['id']):
        return None
    outgoing, incoming = (load_leg(cur, leg['id'], date.fromisoformat(leg['day'])) for leg in (left, right))
    if outgoing and incoming:
        anchor, other = (outgoing, incoming) if anchor_id == left['id'] else (incoming, outgoing)
        return manual_pair_type(anchor, other)
    return None


def _broker_funding_options(cur, anchor):
    ids = _broker_funding_ids(cur, anchor)
    if not ids:
        return []
    originals = SourceOriginals(cur)
    facts = _leg_rows(cur, 'trade', ids, lock=False)
    originals.verify_many(facts)
    bank_leg = {**anchor['pair_evidence'], 'kind': 'cashflow', 'day': anchor['transaction_date'].isoformat()}
    options = []
    for fact in facts:
        try:
            leg = _verified_leg(fact, 'trade', originals, hash_only=True)
        except ValueError:
            continue
        if is_manual_funding_pair(bank_leg, leg):
            options.append({
                'id': fact['id'], 'kind': 'trade', 'account_code': fact['account_code'],
                'transaction_date': fact['day'], 'amount': fact['amount'], 'currency': fact['currency'],
                'amount_cny': fact['amount'] if fact['currency'] == 'CNY' else None,
                'summary': '券商入金', 'source_evidence_status': 'verified', 'match_type': MANUAL_FUNDING_PAIR,
            })
    return options


def pairing_options(cur, anchor_id, *, month=None, offset=0, limit=10):
    cur.execute('SELECT event_date FROM public.pams_cashflow_fact WHERE cashflow_id=%s', (anchor_id,))
    fact = cur.fetchone()
    occupied = occupied_cashflow_ids(cur)
    if not fact or anchor_id in occupied:
        raise ValueError('这笔流水已配对或无法读取，请刷新待配对列表。')
    day = fact['event_date']
    anchor = load_leg(cur, anchor_id, day)
    if not anchor or not is_manual_anchor(anchor) or anchor['source_evidence_status'] != 'verified':
        raise ValueError('这笔流水的内部转账分类或来源凭证需要核对。')
    if month:
        start = date.fromisoformat(month + '-01')
        end = start.replace(day=monthrange(start.year, start.month)[1])
    else:
        start, end = day - timedelta(days=7), day + timedelta(days=7)
    rows = load_cash_rows(cur, start=start, end=end, base_currency='CNY', include_pair_evidence=True)
    options = [{**_display_row(row), 'match_type': match_type} for row in rows
               if row['id'] not in occupied and (match_type := manual_pair_type(anchor, row))]
    if start <= day <= end and _is_funding_anchor(anchor):
        options.extend(_broker_funding_options(cur, anchor))

    def sort_key(row):
        if row['currency'] == anchor['currency']:
            difference = abs(anchor['amount'] + row['amount'])
        elif anchor['amount_cny'] is not None and row['amount_cny'] is not None:
            difference = abs(anchor['amount_cny'] + row['amount_cny'])
        else:
            difference = None
        return (difference is None, difference or 0, abs(row['transaction_date'] - day),
                row.get('kind') != 'trade', -row['id'])

    options.sort(key=sort_key)
    from domain.ledger.combined_card_repayment import candidates
    from domain.ledger.ledger_view import _load_accounts_config

    groups = list(candidates(rows, _load_accounts_config().get('accounts', {}), occupied, anchor_id=anchor_id)) if offset == 0 else []
    return {'rows': options[offset:offset + limit], 'repayment_groups': groups,
            'next_offset': offset + limit if offset + limit < len(options) else None}
