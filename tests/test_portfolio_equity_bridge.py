from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.portfolio_equity_bridge import (  # noqa: E402
    _attribution_pnl_basis,
    _bridge_quality,
    _bridge_cashflow_excluded_accounts,
    _cashflow_breakdown,
    _cashflow_breakdown_from_rows,
    _funding_closure_checks,
    build_bridge_reconciliation_from_inputs,
    build_nav_bridge_steps_from_inputs,
    build_nav_growth_explanation_from_inputs,
    period_investment_effect_from_inputs,
)


class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> dict:
        return self.rows.pop(0)


class _FakeRowsCursor:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return self.rows


def test_equity_bridge_keeps_unexplained_residual_visible() -> None:
    bridge = build_bridge_reconciliation_from_inputs(
        beginning_nav_cny=Decimal("631748.3494646280538478393760"),
        ending_nav_cny=Decimal("1179735.093170963"),
        external_cashflow_cny=Decimal("349836.3879"),
        investment_pnl_cny=Decimal("106611.22082038809"),
    )

    assert bridge["nav_delta_cny"] == Decimal("547986.7437063349461521606240")
    assert bridge["nav_residual_before_investment_cny"] == Decimal("198150.3558063349461521606240")
    assert bridge["unexplained_residual_cny"] == Decimal("91539.1349859468561521606240")
    assert bridge["is_reconciled"] is False


def test_equity_bridge_reconciles_only_when_residual_is_small() -> None:
    bridge = build_bridge_reconciliation_from_inputs(
        beginning_nav_cny=Decimal("100"),
        ending_nav_cny=Decimal("150"),
        external_cashflow_cny=Decimal("20"),
        investment_pnl_cny=Decimal("30"),
    )

    assert bridge["unexplained_residual_cny"] == Decimal("0")
    assert bridge["is_reconciled"] is True


def test_bridge_quality_requires_investment_attribution_to_explain_nav_effect() -> None:
    quality = _bridge_quality(
        bridge={"unexplained_residual_cny": Decimal("0")},
        investment_attribution_gap_cny=Decimal("1000"),
        checks=[],
    )

    assert quality["status"] == "unreconciled"
    assert quality["investment_attribution_explained"] is False
    assert quality["blocking_checks"][0]["code"] == "investment_attribution_gap"


def test_period_investment_effect_is_nav_delta_after_external_cashflow() -> None:
    effect = period_investment_effect_from_inputs(
        beginning_nav_cny=Decimal("768674.39"),
        ending_nav_cny=Decimal("1212212.58"),
        external_cashflow_cny=Decimal("310745.75"),
    )

    assert effect == Decimal("132792.44")


def test_nav_bridge_steps_are_additive_and_balanced() -> None:
    steps = build_nav_bridge_steps_from_inputs(
        beginning_nav_cny=Decimal("1000"),
        external_cashflow_cny=Decimal("200"),
        period_investment_effect_cny=Decimal("50"),
        unexplained_residual_cny=Decimal("-10"),
        ending_nav_cny=Decimal("1240"),
    )

    assert [step["key"] for step in steps] == [
        "beginning_nav",
        "external_cashflow",
        "period_investment_effect",
        "unexplained_residual",
        "ending_nav",
    ]
    assert steps[-2]["running_nav_cny"] == Decimal("1240")
    assert steps[-1]["is_balanced"] is True


def test_nav_growth_explanation_keeps_additive_drivers_separate_from_attribution_gap() -> None:
    bridge = build_bridge_reconciliation_from_inputs(
        beginning_nav_cny=Decimal("768674.39"),
        ending_nav_cny=Decimal("1212212.58"),
        external_cashflow_cny=Decimal("310745.75"),
        investment_pnl_cny=Decimal("132792.44"),
    )
    explanation = build_nav_growth_explanation_from_inputs(
        beginning_nav_cny=Decimal("768674.39"),
        ending_nav_cny=Decimal("1212212.58"),
        cashflows={
            "external_net_cny": Decimal("310745.75"),
            "buckets": {
                "main_income": {"amount_cny": Decimal("560390.48"), "count": 11},
                "expense": {"amount_cny": Decimal("-374588.19"), "count": 120},
                "investment_realized_cash": {
                    "amount_cny": Decimal("37120"),
                    "count": 1,
                    "excluded_from_external_cashflow": True,
                },
            },
        },
        period_investment_effect_cny=Decimal("132792.44"),
        attribution_pnl_cny=Decimal("106611.22"),
        attribution_current_float_pnl_cny=Decimal("100000.00"),
        attribution_closed_realized_pnl_cny=Decimal("6611.22"),
        bridge=bridge,
    )

    assert explanation["summary"]["nav_delta_cny"] == Decimal("443538.19")
    assert explanation["drivers"][0]["key"] == "external_cashflow"
    assert explanation["drivers"][0]["amount_cny"] == Decimal("310745.75")
    assert explanation["drivers"][1]["key"] == "period_investment_effect"
    assert explanation["drivers"][1]["amount_cny"] == Decimal("132792.44")
    assert explanation["drivers"][2]["key"] == "unexplained_residual"
    assert explanation["drivers"][2]["amount_cny"] == Decimal("0.00")
    assert [item["key"] for item in explanation["investment_breakdown"]] == [
        "period_investment_effect",
        "attributed_investment_pnl",
        "closed_realized_pnl",
        "float_period_gap",
    ]
    assert explanation["investment_breakdown"][0]["amount_cny"] == Decimal("132792.44")
    assert explanation["investment_breakdown"][1]["amount_cny"] == Decimal("106611.22")
    assert explanation["investment_breakdown"][2]["amount_cny"] == Decimal("6611.22")
    assert explanation["investment_breakdown"][3]["amount_cny"] == Decimal("-32792.44")
    assert explanation["summary"]["cumulative_period_gap_cny"] == Decimal("-26181.22")
    assert any(item["key"] == "investment_realized_cash" and item["excluded_from_external_cashflow"] for item in explanation["cashflow_items"])


