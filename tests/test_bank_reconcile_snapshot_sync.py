from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints.bank_reconcile import _statement_snapshot_payload, _sync_holding_snapshot_from_statement  # noqa: E402
from services.portfolio_state import _asset_type  # noqa: E402


def test_statement_snapshot_payload_uses_latest_provident_fund_balance() -> None:
    result = {
        "account_code": "hpf",
        "account_kind": "provident_fund",
        "latest_balance": Decimal("17940.63"),
        "latest_balance_date": date(2026, 5, 26),
        "rows": [{"currency": "CNY"}],
    }
    profile = {
        "account_code": "hpf",
        "currency": "CNY",
        "pams_target": {
            "account_code": "hpf",
            "display_name": "深圳公积金",
            "is_asset": True,
            "snapshot": {
                "asset_liability": "资产",
                "major_category": "公积金",
                "minor_category": "住房公积金",
                "name": "深圳公积金账户",
            },
        },
    }

    payload = _statement_snapshot_payload(result, profile)

    assert payload is not None
    assert payload["account_code"] == "hpf"
    assert payload["name"] == "深圳公积金账户"
    assert payload["market_value"] == Decimal("17940.63")
    assert payload["cost"] == Decimal("17940.63")
    assert payload["as_of"] == date(2026, 5, 26)


def test_provident_fund_snapshot_classifies_as_cash_like_asset() -> None:
    assert (
        _asset_type(
            {
                "asset_liability": "资产",
                "major_category": "公积金",
                "minor_category": "住房公积金",
            }
        )
        == "cash"
    )


class _SnapshotSyncCursor:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[object] = []
        self.fetchone_row = None

    def execute(self, query: str, params=None) -> None:
        self.queries.append(query)
        self.params.append(params)
        if "to_regclass" in query:
            self.fetchone_row = {"table_name": None}

    def fetchone(self):
        return self.fetchone_row


def test_snapshot_sync_skips_missing_legacy_asset_category_table() -> None:
    cur = _SnapshotSyncCursor()
    result = {
        "account_code": "santander",
        "account_kind": "debit",
        "latest_balance": Decimal("527.49"),
        "latest_balance_date": date(2026, 6, 20),
        "rows": [{"currency": "MXN"}],
    }
    profile = {
        "account_code": "santander",
        "currency": "MXN",
        "pams_target": {
            "account_code": "santander",
            "display_name": "Santander4209海外储蓄卡",
            "is_asset": True,
            "snapshot": {
                "asset_liability": "资产",
                "major_category": "现金",
                "minor_category": "现金/活期",
                "name": "Santander4209海外储蓄卡现金",
            },
        },
    }

    payload = _sync_holding_snapshot_from_statement(cur, result, profile)

    assert payload is not None
    assert payload["market_value"] == Decimal("527.49")
    assert any("INSERT INTO pams_accounts" in query for query in cur.queries)
    assert any("SELECT to_regclass" in query for query in cur.queries)
    assert not any("INSERT INTO pams_asset_categories" in query for query in cur.queries)
    assert any("INSERT INTO pams_holdings_snapshot" in query for query in cur.queries)
