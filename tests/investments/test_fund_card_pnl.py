from datetime import date
from decimal import Decimal

import pytest

from domain.investments.holdings.personal_holdings import position_pnl
from domain.investments.returns.fund_return_history import apply_fund_card_pnl


def event(day, kind, units, cash):
    return dict(asset_id="fund", symbol="000000", currency="CNY", trade_type=kind,
                quantity=units, net_cash=cash, trade_date=date(2026, 1, day),
                fund_execution_confirmed=True, source_evidence_status="verified")


def project(orders, quantity, observed=None):
    end = date(2026, 1, 15)
    row = dict(asset_id="fund", symbol="000000", currency="CNY", owner_ratio=Decimal(".5"),
               quantity=quantity, cost=50, cost_cny=25, market_value=120, market_value_gross_cny=120,
               market_value_cny=60, realized_trade_pnl_source="unavailable_trade_coverage")
    snapshots = [dict(asset_id="fund", symbol="000000", as_of=end,
                      quantity=quantity if observed is None else observed)]
    apply_fund_card_pnl(row, orders, snapshots, end)
    return row


def test_verified_cash_and_remaining_cost_reconcile_without_double_applying_ownership():
    buy = event(1, "fund_subscription", 10, -100)
    row = project([buy, event(16, "fund_redemption", 10, 120)], 10)
    assert row["realized_trade_pnl_cny"] == 0
    row = project([buy, event(8, "fund_redemption", 4, 55), event(9, "dividend", None, 3)], 6)
    assert row["reported_cost"] == 50 and row["cost"] == 60
    assert row["holding_pnl_cny"] == 39
    assert position_pnl([row]) == dict(scope="account", cumulative_cny=78, realized_cny=15,
                                       floating_cny=60, net_distributions_cny=3)


@pytest.mark.parametrize("change,observed", [
    ({"source_evidence_status": "unknown"}, 10), ({"fund_execution_confirmed": False}, 10),
    ({"asset_id": "different"}, 10), ({"currency": "USD"}, 10), ({"net_cash": None}, 10), ({}, 7),
])
def test_incomplete_evidence_leaves_realized_unknown(change, observed):
    row = project([{**event(1, "fund_subscription", 10, -100), **change}], 10, observed)
    assert row["realized_trade_pnl_source"] == "unavailable_trade_coverage"
    assert position_pnl([row])["realized_cny"] is None
