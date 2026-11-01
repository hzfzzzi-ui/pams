"""Parse Tencent A-share observations without inventing a valuation date."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import urllib.request
from zoneinfo import ZoneInfo


_SYMBOL = re.compile(r"^(?:sh|sz|bj)\d{6}$")


def parse_tencent_stock_quotes(text: str) -> dict[str, dict]:
    """Return only quotes with a positive price, matching code and source time.

    Parsing an observation does not establish that it is a closing price or
    suitable for a particular valuation date; callers must make that decision.
    """
    observations = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition('=')
        symbol = key.removeprefix('v_') if key.startswith('v_') else ''
        if not sep or not _SYMBOL.fullmatch(symbol):
            continue
        fields = value.strip().removesuffix(';').strip('"').split('~')
        if len(fields) <= 30 or fields[2] != symbol[2:] or not re.fullmatch(r'\d{14}', fields[30]):
            continue
        try:
            price = Decimal(fields[3])
            observed_at = datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        except (InvalidOperation, ValueError):
            continue
        if not price.is_finite() or price <= 0:
            continue
        observations[symbol] = {'symbol': symbol, 'price': price, 'price_time': observed_at,
                                'source': 'tencent', 'price_date': observed_at.date()}
    return observations


def fetch_tencent_stock_quotes(symbols: list[str]) -> dict[str, dict]:
    """Fetch quotes with their actual observation time; never persist data."""
    from domain.imports.isolation import require_external_access
    require_external_access()
    if any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError('invalid Tencent stock symbol')
    if not symbols:
        return {}
    requested = set(symbols)
    url = 'https://qt.gtimg.cn/q=' + ','.join(dict.fromkeys(symbols))
    request = urllib.request.Request(url, headers={'User-Agent': 'PAMS/2.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        observations = parse_tencent_stock_quotes(response.read().decode('gbk', errors='replace'))
    return {symbol: row for symbol, row in observations.items() if symbol in requested}


def completed_session_prices(observations: dict[str, dict], *, now: datetime) -> dict[str, Decimal]:
    """Only report a daily close when its observed time is after session close."""
    if now.utcoffset() is None:
        raise ValueError('aware current time required')
    import exchange_calendars as xcals
    day = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    try:
        calendar = xcals.get_calendar('XSHG')
        if not calendar.is_session(day.isoformat()):
            return {}
        close = calendar.session_close(day.isoformat()).to_pydatetime()
    except (ValueError, OverflowError):
        return {}
    return {symbol: row['price'] for symbol, row in observations.items()
            if row.get('price_date') == day and isinstance(row.get('price_time'), datetime)
            and row['price_time'].utcoffset() is not None and close <= row['price_time'] <= now}
