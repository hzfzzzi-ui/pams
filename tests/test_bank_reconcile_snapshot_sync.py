from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints.bank_reconcile import _statement_snapshot_payload  # noqa: E402
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
