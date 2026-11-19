"""Reconstruct a reviewed full-exit position without creating trade facts."""
from datetime import date
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def replay_reviewed_cycle(rows, evidence):
    """Return marked-to-market inputs only for a complete, single-entry cycle.

    Supplemental evidence must be independently reviewed by the source layer.
    Cumulative NAV marks are estimates unless unit and cumulative NAV agree at
    both confirmed endpoints; even then their intermediate values stay marked
    as estimates because cash distributions are not independently evidenced.
    """
    if len(rows) != 2 or any(row.get('source_evidence_status') != 'verified' for row in rows):
        return None
    buys = [row for row in rows if row['trade_type'] in {'buy', 'fund_subscription'}]
    sells = [row for row in rows if row['trade_type'] in {'sell', 'fund_redemption'}]
    if len(buys) != 1 or len(sells) != 1:
        return None
    buy, sell = buys[0], sells[0]
    if (buy.get('asset_id') is None or buy.get('asset_id') != sell.get('asset_id')
            or buy.get('account_code') != sell.get('account_code') or buy.get('currency') != sell.get('currency')):
        return None
    buy_cash = _number(buy.get('net_cash'))
    if buy_cash is None or buy_cash >= 0:
        return None
    sources = {row['trade_id']: [item['fields'] for item in evidence.get(row['trade_id'], [])
                                if item.get('status') == 'verified' and isinstance(item.get('fields'), dict)]
               for row in rows}
    confirmations = [fields for fields in sources[sell['trade_id']]
                     if fields.get('complete_exit') is True and fields.get('complete_history') is True
                     and isinstance(fields.get('complete_trade_ids'), list)
                     and all(type(value) is int for value in fields['complete_trade_ids'])
                     and set(fields.get('complete_trade_ids') or []) == {buy['trade_id'], sell['trade_id']}]
    if not confirmations:
        return None
    quantities = {_number(fields.get('quantity')) for fields in sources[buy['trade_id']] if fields.get('quantity') is not None}
    exits = {(fields.get('execution_date'), _number(fields.get('quantity')), _number(fields.get('unit_nav')))
             for fields in sources[sell['trade_id']]
             if fields.get('execution_date') and fields.get('quantity') is not None and fields.get('unit_nav') is not None}
    if len(quantities) != 1 or len(exits) != 1:
        return None
    quantity = next(iter(quantities))
    exit_date_text, exit_quantity, exit_nav = next(iter(exits))
    if quantity is None or quantity <= 0 or exit_quantity != quantity or exit_nav is None or exit_nav <= 0:
        return None
    try:
        exit_date = date.fromisoformat(exit_date_text)
    except (ValueError, TypeError):
        return None
    if not buy['trade_date'] < exit_date <= sell['trade_date']:
        return None
    net = _number(sell.get('net_cash'))
    if net is None or abs(net - quantity * exit_nav) > Decimal('0.005'):
        return None
    observations = {}
    closed_days = set()
    for fields in sources[buy['trade_id']]:
        closures = fields.get('valuation_closed_dates', [])
        if not isinstance(closures, list):
            return None
        try:
            closed_days.update(date.fromisoformat(day) for day in closures)
        except (TypeError, ValueError):
            return None
        entries = fields.get('valuation_observations', [])
        if not isinstance(entries, list):
            return None
        for item in entries:
            try:
                day = date.fromisoformat(item['date'])
                price = _number(item['cumulative_nav'])
            except (KeyError, TypeError, ValueError):
                return None
            if price is None or price <= 0 or (day in observations and observations[day] != price):
                return None
            observations[day] = price
    if observations.get(exit_date) != exit_nav:
        return None
    if closed_days & observations.keys():
        return None
    try:
        anchors = [(date.fromisoformat(fields['valuation_date']), _number(fields['valuation_unit_price']))
                   for fields in sources[buy['trade_id']]
                   if fields.get('valuation_date') and fields.get('valuation_unit_price')]
    except (TypeError, ValueError):
        return None
    if not anchors or not all(value is not None and observations.get(day) == value for day, value in anchors):
        return None
    return {'buy_quantity': quantity, 'exit_date': exit_date, 'prices': observations, 'closed_days': closed_days,
            'valuation_status': 'estimated_cumulative_nav'}
