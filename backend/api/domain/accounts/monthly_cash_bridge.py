"""Reconcile native month-end cash against bounded, verified source evidence."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.market.month_end_marks import is_month_end


def _interval_balances(opening, closing, changes, targets):
    first, last = opening['as_of'], closing['as_of']
    initial = Decimal(opening['amount'])
    if initial + sum(changes.values()) != Decimal(closing['amount']):
        return None
    return {day: initial + sum(value for posted, value in changes.items() if posted <= day)
            for day in targets if first < day <= last}


def posted_balances(closes, flows, dates, currency, charges):
    """Prove the posting interval to the cent before reading its intermediate balances."""
    results = {}
    for opening, closing in zip(closes, closes[1:]):
        start, end = opening['as_of'], closing['as_of']
        targets = [d for d in dates if start < d < end]
        if not targets or (end - start).days > 62:
            continue
        movement = defaultdict(Decimal)
        uncertain = False
        for row in flows:
            posting = row.get('posting_date')
            day = posting or row['transaction_date']
            if not start < day <= end:
                continue
            if not posting or row['source_evidence_status'] != 'verified' or row['currency'] != currency:
                uncertain = True
                break
            movement[day] += row['amount']
        if uncertain:
            continue
        first, last = Decimal(opening['amount']), Decimal(closing['amount'])
        if first + sum(movement.values()) != last:
            # A dated charge can close a gap in the published ledger. Never add it
            # to an interval which already reconciles, or accept competing evidence.
            additions = [(d, entries) for d, entries in charges.items() if start < d <= end]
            if any(len(entries) != 1 for _, entries in additions):
                continue
            for d, entries in additions:
                unit, value = next(iter(entries))
                if unit == currency:
                    movement[d] -= Decimal(value)
        results.update(_interval_balances(opening, closing, movement, targets) or {})
    return results


def reconciled_card_months(cur, accounts, dates, closings, charges):
    targets = [d for d in dates if is_month_end(d)]
    codes = [c for c, (_, card) in accounts.items() if card and closings.get(c)]
    if not targets or not codes:
        return {}
    # Select by posting date, including transactions initiated in a different
    # reporting period. The canonical reader checks each fact against its original.
    first, last = min(targets) - timedelta(days=62), max(targets) + timedelta(days=62)
    cur.execute("""SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code=ANY(%s::text[])
                   AND coalesce(posting_date,event_date) BETWEEN %s AND %s""", (codes, first, last))
    ids = [r['cashflow_id'] for r in cur.fetchall()]
    rows = load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY', fact_ids=ids) if ids else []
    by_account = defaultdict(list)
    for row in rows:
        by_account[row['account_code']].append(row)
    result = {}
    for code in codes:
        derived = posted_balances(closings[code], by_account[code], targets, accounts[code][0],
                                  {d: entries for (c, d), entries in charges.items() if c == code})
        result.update({(code, day): value for day, value in derived.items()})
    return result


def table_balances_at(closes, movements, targets):
    """Give a statement-reconciled balance or a separately labelled one-ended estimate."""
    months = {}
    if not movements:
        return months
    latest_reconciled = None
    for opening, closing in zip(closes, closes[1:]):
        first, last = opening['as_of'], closing['as_of']
        changes = {day: value for day, value in movements.items() if first < day <= last}
        closed = _interval_balances(opening, closing, changes, targets)
        if (last - first).days > 35 or closed is None:
            latest_reconciled = None
            continue
        latest_reconciled = last
        for day, amount in closed.items():
            months[day] = (amount, 'posted_statement_bridge', first, last)
    if latest_reconciled:
        anchor = Decimal(closes[-1]['amount'])
        for day in targets:
            if latest_reconciled < day <= max(movements):
                relevant = {d: v for d, v in movements.items() if latest_reconciled < d <= day}
                months[day] = (anchor + sum(relevant.values()), 'statement_plus_postings',
                               latest_reconciled, max(relevant, default=latest_reconciled))
    return months
