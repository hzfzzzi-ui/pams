from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from domain.investments.joint_ownership import (
    OpeningOwnership,
    OwnershipFlow,
    owned_period_pnl,
    replay_ownership,
)


OPEN = datetime(2024, 1, 1, tzinfo=timezone.utc)
INFLOW = OPEN + timedelta(days=1)
OUTFLOW = OPEN + timedelta(days=2)
ANCHOR = OpeningOwnership(OPEN, D(100), {"first": D(50), "second": D(50)}, "opening-proof")
BUY_IN = OwnershipFlow("one", INFLOW, D(30), D(120), {"first": D(24), "second": D(6)}, "split", "pre-flow")


def test_mixed_allocation_then_one_owner_withdraws() -> None:
    withdrawal = OwnershipFlow("two", OUTFLOW, D(-30), D(150), {"second": D(-30)}, "withdrawal", "pre-flow")
    before = replay_ownership(ANCHOR, [withdrawal, BUY_IN], at=INFLOW, verified_through=OUTFLOW)
    after = replay_ownership(ANCHOR, [withdrawal, BUY_IN], at=OUTFLOW, verified_through=OUTFLOW)
    assert before.units_by_owner == {"first": D(70), "second": D(55)}
    assert after.units_by_owner == {"first": D(70), "second": D(30)}
    assert D(150) / sum(before.units_by_owner.values()) == D(120) / sum(after.units_by_owner.values())
    assert after.net_flow_by_owner == {"first": D(24), "second": D(-24)}
    assert owned_period_pnl(opening_owned_nav=D(50), closing_owned_nav=after.ratio("first") * D(120),
                            personal_net_flow=after.net_flow_by_owner["first"]) == D(10)


def test_no_ratio_when_history_or_valuation_is_unverified() -> None:
    assert replay_ownership(None, [], at=INFLOW, verified_through=INFLOW).ratio("first") is None
    assert replay_ownership(ANCHOR, [], at=INFLOW, verified_through=None).status == "incomplete_flow_coverage"
    missing = replay_ownership(ANCHOR, [replace(BUY_IN, pre_flow_nav=None)], at=INFLOW, verified_through=INFLOW)
    assert missing.status == "missing_pre_flow_valuation"
    assert missing.blocked_fact_id == "one"
    assert missing.ratio("first") is None


def test_missing_allocation_duplicate_event_and_overdraft_are_blocked() -> None:
    split = replace(BUY_IN, allocation_by_owner={"first": D(24)})
    assert replay_ownership(ANCHOR, [split], at=INFLOW, verified_through=INFLOW).status == "unverified_allocation"
    replayed = replace(BUY_IN, at=OUTFLOW)
    assert replay_ownership(ANCHOR, [BUY_IN, replayed], at=OUTFLOW, verified_through=OUTFLOW).status == "ambiguous_flow_order"
    overdraft = replace(BUY_IN, amount=D(-70), allocation_by_owner={"first": D(-70)})
    assert replay_ownership(ANCHOR, [overdraft], at=INFLOW, verified_through=INFLOW).status == "withdrawal_exceeds_equity"
