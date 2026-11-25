"""Personal position history for a selected holding, with dated execution evidence."""
from datetime import date

from domain.investments.valuation.capital_ownership import position_ownership
from domain.investments.returns.fund_return_history import _trade_day
from domain.investments.returns.investment_return_math import norm
from domain.investments.holdings.position_performance import (
    BUYS, SELLS, CHANGES, ZERO, _load_positions, _load_prices, _market_price, _position_days,
    _supplement_selected_overseas_prices,
)
from domain.investments.trades.trade_read_v2 import load_trade_rows
from domain.investments.valuation.valuation_nav import load_fx_timelines, series_dates


def holding_history(cur, selection, start, end, owner_ratio, *, all_trades=False):
    dates = series_dates(start, end, 'daily')
    positions, kinds, keys = _load_positions(cur, end, (), selection)
    prices = _load_prices(cur, kinds, end)
    prices = _supplement_selected_overseas_prices(prices, kinds, start, end)
    fx = load_fx_timelines(cur, 'CNY') if any(key[1] != 'CNY' for key in kinds) else {}
    values = {day: [] for day in dates}
    trades = []
    owners = position_ownership(cur, {code for code, _ in selection}, dates, owner_ratio)
    for (account, currency, asset_id), key in keys.items():
        kind = kinds[key]
        events = {}
        for trade in load_trade_rows(cur, account_code=account, start=date.min, end=date.max if all_trades else end,
                                     include_fund_execution=True, trade_types=tuple(sorted(CHANGES)),
                                     instrument=(asset_id, key[2])):
            day = _trade_day(trade) if kind == 'fund' else trade['trade_date']
            if day <= end:
                events.setdefault(day, []).append(trade)
            action = trade['trade_type']
            if ((all_trades or start <= day <= end) and action in BUYS | SELLS and trade['source_evidence_status'] == 'verified'
                    and (kind != 'fund' or trade['fund_execution_confirmed'])):
                trades.append({'date': day, 'side': 'buy' if action in BUYS else 'sell',
                               'quantity': trade['quantity'], 'price': trade['price'], 'currency': currency,
                               'account': account, 'id': trade['trade_id']})
        for day, qty, confirmed, reference, _ in _position_days(dates, positions[key], events, kind):
            ownership = owners[account].get(day)
            if day not in values or qty is None or ownership is None:
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


def holding_price_comparison(cur, selection, start, end, benchmark_loader):
    """Compare a single instrument and its benchmark on their common observed dates."""
    positions, kinds, keys = _load_positions(cur, end, (), selection)
    identities = {(key[1], key[2], kinds[key]) for key in keys.values()}
    if len(identities) != 1:
        return {'status': 'unsupported', 'rows': []}
    key = next(iter(keys.values()))
    kind = kinds[key]
    benchmarks = {'stock': ('沪深300', '000300.SH', 'sh000300'),
                  'fund': ('上证指数', '000001.SH', 'sh000001')}
    if kind == 'overseas':
        markets = {row.get('major_category') for row in positions[key].values()}
        overseas = {'美股': ('标普500', '^GSPC', '^GSPC'),
                    '港股': ('恒生指数', '^HSI', '^HSI'),
                    '韩股': ('KOSPI', '^KS11', '^KS11')}
        if len(markets) != 1 or next(iter(markets)) not in overseas:
            return {'status': 'benchmark_unavailable', 'rows': []}
        benchmarks['overseas'] = overseas[next(iter(markets))]
    if kind not in benchmarks:
        return {'status': 'benchmark_unavailable', 'rows': []}
    name, symbol, provider_symbol = benchmarks[kind]
    benchmark_closes = benchmark_loader(symbol, provider_symbol, name)
    prices = _load_prices(cur, kinds, end)
    prices = _supplement_selected_overseas_prices(prices, kinds, start, end)
    observations = []
    for day in series_dates(start, end, 'daily'):
        mark = _market_price(prices, key, kind, day)
        benchmark = benchmark_closes.get(day.isoformat())
        if mark and mark[0] == day and benchmark is not None:
            observations.append((day, mark[1], benchmark))
    if not observations:
        return {'status': 'missing', 'rows': []}
    base_price, base_benchmark = observations[0][1:]
    return {
        'status': 'ok', 'benchmark': name,
        'base_date': observations[0][0].isoformat(),
        'drawdown_from_peak_pct': norm((observations[-1][1] / max(p for _, p, _ in observations) - 1) * 100),
        'rows': [{'date': day.isoformat(),
                  'asset_index': norm(price / base_price),
                  'benchmark_index': norm(benchmark / base_benchmark),
                  'asset_return_pct': norm((price / base_price - 1) * 100),
                  'benchmark_return_pct': norm((benchmark / base_benchmark - 1) * 100)}
                 for day, price, benchmark in observations],
    }
