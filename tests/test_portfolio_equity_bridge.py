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
    _cashflow_breakdown_from_rows,
    _funding_closure_checks,
    build_bridge_reconciliation_from_inputs,
)


class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> dict:
        return self.rows.pop(0)


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
