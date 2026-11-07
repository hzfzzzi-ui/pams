from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.accounts import balance_snapshot
from domain.investments import portfolio_state, valuation_nav
from domain.investments.portfolio_valuation_state import published_bank_balances_compatible
from endpoints.accounts import _build_accounts_overview


DAY = date(2026, 11, 1)
CONFIG = {'accounts': {
    'bank': {'account_type': 'funding', 'truth_source': 'bank_statement',
             'manual_balance': {'amount': '900', 'as_of': date(2026, 6, 21)}},
    'card': {'account_type': 'liability', 'truth_source': 'bank_statement'},
}}


def test_verified_cash_snapshot_is_shared_and_legacy_credit_card_is_untouched(monkeypatch):
    class Cursor:
        def execute(self, query, params):
            assert params == (['bank'], DAY)

        def fetchall(self):
            return [{'id': 12, 'account_code': 'bank', 'as_of': DAY, 'currency': 'CNY',
                     'market_value': Decimal('150')}]

    cur = Cursor()
    proof = {'status': 'partial', 'member_ids': {12}}
    monkeypatch.setattr(balance_snapshot, 'load_account_snapshot_coverage',
                        lambda *args: {('bank', DAY): proof})
    assert balance_snapshot.load_verified_bank_balances(cur, CONFIG, DAY) == {}
    proof['status'] = 'complete'
    snapshots = balance_snapshot.load_verified_bank_balances(cur, CONFIG, DAY)
    assert not balance_snapshot.prefer_verified_bank_balance(snapshots['bank'], {'as_of': date(2026, 11, 2)})

    monkeypatch.setattr(portfolio_state, '_view_as_of', lambda: DAY)
    monkeypatch.setattr(portfolio_state, '_latest_fx_rates', lambda *args: {'CNY': Decimal(1)})
    monkeypatch.setattr(portfolio_state, '_platform_balances', lambda *args: {})
    balances = {row['account_code']: row for row in portfolio_state.load_account_balances(cur, 'CNY', CONFIG)}
    overview = {row['account_code']: row for row in _build_accounts_overview(
        CONFIG, {}, {}, {}, {}, {}, {}, {}, verified_bank_balances=snapshots)}
    assert overview['bank']['primary_balance'] == snapshots['bank']
    assert balances['bank']['amount_cny'] == overview['bank']['primary_balance']['amount'] == Decimal('150')
    assert overview['card']['primary_balance'] is balances['card']['amount'] is None

    rows = list(balances.values())
    assert not published_bank_balances_compatible({'raw_json': {'account_values_cny': {'bank': '900'}}}, rows)
    assert published_bank_balances_compatible({'raw_json': {'account_values_cny': {'bank': '150'}}}, rows)


def test_nav_anchors_the_snapshot_day_and_uses_flows_in_each_direction():
    before, after = date(2026, 10, 31), date(2026, 11, 2)
    account = {'account_code': 'bank', 'owner_ratio': Decimal('0.5'), 'primary_balance': {
        'source': balance_snapshot.SOURCE, 'currency': 'CNY', 'amount': '150', 'as_of': DAY,
    }, 'manual_balance': {'as_of': date(2026, 6, 21), 'amount': '900', 'currency': 'CNY'}}
    flows = valuation_nav.StatementRowsByAccount()
    flows['bank'] = [{'transaction_date': DAY, 'currency': 'CNY', 'amount': '-40'},
                     {'transaction_date': after, 'currency': 'CNY', 'amount': '10'}]
    args = dict(accounts=[account], rows_by_account=flows, dates=[before, DAY, after],
                terminal_date=after, fx_timelines={}, base_currency='CNY')
    points = valuation_nav._bank_backcast_series(**args)
    assert [points[d]['net_worth'] for d in (before, DAY, after)] == [Decimal(170), Decimal(150), Decimal(155)]
    assert points[before]['estimated_accounts'] == points[after]['estimated_accounts'] == ['bank']
    assert 'estimated_accounts' not in points[DAY]
    flows.unknown('bank', 'missing_original')
    points = valuation_nav._bank_backcast_series(**args)
    assert points[DAY]['net_worth'] == Decimal(150)
    assert points[before]['unavailable_accounts'] == points[after]['unavailable_accounts'] == ['bank']
