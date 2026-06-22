from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.accounts_view import build_account_detail_payload, build_accounts_payload  # noqa: E402
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


def test_build_account_detail_payload_preserves_shape_with_stats():
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
            {"holdings_count": 3, "total_value": "123.45"},
        ]
    )

    payload = build_account_detail_payload(cur, account_code="tonghuashun")

    assert payload["account_code"] == "tonghuashun"
    assert payload["holdings_count"] == 3
    assert payload["total_value"] == "123.45"
    assert cur.executed[0][1] == ("tonghuashun",)
    assert cur.executed[1][1][1] == "tonghuashun"


def test_build_account_detail_payload_returns_zero_total_when_stats_value_missing():
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
            {"holdings_count": 0, "total_value": None},
        ]
    )

    payload = build_account_detail_payload(cur, account_code="empty")

    assert payload["total_value"] == "0"


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
    fx = {
        "USD": {
            "HKD": Decimal("0.127588"),
            "KRW": Decimal("0.000653"),
            "MXN": Decimal("0.057694"),
        }
    }

    balance = _primary_platform_balance(rows, "USD", fx)

    expected = (
        Decimal("32507.9996")
        + Decimal("13924.4867") * Decimal("0.127588")
        + Decimal("4727382.7800") * Decimal("0.000653")
        + Decimal("108999.9998") * Decimal("0.057694")
    )
    assert balance is not None
    assert balance["currency"] == "USD"
    assert balance["as_of"] == date(2026, 6, 21)
    assert Decimal(str(balance["amount"])).quantize(Decimal("0.01")) == expected.quantize(Decimal("0.01"))


def test_primary_platform_balance_falls_back_to_base_when_material_fx_is_missing():
    rows = [
        {"currency": "USD", "market_value": "100", "as_of": date(2026, 6, 21)},
        {"currency": "SEK", "market_value": "1000", "as_of": date(2026, 6, 21)},
    ]

    balance = _primary_platform_balance(rows, "USD", {"USD": {}})

    assert balance is not None
    assert balance["amount"] == Decimal("100")
    assert balance["currency"] == "USD"


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
