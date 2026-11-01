from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.accounts.accounts_view import build_account_detail_payload, build_accounts_payload  # noqa: E402
from endpoints.accounts import _pick_primary_balance, _primary_platform_balance, _reconcile_status  # noqa: E402


class FakeCursor:
    def __init__(self, results):
        self.results = list(results) if isinstance(results, list) else results
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.results


class SequenceCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_build_accounts_payload_active_true_preserves_fields():
    cur = FakeCursor(
        [
            {
                "account_code": "ibkr",
                "display_name": "IBKR",
                "base_currency": "USD",
                "is_asset": True,
            }
        ]
    )

    payload = build_accounts_payload(cur, active=True)

    assert payload == {
        "accounts": [
            {
                "account_code": "ibkr",
                "display_name": "IBKR",
                "base_currency": "USD",
                "is_asset": True,
            }
        ]
    }
    assert "WHERE active = true" in cur.executed[0][0]
    assert "active" not in payload["accounts"][0]


def test_build_accounts_payload_active_false_includes_active_field():
    cur = FakeCursor(
        [
            {
                "account_code": "old",
                "display_name": "Old Account",
                "base_currency": "CNY",
                "is_asset": True,
                "active": False,
            }
        ]
    )

    payload = build_accounts_payload(cur, active=False)

    assert payload["accounts"][0]["active"] is False
    assert "WHERE active = true" not in cur.executed[0][0]
    assert "ORDER BY display_name" in cur.executed[0][0]


def test_build_accounts_payload_handles_empty_result():
    assert build_accounts_payload(FakeCursor([]), active=True) == {"accounts": []}


def test_build_account_detail_payload_preserves_shape_with_stats(monkeypatch):
    from domain.investments import snapshot_coverage
    monkeypatch.setattr(snapshot_coverage, "load_account_snapshot_coverage", lambda cur, keys: {key: {"status": "complete"} for key in keys})
    cur = SequenceCursor(
        [
            {
                "account_code": "tonghuashun",
                "display_name": "同花顺",
                "base_currency": "CNY",
                "is_asset": True,
                "active": True,
                "created_at": "2026-01-01",
            },
            [{"holdings_count": 3, "market_value": "123.45", "missing_values": 0,
              "currency": "CNY", "as_of": date(2026, 1, 2)}],
        ]
    )

    payload = build_account_detail_payload(cur, account_code="tonghuashun")

    assert payload["account_code"] == "tonghuashun"
    assert payload["holdings_count"] == 3
    assert payload["total_value"] == "123.45"
    assert cur.executed[0][1] == ("tonghuashun",)
    assert cur.executed[1][1][1] == "tonghuashun"


def test_build_account_detail_payload_keeps_missing_total_unknown():
    cur = SequenceCursor(
        [
            {
                "account_code": "empty",
                "display_name": "Empty",
                "base_currency": "CNY",
                "is_asset": True,
                "active": True,
                "created_at": "2026-01-01",
            },
            [],
        ]
    )

    payload = build_account_detail_payload(cur, account_code="empty")

    assert payload["total_value"] is None
    assert payload["total_value_status"] == "unknown"


def test_build_account_detail_payload_returns_none_for_missing_account():
    assert build_account_detail_payload(SequenceCursor([None]), account_code="missing") is None


def test_bank_statement_account_can_use_manual_balance_as_reviewable_fallback():
    status = _reconcile_status(
        account_type="funding",
        truth_source="bank_statement",
        statement_balance=None,
        platform_balance=None,
        manual_balance={"amount": 0.63, "currency": "CNY", "as_of": "2026-06-21"},
    )

    assert status == "ready_for_review"


