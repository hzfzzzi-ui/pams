from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.cashflow_fact import build_cashflow_fact, insert_cashflow_facts  # noqa: E402


class FakeInsertCursor:
    def __init__(self) -> None:
        self.source_hashes: set[str] = set()
        self.source_keys: set[tuple[str, str]] = set()
        self.rowcount = 0

    def execute(self, sql: str, params=None) -> None:
        assert "ON CONFLICT DO NOTHING" in sql
        assert params is not None
        source_hash = str(params["source_hash"])
        source_key = (str(params["source_table"]), str(params["source_id"]))
        if source_hash in self.source_hashes or source_key in self.source_keys:
            self.rowcount = 0
            return
        self.source_hashes.add(source_hash)
        self.source_keys.add(source_key)
        self.rowcount = 1


def _statement_row(**overrides):
    row = {
        "id": 1,
        "source_uid": "bank-row-001",
        "transaction_date": date(2026, 5, 1),
        "posting_date": date(2026, 5, 2),
        "account_code": "ccb_7332",
        "amount": Decimal("-500.00"),
        "currency": "CNY",
        "fx_rate_to_cny": Decimal("1"),
        "amount_cny": Decimal("-500.00"),
        "category": "unclassified",
        "subcategory": None,
        "summary": "bank transfer",
        "description": "",
        "counterparty": None,
        "raw_json": {},
    }
    row.update(overrides)
    return row


def test_credit_card_repayment_is_not_spending() -> None:
    fact = build_cashflow_fact(
        _statement_row(description="credit card repayment"),
        {
            "id": 10,
            "match_key": "repayment-1",
            "match_type": "credit_card_payment",
            "from_account_code": "ccb_7332",
            "to_account_code": "credit_card",
            "confidence": Decimal("0.97"),
        },
    )

    assert fact["cashflow_type"] == "debt_repayment"
    assert fact["affects_spending"] is False
    assert fact["affects_nav"] is False
    assert fact["counter_account_code"] == "credit_card"


def test_investment_funding_is_not_operating_expense() -> None:
    fact = build_cashflow_fact(
        _statement_row(id=2, source_uid="bank-row-002", amount=Decimal("-1000"), amount_cny=Decimal("-1000")),
        {
            "id": 11,
            "match_key": "funding-1",
            "match_type": "investment_funding",
            "from_account_code": "ccb_7332",
            "to_account_code": "broker",
            "confidence": Decimal("0.98"),
        },
    )

    assert fact["cashflow_type"] == "investment_funding"
    assert fact["affects_spending"] is False
    assert fact["affects_investment_capital"] is True
    assert fact["counter_account_code"] == "broker"


def test_cashflow_source_hash_uses_source_uid_before_row_id() -> None:
    first = build_cashflow_fact(_statement_row(id=3, source_uid="stable-import-row"))
    replay = build_cashflow_fact(_statement_row(id=300, source_uid="stable-import-row"))

    assert first["source_hash"] == replay["source_hash"]
    assert first["source_id"] != replay["source_id"]


def test_insert_cashflow_facts_is_idempotent_by_source_hash() -> None:
    first = build_cashflow_fact(_statement_row(id=4, source_uid="same-raw-row"))
    replay = build_cashflow_fact(_statement_row(id=400, source_uid="same-raw-row"))
    cur = FakeInsertCursor()

    assert insert_cashflow_facts(cur, [first, replay]) == 1
    assert insert_cashflow_facts(cur, [first]) == 0
    assert len(cur.source_hashes) == 1
