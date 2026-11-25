"""Unfilled subscription principal held between a documented debit and refund."""
from collections import Counter
from datetime import date

from domain.imports.publication.trade_supplemental_evidence import read as trade_evidence
from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows
from domain.investments.trades.trade_read_v2 import load_trade_rows


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


def _verified_settlements(cur, trades=None, cash_ids=None):
    # Only explicitly bound cash legs may settle an order. Equal amounts alone
    # cannot identify a payer, an order or a refund.
    cur.execute("""SELECT c.from_trade_id,f.account_code,c.evidence->'fields'->'subscription_settlement' AS legs
        FROM evidence_confirmation c JOIN pams_investment_trade_fact f ON f.trade_id=c.from_trade_id
        WHERE c.confirmation_type='trade_source' AND c.revoked_at IS NULL
        AND c.evidence->'fields' ? 'subscription_settlement'""")
    bindings = {}
    accounts = {}
    for row in cur.fetchall():
        legs = row['legs']
        if isinstance(legs, dict):
            ids = (legs.get('payment_cashflow_id'), legs.get('refund_cashflow_id'))
            if all(type(i) is int and i > 0 for i in ids):
                bindings.setdefault(row['from_trade_id'], set()).add(ids)
                accounts[row['from_trade_id']] = row['account_code']
    used = Counter(i for links in bindings.values() for pair in links for i in pair)
    known = {row['trade_id']: row for row in trades} if trades is not None else None
    for ident, links in bindings.items():
        if len(links) != 1:
            continue
        paid, returned = next(iter(links))
        if used[paid] != 1 or used[returned] != 1 or (cash_ids is not None and not cash_ids.intersection((paid, returned))):
            continue
        if known is not None:
            order = known.get(ident)
        else:
            candidates = load_trade_rows(cur, account_code=accounts[ident], start=date.min, end=date.max,
                                         fact_id=ident, include_fund_execution=True)
            order = candidates[0] if len(candidates) == 1 else None
        if not order:
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
                yield claim, paid, returned


def subscription_refunds(cur, trades):
    return [claim for claim, _, _ in _verified_settlements(cur, trades=trades)]


def subscription_cash_ids(cur, cash_ids):
    """Both legs of an evidenced partially filled subscription move investment principal."""
    return {ident for _, debit, refund in _verified_settlements(cur, cash_ids=cash_ids)
            for ident in (debit, refund)}
