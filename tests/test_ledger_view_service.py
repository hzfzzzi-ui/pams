from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.ledger_view import classify_ledger_row, summarize_period  # noqa: E402


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
