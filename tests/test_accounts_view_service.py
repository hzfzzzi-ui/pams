from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.accounts_view import build_account_detail_payload, build_accounts_payload  # noqa: E402
from endpoints.accounts import _reconcile_status  # noqa: E402


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
