from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.investment_trade_fact import (  # noqa: E402
    MANUAL_TABLE,
    TRADINGVIEW_TABLE,
    build_investment_trade_fact,
    insert_investment_trade_facts,
)
from endpoints.investment_transactions import (  # noqa: E402
    BUY_REASON_VALUES,
    InvestmentTradeReviewUpdate,
    REVIEW_TAG_VALUES,
    SELL_REASON_VALUES,
    _apply_owner_amount_scope,
    _asset_filter_display_name,
    _enrich_asset_filter_options,
    _field_was_provided,
    _infer_review_status,
    _infer_transition_quality,
    _review_options_payload,
    _transaction_summary,
    _validate_enum,
    _validate_review_tags,
    _validate_transition_triggers,
)
from fastapi import HTTPException  # noqa: E402


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


class FakeAssetLookupCursor:
    def __init__(self) -> None:
        self.rows = []

    def execute(self, sql: str, params=None) -> None:
        if "to_regclass" in sql:
            table_name = str(params[0]).split(".")[-1] if params else ""
            self.rows = [{"exists": table_name == "pit_security_master"}]
            return
        if "FROM pit_security_master" in sql:
            self.rows = [{"symbol": "sh600396", "ts_code": "600396.SH", "name": "华电辽能"}]
            return
        self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeQqqAliasCursor:
    def __init__(self) -> None:
        self.rows = []

    def execute(self, sql: str, params=None) -> None:
        if "to_regclass" in sql:
            table_name = str(params[0]).split(".")[-1] if params else ""
            self.rows = [{"exists": table_name in {"pams_asset_master", "pams_asset_alias"}}]
            return
        if "FROM pams_asset_alias" in sql:
            self.rows = [
                {
                    "symbol": "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I",
                    "name": "QQQ - INVESCO QQQ TRUST SERIES 1",
                }
            ]
            return
        self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _manual_trade(**overrides):
    row = {
        "source_table": MANUAL_TABLE,
        "source_id": "1",
        "source_key": "manual:trade-001",
        "trade_date": date(2026, 5, 8),
        "trade_time": datetime(2026, 5, 8, 10, 30),
        "settle_date": None,
        "account_code": "fund_account",
        "broker": "manual",
        "portfolio_code": None,
        "asset_id": None,
        "symbol": "510300",
        "name": "CSI 300 ETF",
        "market": "CN_FUND",
        "currency": "CNY",
        "side": "buy",
        "quantity": Decimal("100"),
        "price": Decimal("4.00"),
        "gross_amount": None,
        "fee_amount": Decimal("1.00"),
        "tax_amount": Decimal("0"),
        "net_cash": None,
        "fx_rate_to_cny": Decimal("1"),
        "realized_pnl": None,
        "raw_json": {},
    }
    row.update(overrides)
    return row


def test_manual_buy_trade_fact_has_signed_cash_and_position_flags() -> None:
    fact = build_investment_trade_fact(_manual_trade())

    assert fact["trade_type"] == "buy"
    assert fact["side"] == "buy"
    assert fact["gross_amount"] == Decimal("400.00")
    assert fact["net_cash"] == Decimal("-401.00")
    assert fact["affects_position"] is True
    assert fact["affects_cash"] is True


def test_dividend_trade_fact_does_not_change_position() -> None:
    fact = build_investment_trade_fact(
        _manual_trade(
            source_id="2",
            source_key="manual:dividend-001",
            side="dividend",
            quantity=None,
            price=None,
            gross_amount=Decimal("20"),
            fee_amount=Decimal("0"),
        )
    )

    assert fact["trade_type"] == "dividend"
    assert fact["net_cash"] == Decimal("20")
    assert fact["realized_pnl"] == Decimal("20")
    assert fact["realized_pnl_cny"] == Decimal("20")
    assert fact["affects_position"] is False
    assert fact["affects_realized_pnl"] is True


def test_cash_deposit_trade_fact_is_not_unknown_or_position_affecting() -> None:
    fact = build_investment_trade_fact(
        _manual_trade(
            source_id="5",
            source_key="manual:deposit-001",
            side="deposit",
            symbol="$CASH",
            name="券商入金",
            quantity=Decimal("1000"),
            price=None,
            gross_amount=None,
            fee_amount=Decimal("0"),
        )
    )

    assert fact["trade_type"] == "deposit"
    assert fact["side"] == "deposit"
    assert fact["gross_amount"] == Decimal("1000")
    assert fact["net_cash"] == Decimal("1000")
    assert fact["affects_cash"] is True
    assert fact["affects_position"] is False
    assert fact["affects_cost_basis"] is False
    assert fact["affects_realized_pnl"] is False


