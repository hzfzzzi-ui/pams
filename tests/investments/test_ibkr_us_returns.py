from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.ibkr_us_returns import _native_inputs, _payload  # noqa: E402


FRIDAY = date(2026, 3, 6)
MONDAY = date(2026, 3, 9)


def trade(day, conid, exchange, *, quantity, net_cash):
    return ({
        'business_date': day.isoformat(), 'currency': 'USD', 'trade_type': 'buy',
        'quantity': quantity, 'net_cash': net_cash,
    }, {'official_attributes': {'conid': conid, 'listingExchange': exchange}}, {})


def distribution(conid, summary, net_cash):
    return ({
        'business_date': MONDAY.isoformat(), 'currency': 'USD', 'summary': summary, 'net_cash': net_cash,
    }, {'official_attributes': {'conid': conid}}, {})


def test_cny_us_sleeve_checks_contracts_and_uses_broker_dates():
    positions = {
        FRIDAY: {'1': {'currency': 'USD', 'quantity': Decimal(1), 'market_value': Decimal(100)}},
        MONDAY: {'1': {'currency': 'USD', 'quantity': Decimal(2), 'market_value': Decimal(222)}},
    }
    candidates = {
        'trade': {
            'initial': trade(FRIDAY, '1', 'NASDAQ', quantity='1', net_cash='-100'),
            'add': trade(MONDAY, '1', 'NASDAQ', quantity='1', net_cash='-112'),
            'foreign': trade(MONDAY, '2', 'SEHK', quantity='1', net_cash='-500'),
        },
        'cash': {
            'dividend': distribution('1', 'Dividends', '10'),
            'tax': distribution('1', 'Withholding Tax', '-2'),
            'foreign': distribution('2', 'Dividends', '20'),
        },
    }
    values, flows = _native_inputs(positions, candidates)
    kwargs = dict(start=FRIDAY, through=MONDAY, interval='daily', owner_ratio=Decimal('0.5'))
    assert _payload(values, flows, {FRIDAY: Decimal(7)}, **kwargs) is None
    result = _payload(values, flows, {FRIDAY: Decimal(7), MONDAY: Decimal(7)}, **kwargs)
    assert [row['date'] for row in result['rows']] == [str(FRIDAY), str(MONDAY)]
    assert Decimal(result['rows'][-1]['net_flow']) == Decimal(364)
    assert Decimal(result['summary']['total_return']) == Decimal(63)
    assert Decimal(result['summary']['total_return_rate']) == Decimal('0.18')
    positions[MONDAY]['1']['quantity'] = Decimal(3)
    with pytest.raises(ValueError, match='reproduce broker quantities'):
        _native_inputs(positions, candidates)
