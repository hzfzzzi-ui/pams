"""Unfilled subscription principal held between a documented debit and refund."""
from collections import Counter
from datetime import date

from domain.imports.trade_supplemental_evidence import read as trade_evidence
from domain.ledger.cashflow_read_v2 import load_cash_rows


def _claim(order, debit, refund):
    if (order['trade_type'] != 'fund_subscription' or not order.get('fund_execution_confirmed')
            or any(row['source_evidence_status'] != 'verified' for row in (order, debit, refund))
            or debit['account_code'] != refund['account_code'] or debit['account_code'] == order['account_code']
            or not order['currency'] == debit['currency'] == refund['currency']
            or not (debit['amount'] < order['net_cash'] < 0)
            or debit['amount'] + refund['amount'] != order['net_cash']):
        return None
    paid = debit['posting_date'] or debit['transaction_date']
    returned = refund['posting_date'] or refund['transaction_date']
    if paid < returned:
        return paid, returned, refund['amount']
    return None


def subscription_refunds(cur, trades):
    # Only explicitly bound cash legs may settle an order. Equal amounts alone
    # cannot identify a payer, an order or a refund.
    cur.execute("""SELECT from_trade_id, evidence->'fields'->'subscription_settlement' AS legs
        FROM evidence_confirmation WHERE confirmation_type='trade_source' AND revoked_at IS NULL
        AND evidence->'fields' ? 'subscription_settlement'""")
    bindings = {}
    for row in cur.fetchall():
        legs = row['legs']
        if isinstance(legs, dict):
            ids = (legs.get('payment_cashflow_id'), legs.get('refund_cashflow_id'))
            if all(type(i) is int and i > 0 for i in ids):
                bindings.setdefault(row['from_trade_id'], set()).add(ids)
    used = Counter(i for links in bindings.values() for pair in links for i in pair)
    claims = []
    for order in trades:
        links = bindings.get(order['trade_id'], set())
        if len(links) != 1:
            continue
        paid, returned = next(iter(links))
        if used[paid] != 1 or used[returned] != 1:
            continue
        ids = {'payment_cashflow_id': paid, 'refund_cashflow_id': returned}
        if not any(p['status'] == 'verified' and p['fields'].get('subscription_settlement') == ids
                   for p in trade_evidence(cur, trade_id=order['trade_id'])):
            continue
        cash = {r['id']: r for r in load_cash_rows(cur, start=date.min, end=date.max,
                base_currency='CNY', fact_ids=[paid, returned])}
        if paid in cash and returned in cash:
            claim = _claim(order, cash[paid], cash[returned])
            if claim:
                claims.append(claim)
    return claims
