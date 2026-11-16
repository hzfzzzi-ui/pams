"""Exercise campaign history using synthetic trade facts."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.investments.trade_campaigns import build_position_campaigns_from_rows  # noqa: E402


def trade(trade_id, day, side, quantity, gross, **extra):
    return {'trade_id': trade_id, 'trade_date': date(2025, 1, day),
            'account_code': 'synthetic', 'symbol': 'TEST', 'name': 'Example',
            'currency': 'CNY', 'side': side, 'trade_type': side,
            'quantity': None if quantity is None else Decimal(quantity),
            'gross_amount': Decimal(gross), 'fee_amount': Decimal('0'),
            'tax_amount': Decimal('0'), **extra}


def test_fifo_partial_sale_preserves_open_lot_and_realized_pnl():
    rows = [trade(1, 1, 'buy', '4', '40'), trade(2, 2, 'buy', '6', '72'),
            trade(3, 3, 'sell', '5', '75')]
    campaign = build_position_campaigns_from_rows(rows)[0]
    assert campaign['status'] == 'open'
    assert campaign['open_quantity'] == '5.000000'
    assert campaign['open_cost_basis'] == '60.00'
    assert campaign['realized_pnl_estimated'] == '23.00'
    assert [item['buy_trade_id'] for item in campaign['lot_matches']] == [1, 2]
    assert [item['quantity'] for item in campaign['lot_matches']] == ['4.000000', '1.000000']


def test_reopening_after_full_exit_starts_a_new_campaign():
    rows = [trade(1, 1, 'buy', '4', '40'), trade(2, 2, 'sell', '4', '48'),
            trade(3, 3, 'buy', '2', '24')]
    latest, closed = build_position_campaigns_from_rows(rows)
    assert latest['status'] == 'open' and latest['start_date'] == '2025-01-03'
    assert closed['status'] == 'closed' and closed['end_date'] == '2025-01-02'
    assert closed['realized_pnl_estimated'] == '8.00'


def test_amount_only_fund_does_not_invent_lot_matches():
    rows = [trade(1, 1, 'buy', None, '40', trade_type='fund_subscription'),
            trade(2, 2, 'sell', None, '45', trade_type='fund_redemption')]
    campaign = build_position_campaigns_from_rows(rows)[0]
    assert campaign['status'] == 'amount_only'
    assert campaign['amount_only_trade_count'] == 2
    assert campaign['lot_matches'] == []
    assert campaign['warnings']
