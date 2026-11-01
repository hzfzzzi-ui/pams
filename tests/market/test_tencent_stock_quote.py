from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from domain.market.tencent_stock_quote import fetch_tencent_stock_quotes, parse_tencent_stock_quotes


def quote(symbol='sh600001', code='600001', price='12.34', timestamp='20261030161438'):
    fields = [''] * 31
    fields[2], fields[3], fields[30] = code, price, timestamp
    return f'v_{symbol}="' + '~'.join(fields) + '";'


def test_parse_preserves_observed_date_and_price():
    row = parse_tencent_stock_quotes(quote())['sh600001']
    assert row['price'] == Decimal('12.34')
    assert row['price_date'] == date(2026, 10, 30)
    assert row['price_time'].utcoffset().total_seconds() == 8 * 3600


def test_rejects_wrong_symbol_bad_time_and_invalid_price():
    for line in (quote(code='600002'), quote(timestamp=''), quote(timestamp='2026111123456'), quote(price='NaN'), quote(price='0')):
        assert parse_tencent_stock_quotes(line) == {}


def test_fetch_returns_only_requested_symbols_and_validates_input(monkeypatch):
    monkeypatch.delenv('PAMS_SOURCE_MODE', raising=False)
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return (quote() + '\n' + quote('sh600002', '600002')).encode('gbk')

    with patch('urllib.request.urlopen', return_value=Response()) as call:
        assert set(fetch_tencent_stock_quotes(['sh600001'])) == {'sh600001'}
        assert call.call_args.kwargs['timeout'] == 15
    with pytest.raises(ValueError, match='symbol'):
        fetch_tencent_stock_quotes(['sh600001&other=1'])
