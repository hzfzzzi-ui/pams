from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.portfolio_equity_bridge import build_bridge_reconciliation_from_inputs  # noqa: E402


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
