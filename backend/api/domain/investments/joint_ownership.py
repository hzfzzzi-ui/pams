"""Replay verified joint-account capital flows in the account's base currency."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence


@dataclass(frozen=True)
class OpeningOwnership:
    at: datetime
    nav: Decimal
    equity_by_owner: Mapping[str, Decimal]
    evidence: str


@dataclass(frozen=True)
class OwnershipFlow:
    fact_id: str
    at: datetime
    amount: Decimal
    pre_flow_nav: Decimal | None
    allocation_by_owner: Mapping[str, Decimal]
    allocation_evidence: str
    valuation_evidence: str


@dataclass(frozen=True)
class OwnershipResult:
    status: str
    units_by_owner: Mapping[str, Decimal] | None = None
    net_flow_by_owner: Mapping[str, Decimal] | None = None
    blocked_fact_id: str | None = None

    def ratio(self, owner: str) -> Decimal | None:
        if self.units_by_owner is None or owner not in self.units_by_owner:
            return None
        return self.units_by_owner[owner] / sum(self.units_by_owner.values())


def replay_ownership(
    opening: OpeningOwnership | None,
    flows: Sequence[OwnershipFlow],
    *,
    at: datetime,
    verified_through: datetime | None,
) -> OwnershipResult:
    """Calculate ownership only across a complete, explicitly verified event range.

    The caller supplies allocations and pre-flow valuations verified against source
    evidence. This calculation does not promote suggested matches to confirmed flows.
    """
    if opening is None or at < opening.at or not opening.evidence:
        return OwnershipResult("missing_opening")
    if (not opening.nav.is_finite() or opening.nav <= 0 or not opening.equity_by_owner
            or any(not owner or not value.is_finite() or value < 0
                   for owner, value in opening.equity_by_owner.items())
            or sum(opening.equity_by_owner.values()) != opening.nav):
        return OwnershipResult("invalid_opening")
    if verified_through is None or verified_through < at:
        return OwnershipResult("incomplete_flow_coverage")

    units = dict(opening.equity_by_owner)
    net_flows = dict.fromkeys(units, Decimal(0))
    seen = set()
    previous = opening.at
    for flow in sorted(flows, key=lambda flow: flow.at):
        if flow.at <= opening.at or flow.at > at:
            continue
        if not flow.fact_id or flow.fact_id in seen or flow.at == previous:
            return OwnershipResult("ambiguous_flow_order", blocked_fact_id=flow.fact_id)
        if not flow.amount.is_finite() or flow.amount == 0:
            return OwnershipResult("invalid_flow_amount", blocked_fact_id=flow.fact_id)
        allocated = flow.allocation_by_owner
        if (not flow.allocation_evidence or not allocated
                or any(owner not in units or not value.is_finite() or value == 0
                       or (value > 0) != (flow.amount > 0) for owner, value in allocated.items())
                or sum(allocated.values()) != flow.amount):
            return OwnershipResult("unverified_allocation", blocked_fact_id=flow.fact_id)
        nav = flow.pre_flow_nav
        if nav is None or not nav.is_finite() or nav <= 0 or not flow.valuation_evidence:
            return OwnershipResult("missing_pre_flow_valuation", blocked_fact_id=flow.fact_id)
        if nav + flow.amount <= 0:
            return OwnershipResult("nonpositive_post_flow_nav", blocked_fact_id=flow.fact_id)
        total_units = sum(units.values())
        for owner, value in allocated.items():
            owned_value = units[owner] * nav
            flow_value = value * total_units
            if owned_value + flow_value < 0:
                return OwnershipResult("withdrawal_exceeds_equity", blocked_fact_id=flow.fact_id)
            units[owner] = (owned_value + flow_value) / nav
            net_flows[owner] += value
        previous = flow.at
        seen.add(flow.fact_id)
    return OwnershipResult("confirmed", units, net_flows)


def owned_period_pnl(
    *, opening_owned_nav: Decimal, closing_owned_nav: Decimal, personal_net_flow: Decimal,
) -> Decimal:
    return closing_owned_nav - opening_owned_nav - personal_net_flow
