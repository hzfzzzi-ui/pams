from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.investments.holdings_positions import _replay_weighted_average_trade_pnl, _trade_order_ambiguous


def test_same_day_uses_trade_time_not_import_order():
    day = date(2026, 6, 29)
    rows = [
        {'trade_id': 1, 'trade_date': day, 'trade_time': datetime(2026, 6, 29, 10), 'trade_type': 'sell',
         'quantity': 100, 'net_cash': '1200', 'effective_fx_rate': 1},
        {'trade_id': 2, 'trade_date': day, 'trade_time': datetime(2026, 6, 29, 9), 'trade_type': 'buy',
         'quantity': 200, 'net_cash': '-2000', 'effective_fx_rate': 1},
    ]
    assert not _trade_order_ambiguous(rows)
    state = _replay_weighted_average_trade_pnl(rows, as_of_key='2026-06-29')
    assert state['quantity'] == Decimal('100')
    assert state['cost'] == Decimal('1000')
    assert state['realized_pnl'] == Decimal('200')


def test_same_day_opposite_directions_without_order_is_unknown():
    rows = [{'trade_date': '2026-06-29', 'trade_type': 'buy'},
            {'trade_date': '2026-06-29', 'trade_type': 'sell'}]
    assert _trade_order_ambiguous(rows)
    rows[0]['trade_time'] = rows[1]['trade_time'] = '2026-06-29T09:30:00'
    assert _trade_order_ambiguous(rows)

