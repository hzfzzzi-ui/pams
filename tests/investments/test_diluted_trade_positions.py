from decimal import Decimal
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.investments.diluted_trade_positions import calculate_diluted_positions


def trade(ident, side, quantity, net_cash):
    return {'trade_id': ident, 'account_code': 'test', 'symbol': '600001', 'currency': 'CNY',
            'trade_date': f'2026-01-{ident:02}', 'trade_time': f'2026-01-{ident:02}T10:00:00',
            'side': side, 'quantity': quantity, 'net_cash': net_cash}


def test_sell_proceeds_dilute_remaining_cost():
    row = calculate_diluted_positions([trade(1, 'buy', '200', '-2000'), trade(2, 'sell', '100', '1200')])[0]
    assert row['quantity'] == Decimal('100')
    assert row['diluted_cost'] == Decimal('800')
    assert row['diluted_cost_price'] == Decimal('8')
    assert row['remaining_cost'] == Decimal('1000')
    assert row['realized_trade_pnl'] == Decimal('200')
    assert row['trade_ids'] == [1, 2]


def test_buy_uses_net_cash_including_fee_and_full_exit_resets_basis():
    rows = [trade(1, 'buy', '100', '-1004'), trade(2, 'sell', '100', '1200'), trade(3, 'buy', '100', '-1105')]
    row = calculate_diluted_positions(rows)[0]
    assert row['diluted_cost'] == Decimal('1105')
    assert row['trade_ids'] == [3]
    assert row['all_event_ids'] == [1, 2, 3]


def test_negative_diluted_cost_is_not_silently_clamped():
    row = calculate_diluted_positions([trade(1, 'buy', '200', '-2000'), trade(2, 'sell', '100', '2500')])[0]
    assert row['diluted_cost'] == Decimal('-500')


def test_dividend_reduces_and_separate_tax_increases_cost_once():
    rows = [trade(1, 'buy', '200', '-2000'), trade(2, 'sell', '100', '1200'),
            trade(3, 'dividend', None, '30'), trade(4, 'tax', None, '-5')]
    row = calculate_diluted_positions(rows)[0]
    assert row['diluted_cost'] == Decimal('775')
    assert row['diluted_cost_price'] == Decimal('7.75')
    assert row['remaining_cost'] == Decimal('1000')
    assert row['realized_trade_pnl'] == Decimal('200')
    assert row['net_distributions'] == Decimal('25')
    market_value = Decimal('1500')
    assert (market_value - row['remaining_cost'] + row['realized_trade_pnl'] + row['net_distributions']
            == market_value - row['diluted_cost'])


def test_weighted_average_after_multiple_buys_and_partial_sale():
    rows = [trade(1, 'buy', '100', '-1004'), trade(2, 'buy', '100', '-1105'),
            trade(3, 'sell', '50', '600')]
    row = calculate_diluted_positions(rows)[0]
    assert row['remaining_cost'] == Decimal('1581.75')
    assert row['realized_trade_pnl'] == Decimal('72.75')
    assert row['diluted_cost'] == Decimal('1509')


def test_rejects_different_actions_at_the_same_unknown_ordering():
    buy, sell = trade(1, 'buy', '200', '-2000'), trade(2, 'sell', '100', '1200')
    sell['trade_date'], sell['trade_time'] = buy['trade_date'], buy['trade_time']
    with pytest.raises(ValueError, match='ordering'):
        calculate_diluted_positions([buy, sell])


def test_post_exit_tax_is_reported_separately_and_never_added_to_a_new_position():
    rows = [trade(1, 'buy', '100', '-1000'), trade(2, 'sell', '100', '1200'),
            trade(3, 'tax', None, '-5'), trade(4, 'buy', '100', '-1100')]
    closed = []
    row = calculate_diluted_positions(rows, closed_adjustments=closed)[0]
    assert row['diluted_cost'] == Decimal('1100')
    assert closed[0]['trade_id'] == 3
    with pytest.raises(ValueError, match='without an open position'):
        calculate_diluted_positions(rows)


@pytest.mark.parametrize('rows', [[trade(1, 'sell', '1', '10')],
                                 [trade(1, 'buy', '1', None)],
                                 [trade(True, 'buy', '1', '-10')],
                                 [trade(1, 'buy', '1', '-10'), trade(1, 'buy', '1', '-10')],
                                 [trade(1, 'dividend', None, '10')],
                                 [trade(1, 'buy', '1', '-10'), trade(2, 'tax', None, '5')]])
def test_rejects_incomplete_or_ambiguous_inputs(rows):
    with pytest.raises(ValueError):
        calculate_diluted_positions(rows)
