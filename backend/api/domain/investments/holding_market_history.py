"""Personal position history for a selected holding, with dated execution evidence."""
from datetime import date

from domain.investments.fund_return_history import _trade_day
from domain.investments.investment_return_math import norm
from domain.investments.position_performance import (
    BUYS, SELLS, CHANGES, ZERO, _load_positions, _load_prices, _market_price, _position_days,
)
from domain.investments.trade_read_v2 import load_trade_rows
from domain.investments.valuation_nav import load_fx_timelines, series_dates


def holding_history(cur, selection, start, end, owner_ratio):
    dates = series_dates(start, end, 'daily')
    positions, kinds, keys = _load_positions(cur, end, (), selection)
    prices = _load_prices(cur, kinds, end)
    fx = load_fx_timelines(cur, 'CNY') if any(key[1] != 'CNY' for key in kinds) else {}
    values = {day: [] for day in dates}
    trades = []
    for (account, currency, asset_id), key in keys.items():
        kind = kinds[key]
        ownership = owner_ratio(account)
        events = {}
        for trade in load_trade_rows(cur, account_code=account, start=date.min, end=end,
                                     include_fund_execution=True, trade_types=tuple(sorted(CHANGES)),
                                     instrument=(asset_id, key[2])):
            day = _trade_day(trade) if kind == 'fund' else trade['trade_date']
            events.setdefault(day, []).append(trade)
            action = trade['trade_type']
            if (start <= day <= end and action in BUYS | SELLS and trade['source_evidence_status'] == 'verified'
                    and (kind != 'fund' or trade['fund_execution_confirmed'])):
                trades.append({'date': day, 'side': 'buy' if action in BUYS else 'sell',
                               'quantity': trade['quantity'], 'price': trade['price'], 'currency': currency,
                               'account': account, 'id': trade['trade_id']})
        for day, qty, confirmed, reference, _ in _position_days(dates, positions[key], events, kind):
            if day not in values or qty is None:
                continue
            mark = _market_price(prices, key, kind, day)
            anchor = positions[key].get(day)
            if anchor and anchor['current_price'] is not None and (not mark or mark[0] < day):
                mark = day, anchor['current_price']
            if kind == 'reference' and reference is not None:
                mark = confirmed, reference
            rate = 1 if currency == 'CNY' else next((r for d, r in reversed(fx.get(currency, ())) if d <= day), None)
            if qty == 0:
                values[day].append(ZERO)
            elif mark and mark[1] is not None and rate is not None:
                values[day].append(qty * mark[1] * rate * ownership)
    return {'rows': [{'date': day.isoformat(), 'value': norm(sum(parts)) if len(parts) == len(selection) else None}
                     for day, parts in values.items()],
            'trades': [{k: norm(v) for k, v in trade.items()}
                       for trade in sorted(trades, key=lambda t: (t['date'], t['id']))]}
