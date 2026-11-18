from datetime import date
from decimal import Decimal as D
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))

from domain.investments import holding_market_history as history
from domain.market import benchmark_history as benchmark


def test_personal_units_are_valued_with_observed_prices_and_account_trades(monkeypatch):
    first, second = date(2026, 1, 5), date(2026, 1, 6)
    key = ('shared', 'CNY', 'TEST')
    anchor = {'quantity': D(100), 'source_item_id': 1, 'current_price': D(10)}
    monkeypatch.setattr(history, '_load_positions', lambda *args: (
        {key: {first: anchor}}, {key: 'stock'}, {('shared', 'CNY', 'asset'): key}))
    marks = {('stock', 'TEST', None): ([first, second], [D(10), D(12)])}
    monkeypatch.setattr(history, '_load_prices', lambda *args: marks)
    sale = dict(trade_date=second, trade_type='sell', source_evidence_status='verified',
                quantity=D(20), price=D(12), trade_id=5, net_cash=D(240))
    monkeypatch.setattr(history, 'load_trade_rows', lambda *args, **kwargs: [sale])
    result = history.holding_history(None, [('shared', 'asset')], first, second, lambda _: D('.5'))
    assert [D(r['value']) for r in result['rows']] == [D(500), D(480)]
    assert result['trades'][0]['quantity'] == '20'
    assert result['trades'][0]['date'] == second.isoformat()


def test_benchmark_fills_missing_session_and_preserves_local_close(monkeypatch):
    first, second = date(2026, 1, 5), date(2026, 1, 6)
    monkeypatch.setattr(benchmark, '_completed_history', lambda *args: [
        {'date': first, 'close': D(3001)}, {'date': second, 'close': D(3010)}])
    result = benchmark.benchmark_history({first.isoformat(): '3000'}, first, second, date(2026, 1, 7))
    assert [row['close'] for row in result['rows']] == ['3000', '3010']


def test_complete_trade_list_keeps_chart_window_and_source_verification(monkeypatch):
    from datetime import timedelta

    day = date(2026, 8, 5)
    key = ('account', 'CNY', 'TEST')
    anchor = {'quantity': D(10), 'source_item_id': 1, 'current_price': D(12)}
    monkeypatch.setattr(history, '_load_positions', lambda *args: (
        {key: {day: anchor}}, {key: 'stock'}, {('account', 'CNY', 'asset'): key}))
    monkeypatch.setattr(history, '_load_prices', lambda *args: {})
    facts = [dict(trade_date=day + timedelta(days=offset), trade_type='buy',
                  source_evidence_status=status, quantity=D(2), price=D(12),
                  trade_id=i, net_cash=D(-24))
             for i, (offset, status) in enumerate([
                 (-100, 'verified'), (-2, 'unknown'), (0, 'verified'), (1, 'verified')])]
    monkeypatch.setattr(history, 'load_trade_rows', lambda cur, **query: [
        row for row in facts if query['start'] <= row['trade_date'] <= query['end']])
    args = (None, [('account', 'asset')], day, day, lambda _: D(1))
    chart = history.holding_history(*args)
    complete = history.holding_history(*args, all_trades=True)
    assert [t['id'] for t in chart['trades']] == [2]
    assert [t['id'] for t in complete['trades']] == [0, 2, 3]
    assert complete['rows'] == chart['rows']


def test_holding_chart_supports_full_leap_year_and_rejects_larger_or_reversed_ranges():
    from domain.market.price_history import valid_holding_history_window

    start = date(2024, 1, 1)
    end = date(2024, 12, 31)
    assert valid_holding_history_window(start, start)
    assert valid_holding_history_window(start, end)
    assert not valid_holding_history_window(start, date(2025, 1, 1))
    assert not valid_holding_history_window(end, start)


def test_price_comparison_uses_common_date_and_is_independent_of_quantity(monkeypatch):
    first, second, third = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    key = ('account', 'CNY', 'TEST')
    monkeypatch.setattr(history, '_load_positions', lambda *args: (
        {key: {}}, {key: 'stock'}, {('account', 'CNY', 'asset'): key}))
    monkeypatch.setattr(history, '_load_prices', lambda *args: {
        ('stock', 'TEST', None): ([first, second, third], [D(10), D(12), D(11)])})
    result = history.holding_price_comparison(
        None, [('account', 'asset')], first, third,
        {second.isoformat(): D(3000), third.isoformat(): D(3300)})
    assert result['base_date'] == second.isoformat()
    assert D(result['rows'][0]['asset_return_pct']) == 0
    assert D(result['rows'][1]['asset_return_pct']).quantize(D('.01')) == D('-8.33')
    assert D(result['rows'][1]['benchmark_return_pct']) == D(10)
