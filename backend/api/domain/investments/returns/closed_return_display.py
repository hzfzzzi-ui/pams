"""Expose reconciled closed returns without substituting them for period TWR."""

from datetime import date
from decimal import Decimal


def closed_return_summary(closed, *, start, period_closed=None):
    if not closed:
        return None
    period = Decimal(0)
    incomplete = 0
    for item in closed.get('items', []):
        for cycle in item.get('cycles', []):
            opened, exited = (date.fromisoformat(cycle[key]) for key in ('open_date', 'exit_date'))
            if exited < start:
                continue
            if opened < start:
                incomplete += 1
                continue
            period += Decimal(cycle['trade_pnl']) + Decimal(cycle['distributions'])
    if period_closed is not None and incomplete:
        covered = {(item['account_code'], item['symbol'], cycle['exit_date'])
                   for item in period_closed.get('items', []) for cycle in item.get('cycles', [])}
        needed = {(item['account_code'], item['symbol'], cycle['exit_date'])
                  for item in closed.get('items', []) for cycle in item.get('cycles', [])
                  if date.fromisoformat(cycle['open_date']) < start <= date.fromisoformat(cycle['exit_date'])}
        if needed <= covered:
            period = Decimal(period_closed['amount'])
            incomplete = 0
    return {
        'base_currency': closed.get('base_currency'),
        'lifetime_amount': closed.get('amount'),
        'period_amount': str(period) if incomplete == 0 else None,
        'period_start': start.isoformat(),
        'period_status': 'opening_valuation_missing' if incomplete else 'known',
        'unresolved_cycles': incomplete,
        'included_in_return_curve': False,
    }