def test_nav_growth_explanation_splits_account_reconciliation_from_closed_pnl() -> None:
    bridge = build_bridge_reconciliation_from_inputs(
        beginning_nav_cny=Decimal("1000"),
        ending_nav_cny=Decimal("1300"),
        external_cashflow_cny=Decimal("100"),
        investment_pnl_cny=Decimal("200"),
    )
    explanation = build_nav_growth_explanation_from_inputs(
        beginning_nav_cny=Decimal("1000"),
        ending_nav_cny=Decimal("1300"),
        cashflows={"external_net_cny": Decimal("100"), "buckets": {}},
        period_investment_effect_cny=Decimal("200"),
        attribution_pnl_cny=Decimal("230"),
        attribution_current_float_pnl_cny=Decimal("190"),
        attribution_closed_realized_pnl_cny=Decimal("40"),
        attribution_reconciliation_pnl_cny=Decimal("9.50"),
        bridge=bridge,
    )

    breakdown = {item["key"]: item for item in explanation["investment_breakdown"]}
    assert breakdown["closed_realized_pnl"]["amount_cny"] == Decimal("30.50")
    assert breakdown["account_reconciliation_pnl"]["amount_cny"] == Decimal("9.50")
    assert breakdown["float_period_gap"]["amount_cny"] == Decimal("-0.50")
    assert explanation["summary"]["closed_realized_pnl_cny"] == Decimal("30.50")
    assert explanation["summary"]["account_reconciliation_pnl_cny"] == Decimal("9.50")


def test_attribution_pnl_basis_splits_open_and_closed_rows() -> None:
    current_float, closed = _attribution_pnl_basis(
        {
            "dimensions": {
                "holding": [
                    {"market_value_cny": Decimal("100"), "pnl_cny": Decimal("12")},
                    {"market_value_cny": Decimal("0"), "pnl_cny": Decimal("3")},
                    {"market_value_cny": Decimal("-0.00001"), "pnl_cny": Decimal("2")},
                ]
            }
        },
        Decimal("15"),
    )

    assert current_float == Decimal("12")
    assert closed == Decimal("3")


def test_cashflow_breakdown_excludes_linked_investment_cash_realization() -> None:
    breakdown = _cashflow_breakdown_from_rows(
        [
            {
                "cashflow_type": "operating_income",
                "category": "收入",
                "subcategory": "年终奖",
                "count": 1,
                "amount_cny": Decimal("37120"),
                "is_linked_investment_return": True,
            },
            {
                "cashflow_type": "operating_income",
                "category": "收入",
                "subcategory": "年终奖",
                "count": 1,
                "amount_cny": Decimal("10000"),
                "is_linked_investment_return": False,
            },
        ]
    )

    assert breakdown["external_net_cny"] == Decimal("10000")
    assert breakdown["buckets"]["investment_realized_cash"]["amount_cny"] == Decimal("37120")
    assert breakdown["buckets"]["investment_realized_cash"]["excluded_from_external_cashflow"] is True
    linked_detail = next(item for item in breakdown["details"] if item["bucket"] == "investment_realized_cash")
    assert linked_detail["excluded_from_external_cashflow"] is True


def test_cashflow_breakdown_filters_excluded_nav_accounts() -> None:
    cur = _FakeRowsCursor([])

    _cashflow_breakdown(
        cur,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 22),
        excluded_accounts={"ccb_cny", "legacy"},
    )

    sql, params = cur.executed[0]
    assert "cf.account_code = ANY" in sql
    assert params == (date(2026, 1, 1), date(2026, 6, 22), ["ccb_cny", "legacy"])


def test_bridge_cashflow_excluded_accounts_keep_parent_payment_ledgers() -> None:
    excluded = _bridge_cashflow_excluded_accounts(
        {
            "excluded_accounts": ["ccb_cny", "wechat", "gongjijin", "huawei"],
            "defaults": {},
        }
    )

    assert excluded == {"ccb_cny"}


def test_tonghuashun_personal_capital_checks_cashflow_source_side() -> None:
    cur = _FakeCursor(
        [
            {"count": 1, "amount_cny": Decimal("100"), "first_date": date(2026, 1, 1), "last_date": date(2026, 1, 1)},
            {"count": 1, "amount_cny": Decimal("100"), "first_date": date(2026, 1, 1), "last_date": date(2026, 1, 1)},
            {"count": 5, "amount_cny": Decimal("400000")},
            {"count": 6, "amount_cny": Decimal("250000"), "first_date": date(2025, 12, 15), "last_date": date(2026, 5, 20)},
        ]
    )

    checks = _funding_closure_checks(
        cur,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 21),
        account_cfg={
            "defaults": {"owner_ratio": 1},
            "accounts": {"tonghuashun": {"owner_ratio": "0.625", "personal_capital": "250000"}},
        },
    )

    ths = next(item for item in checks if item["code"] == "tonghuashun_personal_capital")
    assert ths["status"] == "ok"
    assert ths["investment_trade_deposit_gross_cny"] == Decimal("400000")
    assert ths["investment_trade_deposit_personal_cny"] == Decimal("250000.000")
    assert ths["cashflow_source_personal_cny"] == Decimal("250000")
    assert ths["source_cashflow_count"] == 6