def test_cash_withdraw_trade_fact_is_not_unknown_or_position_affecting() -> None:
    fact = build_investment_trade_fact(
        _manual_trade(
            source_id="6",
            source_key="manual:withdraw-001",
            side="withdrawal",
            symbol="$CASH",
            name="券商出金",
            quantity=Decimal("300"),
            price=None,
            gross_amount=None,
            fee_amount=Decimal("0"),
        )
    )

    assert fact["trade_type"] == "withdraw"
    assert fact["side"] == "withdraw"
    assert fact["gross_amount"] == Decimal("300")
    assert fact["net_cash"] == Decimal("-300")
    assert fact["affects_cash"] is True
    assert fact["affects_position"] is False
    assert fact["affects_cost_basis"] is False
    assert fact["affects_realized_pnl"] is False


def test_unknown_cancel_trade_fact_does_not_affect_calculation_flags() -> None:
    fact = build_investment_trade_fact(
        _manual_trade(
            source_id="7",
            source_key="manual:cancel-001",
            side="cancel",
            quantity=None,
            price=None,
            gross_amount=Decimal("30000"),
            fee_amount=Decimal("0"),
        )
    )

    assert fact["trade_type"] == "unknown"
    assert fact["side"] == "cancel"
    assert fact["net_cash"] is None
    assert fact["affects_cash"] is False
    assert fact["affects_position"] is False
    assert fact["affects_cost_basis"] is False
    assert fact["affects_realized_pnl"] is False


def test_fx_pair_trade_fact_is_separated_from_buy_sell_amounts() -> None:
    fact = build_investment_trade_fact(
        _manual_trade(
            source_id="8",
            source_key="manual:fx-001",
            side="BUY",
            symbol="USD.MXN",
            name="USD.MXN",
            market="CASH",
            currency="MXN",
            quantity=Decimal("4626"),
            price=Decimal("17.2914"),
            gross_amount=None,
            fee_amount=Decimal("2"),
            net_cash=Decimal("0"),
        )
    )

    assert fact["trade_type"] == "fx_conversion"
    assert fact["side"] == "fx_conversion"
    assert fact["gross_amount"] == Decimal("79990.0164")
    assert fact["net_cash"] == Decimal("0")
    assert fact["affects_cash"] is True
    assert fact["affects_position"] is False
    assert fact["affects_cost_basis"] is False
    assert fact["affects_realized_pnl"] is False


def test_asset_filter_options_enrich_code_placeholder_names() -> None:
    assets = [{"asset_key": "600396", "symbol": "600396", "name": "SSE:600396"}]

    _enrich_asset_filter_options(FakeAssetLookupCursor(), assets)

    assert assets[0]["name"] == "华电辽能"
    assert assets[0]["display_name"] == "华电辽能"


def test_asset_filter_options_do_not_replace_named_fund_with_underlying_alias() -> None:
    assets = [
        {
            "asset_key": "华泰柏瑞纳斯达克100etf发起式联接(qdii)i",
            "symbol": None,
            "name": "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I",
        }
    ]

    _enrich_asset_filter_options(FakeQqqAliasCursor(), assets)

    assert assets[0]["name"] == "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I"
    assert assets[0]["display_name"] == "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I"


def test_asset_filter_display_name_uses_safe_fallbacks() -> None:
    assert _asset_filter_display_name({"asset_key": "600396", "symbol": "600396", "name": "SSE:600396"}) == "600396"
    assert _asset_filter_display_name({"asset_key": "USD.MXN", "symbol": "USD.MXN", "name": "USD.MXN"}) == "美元/墨西哥比索 外汇"
    assert _asset_filter_display_name(
        {
            "asset_key": "华泰柏瑞纳斯达克100etf发起式联接(qdii)i",
            "symbol": None,
            "name": "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I",
        }
    ) == "华泰柏瑞纳斯达克100ETF发起式联接(QDII)I"


