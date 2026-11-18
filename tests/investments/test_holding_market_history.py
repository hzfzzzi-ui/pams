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
