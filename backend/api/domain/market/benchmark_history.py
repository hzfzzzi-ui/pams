"""Read local index closes first, supplement missing sessions from the public daily series."""
from datetime import timedelta
from functools import lru_cache

from domain.market.price_history import build_price_series_payload, normalize_price_instrument
from domain.market.tencent_stock_history import fetch_history


@lru_cache(maxsize=32)
def _completed_history(symbol, start, end):
    return fetch_history(symbol, start=start, end=end)


def local_benchmark(cur, start, end, symbol='000300.SH'):
    index = normalize_price_instrument(symbol, benchmark=True)
    series = build_price_series_payload(cur, instruments=[index], start=start, end=end, normalize=False)
    return {row['date']: row['close'] for row in series['series'][0]['rows']}


def benchmark_history(local, start, end, today, symbol='sh000300', name='沪深300'):
    closes = dict(local)
    # Only completed sessions qualify. A partial current-day bar is not a close.
    through = min(end, today - timedelta(days=1))
    if start <= through and any(
        day.weekday() < 5 and day.isoformat() not in closes
        for day in (start + timedelta(days=i) for i in range((through - start).days + 1))
    ):
        try:
            for item in _completed_history(symbol, start, through):
                closes.setdefault(item['date'].isoformat(), item['close'])
        except (OSError, ValueError):
            pass
    return {'name': name, 'rows': [{'date': day, 'close': str(closes[day])} for day in sorted(closes)]}