def test_transaction_summary_keeps_raw_amounts_and_adds_personal_scope() -> None:
    rows = [
        {
            "account_code": "tonghuashun",
            "side_normalized": "buy",
            "gross_amount_cny": Decimal("1000"),
            "fees_cny": Decimal("10"),
            "source": "manual",
        },
        {
            "account_code": "ibkr",
            "side_normalized": "sell",
            "gross_amount_cny": Decimal("200"),
            "fees_cny": Decimal("2"),
            "source": "ibkr",
        },
    ]

    _apply_owner_amount_scope(
        rows,
        {
            "defaults": {"owner_ratio": 1},
            "accounts": {
                "tonghuashun": {"owner_ratio": "0.625"},
                "ibkr": {},
            },
        },
    )
    summary = _transaction_summary(rows, "trade_fact")

    assert rows[0]["owner_ratio"] == Decimal("0.625")
    assert rows[0]["gross_amount_personal_cny"] == Decimal("625.000")
    assert rows[0]["fees_personal_cny"] == Decimal("6.250")
    assert rows[1]["owner_ratio"] == Decimal("1")
    assert rows[1]["gross_amount_personal_cny"] == Decimal("200")
    assert summary["amount_scope"] == "account_original"
    assert summary["personal_amount_scope"] == "owner_ratio_adjusted"
    assert summary["has_owner_adjustments"] is True
    assert summary["owner_adjusted_accounts"] == ["tonghuashun"]
    assert summary["buy_amount_cny"] == 1000.0
    assert summary["buy_amount_personal_cny"] == 625.0
    assert summary["sell_amount_cny"] == 200.0
    assert summary["sell_amount_personal_cny"] == 200.0
    assert summary["fees_cny"] == 12.0
    assert summary["fees_personal_cny"] == 8.25


def test_tradingview_buy_maps_to_trade_fact_buy() -> None:
    fact = build_investment_trade_fact(
        {
            "source_table": TRADINGVIEW_TABLE,
            "source_id": "9",
            "source_key": "row-hash-9",
            "trade_date": date(2026, 5, 9),
            "trade_time": datetime(2026, 5, 9, 14, 30),
            "settle_date": None,
            "account_code": "tonghuashun",
            "broker": "tradingview",
            "portfolio_code": "a_share",
            "asset_id": None,
            "symbol": "600519",
            "name": "600519",
            "market": "CN_A",
            "currency": "CNY",
            "side": "Buy",
            "quantity": Decimal("100"),
            "price": Decimal("1500"),
            "gross_amount": Decimal("150000"),
            "fee_amount": Decimal("5"),
            "tax_amount": Decimal("0"),
            "net_cash": None,
            "fx_rate_to_cny": Decimal("1"),
            "realized_pnl": None,
            "raw_json": {},
        }
    )

    assert fact["trade_type"] == "buy"
    assert fact["net_cash"] == Decimal("-150005")
    assert fact["affects_position"] is True


def test_trade_source_hash_uses_source_key_before_row_id() -> None:
    first = build_investment_trade_fact(_manual_trade(source_id="3", source_key="manual:stable-trade"))
    replay = build_investment_trade_fact(_manual_trade(source_id="300", source_key="manual:stable-trade"))

    assert first["source_hash"] == replay["source_hash"]
    assert first["source_id"] != replay["source_id"]


def test_insert_trade_facts_is_idempotent_by_source_hash() -> None:
    first = build_investment_trade_fact(_manual_trade(source_id="4", source_key="manual:same-trade"))
    replay = build_investment_trade_fact(_manual_trade(source_id="400", source_key="manual:same-trade"))
    cur = FakeInsertCursor()

    assert insert_investment_trade_facts(cur, [first, replay]) == 1
    assert insert_investment_trade_facts(cur, [first]) == 0
    assert len(cur.source_hashes) == 1


