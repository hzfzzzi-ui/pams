from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.cashflow_fact import build_cashflow_fact, fetch_cashflow_source_rows, insert_cashflow_facts  # noqa: E402


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


class FakeFetchCursor:
    def __init__(self) -> None:
        self.last_sql = ""
        self.fetchall_rows: list[dict] = []

    def execute(self, sql: str, params=None) -> None:
        self.last_sql = sql

    def fetchall(self):
        return self.fetchall_rows


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


def test_cashflow_source_rows_skip_reimported_source_uid() -> None:
    cur = FakeFetchCursor()

    assert fetch_cashflow_source_rows(cur) == []
    assert "f.source_uid = r.source_uid" in cur.last_sql


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


def test_source_only_investment_transfer_is_pending_internal_flow() -> None:
    fact = build_cashflow_fact(
        _statement_row(
            id=20,
            source_uid="santander-ibkr-pending",
            account_code="santander",
            amount=Decimal("-29000"),
            currency="MXN",
            amount_cny=Decimal("-14500"),
            summary="TRANSFERENCIA A INTERACTIVE BROKERS LLC",
            description="TRANSFERENCIA A INTERACTIVE BROKERS LLC",
        ),
        {
            "id": 20,
            "match_key": "pending-ibkr",
            "match_type": "account_transfer",
            "from_account_code": "santander",
            "to_account_code": "ibkr",
            "source_row_id": 20,
            "target_row_id": None,
            "confidence": Decimal("0.85"),
        },
    )

    assert fact["cashflow_type"] == "internal_transfer"
    assert fact["affects_spending"] is False
    assert fact["affects_investment_capital"] is False
    assert fact["is_internal_transfer"] is True
    assert fact["counter_account_code"] == "ibkr"


def test_payment_proxy_target_side_is_operating_expense() -> None:
    fact = build_cashflow_fact(
        _statement_row(
            id=486,
            source_uid="wechat-payment-row",
            account_code="wechat",
            amount=Decimal("-441"),
            amount_cny=Decimal("-441"),
            summary="群收款",
            description="🥇yi 建设银行储蓄卡(7332) 支付成功",
            counterparty="🥇yi",
        ),
        {
            "id": 12,
            "match_key": "payment-proxy-1",
            "match_type": "payment_proxy",
            "from_account_code": "ccb_7332",
            "to_account_code": "wechat_balance",
            "source_row_id": 1132,
            "target_row_id": 486,
            "confidence": Decimal("0.96"),
        },
    )

    assert fact["cashflow_type"] == "operating_expense"
    assert fact["affects_spending"] is True
    assert fact["is_internal_transfer"] is False
    assert fact["counter_account_code"] is None


def test_payment_proxy_source_side_is_only_for_deduplication() -> None:
    fact = build_cashflow_fact(
        _statement_row(
            id=1132,
            source_uid="ccb-payment-funding-row",
            account_code="ccb_7332",
            amount=Decimal("-441"),
            amount_cny=Decimal("-441"),
            summary="充值",
            description="充值 财付通-微信支付-群收款",
        ),
        {
            "id": 12,
            "match_key": "payment-proxy-1",
            "match_type": "payment_proxy",
            "from_account_code": "ccb_7332",
            "to_account_code": "wechat_balance",
            "source_row_id": 1132,
            "target_row_id": 486,
            "confidence": Decimal("0.96"),
        },
    )

    assert fact["cashflow_type"] == "internal_transfer"
    assert fact["affects_spending"] is False
    assert fact["is_internal_transfer"] is True
    assert fact["counter_account_code"] == "wechat_balance"


def test_personal_bridge_transfer_requires_transfer_match() -> None:
    zhao = build_cashflow_fact(
        _statement_row(
            id=30,
            source_uid="santander-zhao-guanyu",
            account_code="santander",
            amount=Decimal("-40000"),
            currency="MXN",
            amount_cny=Decimal("-16064"),
            summary="TRANSFERENCIA A ZHAO GUANYU",
            description="TRANSFERENCIA A ZHAO GUANYU",
            counterparty="TRANSFERENCIA A ZHAO GUANYU",
        )
    )
    huang = build_cashflow_fact(
        _statement_row(
            id=31,
            source_uid="ccb-huang-xingyu",
            amount=Decimal("-15547.79"),
            amount_cny=Decimal("-15547.79"),
            summary="跨行转出",
            description="跨行转出 跨行转出 6214857602508493/黄星宇",
            counterparty="6214857602508493/黄星宇",
        )
    )

    assert zhao["cashflow_type"] == "operating_expense"
    assert huang["cashflow_type"] == "operating_expense"
    assert zhao["affects_spending"] is True
    assert huang["affects_spending"] is True


def test_confirmed_peer_fx_transfer_is_not_spending() -> None:
    fact = build_cashflow_fact(
        _statement_row(
            id=30,
            source_uid="santander-zhao-guanyu",
            account_code="santander",
            amount=Decimal("-40000"),
            currency="MXN",
            amount_cny=Decimal("-16064"),
            summary="TRANSFERENCIA A ZHAO GUANYU",
            description="TRANSFERENCIA A ZHAO GUANYU",
            counterparty="TRANSFERENCIA A ZHAO GUANYU",
        ),
        {
            "id": 30,
            "match_key": "peer-fx-zhao-samuel",
            "match_type": "account_transfer",
            "relation_code": "peer_fx_transfer",
            "from_account_code": "santander",
            "to_account_code": "alipay",
            "source_row_id": 30,
            "target_row_id": 31,
            "confidence": Decimal("0.95"),
        },
    )

    assert fact["cashflow_type"] == "internal_transfer"
    assert fact["affects_spending"] is False
    assert fact["affects_nav"] is False
    assert fact["is_internal_transfer"] is True
    assert fact["counter_account_code"] == "alipay"


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