def test_primary_platform_balance_converts_net_multi_currency_platform_snapshot():
    rows = [
        {"currency": "USD", "market_value": "52609.1600", "as_of": date(2026, 6, 21)},
        {"currency": "USD", "market_value": "-20101.1604", "as_of": date(2026, 6, 21)},
        {"currency": "HKD", "market_value": "28720.0000", "as_of": date(2026, 6, 21)},
        {"currency": "HKD", "market_value": "-14795.5133", "as_of": date(2026, 6, 21)},
        {"currency": "KRW", "market_value": "21240000.0000", "as_of": date(2026, 6, 21)},
        {"currency": "KRW", "market_value": "-16512617.2200", "as_of": date(2026, 6, 21)},
        {"currency": "MXN", "market_value": "108999.9998", "as_of": date(2026, 6, 21)},
        {"currency": "SEK", "market_value": "-4.0000", "as_of": date(2026, 6, 21)},
    ]
    fx = {'2026-06-21': {
        'USD': Decimal('7'), 'HKD': Decimal('0.893116'), 'KRW': Decimal('0.004571'),
        'MXN': Decimal('0.403858'), 'SEK': Decimal('0.56'),
    }}
    balance = _primary_platform_balance(rows, "USD", fx)
    expected_cny = (Decimal('32507.9996') * Decimal('7')
        + Decimal('13924.4867') * Decimal('0.893116')
        + Decimal('4727382.7800') * Decimal('0.004571')
        + Decimal('108999.9998') * Decimal('0.403858') - Decimal('4') * Decimal('0.56'))
    assert balance is not None
    assert balance['currency'] == 'USD' and balance['as_of'] == date(2026,6,21)
    assert balance['amount_cny'] == expected_cny
    assert balance['amount'] == expected_cny / Decimal('7')


def test_primary_platform_balance_never_drops_currency_when_fx_missing():
    rows = [
        {'currency':'USD','market_value':'100','as_of':date(2026,6,21)},
        {'currency':'SEK','market_value':'4','as_of':date(2026,6,21)},
    ]
    assert _primary_platform_balance(rows, 'USD', {'2026-06-21':{'USD':Decimal('7')}}) is None
    # A quote for a different day cannot fill a gap in the business date.
    assert _primary_platform_balance(rows, 'USD', {'2026-06-22':{'USD':Decimal('7'),'SEK':Decimal('1')}}) is None
    known_native = _primary_platform_balance(rows[:1], 'USD', {})
    assert known_native['amount'] == 100 and known_native['amount_cny'] is None



def test_primary_platform_balance_rejects_missing_values_and_mixed_dates():
    assert _primary_platform_balance([{'currency':'CNY','market_value':None,'as_of':date(2026,6,21)}], 'CNY', {}) is None
    assert _primary_platform_balance([
        {'currency':'USD','market_value':'100','as_of':date(2026,6,21)},
        {'currency':'EUR','market_value':'1','as_of':date(2026,6,22)},
    ], 'USD', {}) is None


def test_owner_ratio_scales_proven_cny_and_native_amount_together():
    from endpoints.accounts import _apply_owner_ratio_to_money
    balance={'currency':'USD','amount':Decimal('200'),'amount_cny':Decimal('1400')}
    adjusted=_apply_owner_ratio_to_money(balance,Decimal('0.5'))
    assert adjusted['amount']==100 and adjusted['amount_cny']==700
    assert balance['amount_cny']==1400



def test_primary_platform_balance_fx_direction_and_negative_cash():
    day = date(2026, 6, 21)
    rows = [{'currency':'USD','market_value':'100','as_of':day},
            {'currency':'EUR','market_value':'-30','as_of':day},
            {'currency':'GBP','market_value':'0','as_of':day}]
    rates = {'2026-06-21':{'USD':Decimal('7'),'EUR':Decimal('8')}}
    usd = _primary_platform_balance(rows,'USD',rates)
    cny = _primary_platform_balance(rows,'CNY',rates)
    assert usd['amount_cny']==Decimal('460')
    assert usd['amount']==Decimal('460') / Decimal('7')
    assert cny['amount']==cny['amount_cny']==Decimal('460')
    assert _primary_platform_balance(rows,'EUR',rates)['amount']==Decimal('460')/Decimal('8')



def test_pick_primary_balance_prefers_latest_platform_snapshot_for_position_nav_account():
    balance = _pick_primary_balance(
        truth_source="manual",
        account_type="fund_platform",
        reconcile_method="position_nav",
        statement_balance=None,
        platform_balance={"amount": Decimal("27942.27"), "currency": "CNY", "as_of": date(2026, 6, 21)},
        manual_balance={"amount": Decimal("22488.13"), "currency": "CNY", "as_of": "2026-06-02"},
    )

    assert balance is not None
    assert balance["amount"] == Decimal("27942.27")


def test_pick_primary_balance_prefers_same_day_manual_total_assets_for_position_nav_account():
    balance = _pick_primary_balance(
        truth_source="platform_snapshot",
        account_type="brokerage",
        reconcile_method="position_nav",
        statement_balance=None,
        platform_balance={"amount": Decimal("242225.00"), "currency": "CNY", "as_of": "2026-06-21"},
        manual_balance={"amount": Decimal("247519.60"), "currency": "CNY", "as_of": "2026-06-21"},
    )

    assert balance is not None
    assert balance["amount"] == Decimal("247519.60")