def test_trade_review_options_are_enumerated_for_attribution() -> None:
    options = _review_options_payload()

    assert "fundamental_valuation" in BUY_REASON_VALUES
    assert "trend_signal" in BUY_REASON_VALUES
    assert "trial_observation" in BUY_REASON_VALUES
    assert "uncertain_review" in BUY_REASON_VALUES
    assert "planned_take_profit" in SELL_REASON_VALUES
    assert "risk_control_stop_loss" in SELL_REASON_VALUES
    assert "trend_signal_exit" in SELL_REASON_VALUES
    assert "replacement_better_asset" in SELL_REASON_VALUES
    assert "subjective_bullish" not in BUY_REASON_VALUES
    assert "emotional_trade" not in BUY_REASON_VALUES
    assert "emotional_sell" not in SELL_REASON_VALUES
    assert "sold_too_early" in REVIEW_TAG_VALUES
    assert "fomo" in REVIEW_TAG_VALUES
    assert "panic" in REVIEW_TAG_VALUES
    assert "loss_rationalization" in REVIEW_TAG_VALUES
    assert len(options["buy_reasons"]) == 10
    assert len(options["sell_reasons"]) == 10
    assert any(item["value"] == "fundamental_valuation" and "估值" in item["label"] for item in options["buy_reasons"])
    assert any(item["value"] == "uncertain_review" and "未确定" in item["label"] for item in options["buy_reasons"])
    assert any(item["value"] == "planned_take_profit" and "止盈" in item["label"] for item in options["sell_reasons"])
    assert any(item["value"] == "risk_control_stop_loss" and "止损" in item["label"] for item in options["sell_reasons"])
    assert any(item["value"] == "sold_too_early" and "卖早" in item["label"] for item in options["review_tags"])
    assert any(item["value"] == "fomo" and item["group"] == "behavior" for item in options["review_tags"])
    assert any(item["value"] == "uncertain" and "未确定" in item["label"] for item in options["position_identities"])
    assert any(item["value"] == "closed" for item in options["current_position_identities"])
    assert any(item["value"] == "trend_exit" for item in options["sell_rule_types"])
    assert any(item["value"] == "loss_rationalization" for item in options["transition_types"])
    assert any(item["value"] == "replacement_better_asset" and "更优标的" in item["label"] for item in options["transition_triggers"])
    assert not any(item["value"].startswith("fund_") for item in options["buy_reasons"] + options["sell_reasons"])


def test_trade_review_rejects_unknown_enum_values() -> None:
    assert _validate_enum("fundamental_valuation", BUY_REASON_VALUES, "buy_reason") == "fundamental_valuation"
    assert _validate_enum("uncertain_review", BUY_REASON_VALUES, "buy_reason") == "uncertain_review"
    assert _validate_enum("risk_control_stop_loss", SELL_REASON_VALUES, "sell_reason") == "risk_control_stop_loss"
    assert _validate_review_tags(["sold_too_early", "sold_too_early", "oversized"]) == [
        "sold_too_early",
        "oversized",
    ]
    assert _validate_transition_triggers(["price_down", "price_down", "research_added"]) == [
        "price_down",
        "research_added",
    ]
    assert _validate_transition_triggers(["replacement_better_asset"]) == ["replacement_better_asset"]
    try:
        _validate_enum("random_reason", BUY_REASON_VALUES, "buy_reason")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("invalid buy_reason should be rejected")

    try:
        _validate_review_tags(["not_a_tag"])
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("invalid review tag should be rejected")

    try:
        _validate_review_tags(["chase_high", "early_bottom", "rushed_buy", "oversized"])
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("too many execution tags should be rejected")


def test_trade_review_update_can_distinguish_omitted_hidden_fields() -> None:
    omitted = InvestmentTradeReviewUpdate(buy_reason="plan_execution", review_note="ok")
    explicit_clear = InvestmentTradeReviewUpdate(buy_reason="plan_execution", trade_intent=None)

    assert not _field_was_provided(omitted, "trade_intent")
    assert _field_was_provided(explicit_clear, "trade_intent")
    assert not _field_was_provided(omitted, "current_position_identity")


def test_trade_review_status_is_inferred_from_content() -> None:
    assert _infer_review_status(None) == "auto"
    assert _infer_review_status(None, buy_reason="plan_execution") == "reviewed"
    assert _infer_review_status(None, review_tags=["sold_too_early"]) == "reviewed"
    assert _infer_review_status(None, current_position_identity="trend") == "reviewed"
    assert _infer_review_status(None, review_note="after action note") == "reviewed"
    assert _infer_review_status("ignored", buy_reason="plan_execution") == "ignored"


def test_identity_transition_quality_flags_drift_patterns() -> None:
    assert _infer_transition_quality("trend", "value", None, [], None) == "suspected_stop_avoidance"
    assert _infer_transition_quality(
        "trend",
        "value",
        "research_reclassify",
        ["new_fundamental_evidence", "research_added"],
        "new thesis",
    ) == "needs_review"
    assert _infer_transition_quality("allocation", "trend", None, [], "short term idea") == "high_risk_drift"
    assert _infer_transition_quality("trial", "value", None, ["research_added"], "completed study") == "reasonable"
