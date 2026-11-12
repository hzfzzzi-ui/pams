"""Manual pairing for cashflows explicitly classified as internal transfers."""
from calendar import monthrange
from datetime import date, timedelta

from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.ledger.confirmed_transfers_v2 import (
    FX_PAIR, is_independent_equal_pair, is_independent_opposite_pair, occupied_cashflow_ids,
)


MANUAL_TYPES = {'internal_transfer', 'fx_conversion', 'debt_repayment',
                'investment_funding', 'investment_withdrawal'}


def is_manual_anchor(row):
    return (row.get('classification_status') == 'confirmed'
            and row.get('classification_source') == 'manual'
            and row.get('cashflow_type') in MANUAL_TYPES)


def manual_pair_type(anchor, other):
    if not is_manual_anchor(anchor) or any(
        row.get('source_evidence_status') != 'verified' or not row.get('amount')
        for row in (anchor, other)
    ):
        return None
    outgoing, incoming = sorted((anchor, other), key=lambda row: row['amount'])
    left, right = outgoing['pair_evidence'], incoming['pair_evidence']
    if is_independent_equal_pair(left, right):
        return 'internal_transfer'
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
    options.sort(key=lambda row: (abs(row['transaction_date'] - day), -row['id']))
    return {'rows': options[offset:offset + limit],
            'next_offset': offset + limit if offset + limit < len(options) else None}
