from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.accounts import sync_status


def test_verified_cashflow_does_not_initialize_balance():
    imported = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    day = date(2026, 11, 1)
    cashflow = sync_status._activity([
        {'source_evidence_status': 'verified', 'transaction_date': day, 'created_at': imported},
        {'source_evidence_status': 'unknown', 'transaction_date': date(2026, 11, 2)},
    ], 'transaction_date')
    balance = sync_status._balance(None, [], {'manual_balance': {'as_of': '2026-06-21'}})
    assert cashflow == {'status': 'recorded', 'count': 1, 'as_of': day, 'synced_at': imported,
                        'has_unverified': True}
    assert balance == {'status': 'uninitialized', 'as_of': None, 'synced_at': None,
                       'historical_as_of': '2026-06-21'}


def test_balance_requires_one_verified_member(monkeypatch):
    day = date(2026, 11, 1)
    published = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    row = {'id': 1, 'source_item_id': 'source-item', 'as_of': day, 'published_at': published,
           'market_value': Decimal('18971.58')}
    monkeypatch.setattr(sync_status, '_complete_member_proven', lambda *args: False)
    assert sync_status._balance(None, [row], {})['status'] == 'review'
    monkeypatch.setattr(sync_status, '_complete_member_proven', lambda *args: True)
    assert sync_status._balance(None, [row], {}) == {
        'status': 'recorded', 'as_of': day, 'amount': '18971.58', 'synced_at': published, 'historical_as_of': None,
    }
    assert sync_status._balance(None, [row, dict(row, id=2)], {})['status'] == 'review'



def test_manual_position_value_keeps_asset_and_debt_dates_separate(monkeypatch):
    asset_day = date(2026, 7, 31)
    debt_day = date(2026, 11, 13)
    rows = [
        {'id': 1, 'as_of': asset_day, 'currency': 'CNY', 'asset_liability': '资产',
         'market_value': Decimal('706500.00')},
        {'id': 2, 'as_of': debt_day, 'currency': 'CNY', 'asset_liability': '负债',
         'market_value': Decimal('310000.00')},
    ]

    class Cursor:
        def execute(self, query, params):
            assert params == ('manual_broker', debt_day)

        def fetchall(self):
            return rows

    monkeypatch.setattr(sync_status, 'current_holding_source', lambda cur, ident: {'status': 'recorded'})
    assert sync_status._manual_position_value(Cursor(), 'manual_broker', debt_day) == {
        'balance_amount': '396500.00', 'balance_currency': 'CNY', 'balance_label': '参考净值',
        'position_value': '706500.00', 'position_as_of': asset_day,
        'position_debt': '310000.00', 'position_debt_as_of': debt_day,
    }
    monkeypatch.setattr(sync_status, 'current_holding_source', lambda cur, ident: {
        'status': 'recorded' if ident == 1 else 'unknown',
    })
    assert sync_status._manual_position_value(Cursor(), 'manual_broker', debt_day) is None


def test_fund_platform_shows_dated_reference_until_recorded_valuation(monkeypatch):
    today = date(2026, 11, 15)
    old = date(2026, 6, 2)
    cfg = {
        'account_type': 'fund_platform', 'truth_source': 'manual', 'reconcile_method': 'position_nav',
        'owner_ratio': '0.5', 'manual_balance': {'amount': '1200.00', 'as_of': old},
    }
    config = {'accounts': {'fund': cfg}}
    monkeypatch.setattr(sync_status, '_manual_position_value', lambda *args: None)
    reference = sync_status._position_balance_summary(None, config, 'fund', cfg, today)
    assert reference == {
        'balance_amount': '600.000', 'balance_currency': 'CNY', 'balance_label': '历史参考余额',
        'balance_basis': 'manual_position_reference', 'balance_as_of': old,
    }
    position = {'balance_amount': '710.00', 'position_as_of': today}
    monkeypatch.setattr(sync_status, '_manual_position_value', lambda *args: position)
    assert sync_status._position_balance_summary(None, config, 'fund', cfg, today) == {
        **position, 'balance_amount': '355.000', 'balance_as_of': today,
    }
    position['position_as_of'] = date(2026, 6, 1)
    assert sync_status._position_balance_summary(None, config, 'fund', cfg, today) == reference
    monkeypatch.setattr(sync_status, '_manual_position_value', lambda *args: None)
    for bad in ({'amount': 'NaN', 'as_of': old}, {'amount': '100'}, {'amount': '100', 'as_of': '2026-11-16'}):
        cfg['manual_balance'] = bad
        assert sync_status._position_balance_summary(None, config, 'fund', cfg, today) is None
