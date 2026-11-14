import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.period_trade_returns import (  # noqa: E402
    attach_closed_trade_returns,
    fund_holding_profit,
    summarize_closed_trades,
)


START = date(2026, 1, 1)
END = date(2026, 11, 14)
IDENTITY = 'SAMPLE-1'


def trade(identifier, day, kind, quantity, cash, *, account='tencent_licaitong', **overrides):
    return dict({
        'trade_id': identifier, 'trade_date': date.fromisoformat(day), 'trade_time': None,
        'trade_type': kind, 'quantity': quantity, 'net_cash': cash,
        'account_code': account, 'asset_id': IDENTITY, 'instrument_identity': '123',
        'symbol': IDENTITY, 'name': 'Sample', 'currency': 'CNY',
        'source_market': '上海Ａ股', 'listing_exchange': 'NASDAQ', 'source_evidence_status': 'verified',
    }, **overrides)


def summarize(rows, **kwargs):
    args = dict(start=START, end=END, owner_ratio=lambda _account: Decimal(1),
                fx_rate=lambda _row: Decimal(1), opening_positions=set(), final_positions={})
    return summarize_closed_trades(rows, **(args | kwargs))


def test_closed_fund_trade_without_a_holding_snapshot_reconciles_with_open_fund_profit():
    rows = [
        trade(1, '2026-01-22', 'fund_subscription', '24634.59', '-30000'),
        trade(2, '2026-03-31', 'fund_subscription', '8031.32', '-8000'),
        trade(3, '2026-07-13', 'fund_redemption', '32665.91', '50641.96'),
    ]
    cycle = summarize(rows)['a_fund'][0]['cycles'][0]
    assert cycle == {
        'open_date': date(2026, 1, 22), 'exit_date': date(2026, 7, 13),
        'trade_pnl': Decimal('12641.96'), 'cost': Decimal('38000'), 'distributions': Decimal(0),
    }
    holdings = {'rows': [
        {'market_value_cny': '52207.89', 'cost_cny': '62473.24'},
        {'market_value_cny': '0', 'cost_cny': '38000', 'is_closed': True},
    ]}
    assert fund_holding_profit(holdings, {'amount': '12641.96'}) == '2376.61'
    assert fund_holding_profit({'rows': [{'market_value_cny': '100'}]}, {'amount': '10'}) is None


@pytest.mark.parametrize(('account', 'section'), [('tonghuashun', 'a_stock'), ('ibkr', 'us_stock')])
def test_stock_cycles_use_net_cash_and_tax_once_then_apply_ownership_and_fx(account, section):
    rows = [
        trade(1, '2026-01-22', 'buy', 10, '-101', account=account, fee_amount=1),
        trade(2, '2026-07-10', 'dividend', None, '5', account=account),
        trade(3, '2026-07-11', 'tax', None, '-1', account=account),
        trade(4, '2026-07-13', 'sell', 10, '119', account=account, fee_amount=1),
    ]
    rates = {1: '1', 2: '2', 3: '2', 4: '2'}
    cycle = summarize(rows, owner_ratio=lambda _: Decimal('0.5'),
                      fx_rate=lambda row: Decimal(rates[row['trade_id']]))[section][0]['cycles'][0]
    assert cycle['trade_pnl'] == Decimal('68.5')
    assert cycle['distributions'] == Decimal('4')


@pytest.mark.parametrize('bad_input', ['incomplete_sell', 'unverified', 'missing_fx', 'opening_position',
                                       'same_day', 'contradictory_snapshot'])
def test_do_not_claim_a_complete_trade_cycle_from_ambiguous_evidence(bad_input):
    buy = trade(1, '2026-01-22', 'fund_subscription', '10', '-10')
    sell = trade(2, '2026-07-13', 'fund_redemption', '10', '15')
    key = ('tencent_licaitong', IDENTITY, 'CNY')
    kwargs = {}
    if bad_input == 'incomplete_sell':
        sell['quantity'] = '9'
    elif bad_input == 'unverified':
        sell['source_evidence_status'] = 'unknown'
    elif bad_input == 'missing_fx':
        kwargs['fx_rate'] = lambda _: None
    elif bad_input == 'opening_position':
        kwargs['opening_positions'] = {key}
    elif bad_input == 'same_day':
        sell['trade_date'] = buy['trade_date']
    elif bad_input == 'contradictory_snapshot':
        kwargs['final_positions'] = {key: (END, Decimal('1'))}
    assert summarize([buy, sell], **kwargs) == {}


def test_ytd_does_not_treat_a_prior_year_buy_as_current_year_cost():
    assert summarize([
        trade(1, '2025-12-02', 'fund_subscription', '10', '-10'),
        trade(2, '2026-07-13', 'fund_redemption', '10', '15'),
    ]) == {}


def test_lifetime_returns_include_closed_cycles_from_prior_years():
    trades = [
        trade(1, '2024-12-02', 'fund_subscription', '10', '-10'),
        trade(2, '2025-07-13', 'fund_redemption', '10', '15'),
        trade(3, '2026-01-22', 'fund_subscription', '10', '-20'),
        trade(4, '2026-07-13', 'fund_redemption', '10', '23'),
    ]
    cycles = summarize(trades, start=date.min)['a_fund'][0]['cycles']
    assert [cycle['trade_pnl'] for cycle in cycles] == [Decimal('5'), Decimal('3')]


def test_keep_verified_ibkr_returns_and_withhold_unverified_fund_rate():
    closed = {'amount': '12641.96', 'status': 'partial'}
    ibkr = {'section': 'us_stock', 'status': 'available',
            'summary': {'total_return': 30, 'total_return_rate': 0.3},
            'rows': [{'date': '2026-07-13', 'display_return_rate': 0.3}]}
    assert attach_closed_trade_returns(ibkr, closed) == {**ibkr, 'closed_trade_returns': closed}
    fund = attach_closed_trade_returns({**ibkr, 'section': 'a_fund'}, closed)
    assert fund['status'] == 'unavailable'
    assert fund['summary']['total_return'] is None
    assert fund['rows'][0]['display_return_rate'] is None
    assert fund['closed_trade_returns'] == closed
