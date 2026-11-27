"""Read local index closes first, supplement missing sessions from the public daily series."""
from datetime import date, timedelta
from functools import lru_cache
import json
from urllib.request import Request, urlopen

from domain.market.price_history import build_price_series_payload, normalize_price_instrument, fetch_yfinance_history
from domain.market.tencent_stock_history import fetch_history


@lru_cache(maxsize=32)
def _completed_history(symbol, start, end):
    if symbol.startswith('^'):
        closes, _ = fetch_yfinance_history([symbol], start=start, end=end)
        return [{'date': day, 'close': price} for day, price in closes.get(symbol, {}).items()]
    return fetch_history(symbol, start=start, end=end)


@lru_cache(maxsize=64)
def overseas_history(symbol, start, end):
    """Read completed overseas closes without mutating market facts."""
    closes, _ = fetch_yfinance_history([symbol], start=start, end=end)
    return tuple(sorted(closes.get(symbol, {}).items()))


@lru_cache(maxsize=32)
def eastmoney_index_history(secid: str, start: date, end: date, cache_day: date | None = None):
    """Read daily index closes from Eastmoney push2his without mutating market facts."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    _ = cache_day
    beg = start.strftime('%Y%m%d')
    end_s = end.strftime('%Y%m%d')
    url = (
        'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        f'?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg={beg}&end={end_s}'
    )
    req = Request(url, headers={'User-Agent': 'PAMS/2.0'})
    with urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    klines = (payload.get('data') or {}).get('klines') or []
    rows = []
    for line in klines:
        parts = str(line).split(',')
        if len(parts) < 2:
            continue
        day_str, close_str = parts[0].strip(), parts[1].strip()
        try:
            close_val = float(close_str)
        except ValueError:
            continue
        if day_str and close_val > 0:
            rows.append((day_str, close_val))
    return tuple(sorted(rows))


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
