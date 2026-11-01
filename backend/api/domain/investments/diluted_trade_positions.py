"""Reconstruct securities quantity and diluted cash cost from reviewed trades.

This calculation is not an account snapshot, an assertion of source coverage,
or evidence of the broker's cost basis. Callers own those checks.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


def _number(value, field):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        return number
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'invalid {field}') from exc


def calculate_diluted_positions(rows, *, closed_adjustments=None):
    """Use signed source net cash; reset basis only after an exact full exit.

    Input must be an already verified, complete set. Dividends reduce cost and
    separate dividend tax increases it, using their signed source net cash.
    Unsupported transfers and corporate actions are rejected. An adjustment
    after a proven complete exit must be returned separately to the caller.
    """
    groups = defaultdict(list)
    identities = set()
    for row in rows:
        account, symbol, currency = (str(row.get(k) or '').strip() for k in ('account_code', 'symbol', 'currency'))
        ident = row.get('trade_id')
        if (not account or not symbol or not currency or isinstance(ident, bool)
                or not isinstance(ident, int) or ident <= 0 or ident in identities):
            raise ValueError('missing or duplicate trade identity')
        if row.get('side') not in {'buy', 'sell', 'dividend', 'tax'}:
            raise ValueError('unsupported non-trade action')
        if not row.get('trade_date') or not row.get('trade_time'):
            raise ValueError('trade ordering is unknown')
        identities.add(ident)
        groups[(account, symbol, currency)].append(row)
    result = []
    for (account, symbol, currency), trades in sorted(groups.items()):
        same_time_sides = defaultdict(set)
        for row in trades:
            same_time_sides[(str(row['trade_date']), str(row['trade_time']))].add(row['side'])
        if any(len(sides) > 1 for sides in same_time_sides.values()):
            raise ValueError('same-time event ordering is ambiguous')
        trades.sort(key=lambda row: (str(row['trade_date']), str(row['trade_time']), row['trade_id']))
        quantity = Decimal(0)
        cost = Decimal(0)
        remaining_cost = Decimal(0)
        realized = Decimal(0)
        distributions = Decimal(0)
        current_ids = []
        seen_full_exit = False
        for row in trades:
            net_cash = _number(row.get('net_cash'), 'net_cash')
            if row['side'] in {'dividend', 'tax'}:
                if (row['side'] == 'dividend' and net_cash <= 0) or (row['side'] == 'tax' and net_cash >= 0):
                    raise ValueError('invalid dividend or tax direction')
                distributions += net_cash
                if quantity == 0:
                    if not seen_full_exit or closed_adjustments is None:
                        raise ValueError('dividend or tax without an open position')
                    closed_adjustments.append({'trade_id': row['trade_id'], 'account_code': account,
                                               'symbol': symbol, 'side': row['side'], 'net_cash': net_cash})
                    continue
                cost -= net_cash
                current_ids.append(row['trade_id'])
                continue
            qty = _number(row.get('quantity'), 'quantity')
            if qty <= 0 or (row['side'] == 'buy' and net_cash >= 0) or (row['side'] == 'sell' and net_cash <= 0):
                raise ValueError('invalid trade direction or quantity')
            if row['side'] == 'sell' and qty > quantity:
                raise ValueError('sell exceeds known quantity')
            if row['side'] == 'buy':
                remaining_cost -= net_cash
            else:
                released_cost = remaining_cost * qty / quantity
                remaining_cost -= released_cost
                realized += net_cash - released_cost
            quantity += qty if row['side'] == 'buy' else -qty
            cost -= net_cash
            current_ids.append(row['trade_id'])
            if quantity == 0:
                seen_full_exit = True
                cost = Decimal(0)
                remaining_cost = Decimal(0)
                current_ids = []
        if quantity:
            result.append({'account_code': account, 'symbol': symbol, 'currency': currency,
                           'quantity': quantity, 'diluted_cost': cost,
                           'diluted_cost_price': cost / quantity,
                           'remaining_cost': remaining_cost,
                           'remaining_cost_price': remaining_cost / quantity,
                           'realized_trade_pnl': realized,
                           'net_distributions': distributions,
                           'trade_ids': current_ids,
                           'all_event_ids': [row['trade_id'] for row in trades],
                           'latest_trade_date': trades[-1]['trade_date']})
    return result
