from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.ledger_view import _account_summary, _serialize_row, classify_ledger_row, summarize_period  # noqa: E402


def test_classify_transfer_before_expense_without_amount_threshold() -> None:
    row = {
        "id": 10,
        "amount_cny": -520,
        "category": "日常消费",
        "subcategory": "小额消费/红包",
        "description": "跨行代收 信用卡还款",
    }

    result = classify_ledger_row(row, transfer_row_ids=set())

    assert result["flow_type"] == "transfer"
    assert result["coarse_category"] == "资金流转"
    assert result["needs_review"] is False


def test_classify_expense_uses_coarse_merchant_category() -> None:
    row = {
        "id": 11,
        "amount_cny": -86.4,
        "category": "未分类",
        "summary": "信用卡交易",
        "description": "DLO*UBER RIDE CDMX MX",
    }

    result = classify_ledger_row(row)

    assert result["flow_type"] == "expense"
    assert result["coarse_category"] == "交通"
    assert result["needs_review"] is False


def test_classify_unmatched_unclear_expense_needs_review() -> None:
    row = {
        "id": 12,
        "amount_cny": -1000,
        "category": "未分类",
        "description": "未知商户",
    }

    result = classify_ledger_row(row)

    assert result["flow_type"] == "expense"
    assert result["coarse_category"] == "待确认支出"
    assert result["needs_review"] is True


def test_summarize_period_uses_one_way_transfer_matches() -> None:
    rows = [
        {"transaction_date_obj": date(2026, 5, 1), "amount_cny": 2000.0, "flow_type": "income", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 2), "amount_cny": -300.0, "flow_type": "expense", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 3), "amount_cny": -1000.0, "flow_type": "transfer", "needs_review": False},
        {"transaction_date_obj": date(2026, 5, 3), "amount_cny": 1000.0, "flow_type": "transfer", "needs_review": False},
    ]
    transfers = [{"match_date_obj": date(2026, 5, 3), "amount_cny": 1000.0}]

    summary = summarize_period(rows, transfers, date(2026, 5, 1), date(2026, 5, 31))

    assert summary["income"] == 2000.0
    assert summary["expense"] == 300.0
    assert summary["net"] == 1700.0
    assert summary["internal_transfer"] == 1000.0


def test_serialize_row_preserves_transfer_fields_for_frontend_filter() -> None:
    row = {
        "id": 21,
        "transaction_date": date(2026, 6, 21),
        "amount": -1000,
        "amount_cny": -1000,
        "flow_type": "transfer",
        "coarse_category": "资金流转",
        "needs_review": False,
        "cashflow_type": "investment_funding",
        "is_internal_transfer": True,
        "transfer_group_id": "case-1",
    }

    out = _serialize_row(row)

    assert out["cashflow_type"] == "investment_funding"
    assert out["is_internal_transfer"] is True
    assert out["transfer_group_id"] == "case-1"


def test_account_summary_groups_month_rows_by_account() -> None:
    rows = [
        {"account_code": "bank-a", "flow_type": "income", "amount_cny": 3000, "needs_review": False},
        {"account_code": "bank-a", "flow_type": "expense", "amount_cny": -800, "needs_review": True},
        {"account_code": "bank-a", "flow_type": "transfer", "amount_cny": -1000, "needs_review": False},
        {"account_code": "wallet-b", "flow_type": "expense", "amount_cny": -120, "needs_review": False},
    ]

    result = _account_summary(rows)
    by_account = {row["account_code"]: row for row in result}

    assert by_account["bank-a"]["income"] == 3000
    assert by_account["bank-a"]["expense"] == 800
    assert by_account["bank-a"]["transfer"] == 1000
    assert by_account["bank-a"]["net"] == 1200
    assert by_account["bank-a"]["row_count"] == 3
    assert by_account["bank-a"]["pending_count"] == 1
    assert by_account["wallet-b"]["expense"] == 120


def test_account_summary_reports_raw_row_activity() -> None:
    rows = [
        {"account_code": "ccb_7332", "flow_type": "transfer", "amount_cny": -1000, "needs_review": False},
        {"account_code": "ccb_7332", "flow_type": "transfer", "amount_cny": 300, "needs_review": False},
    ]

    result = _account_summary(rows)

    assert result[0]["account_code"] == "ccb_7332"
    assert result[0]["row_count"] == 2
    assert result[0]["transfer"] == 1300
    assert result[0]["net"] == -700
