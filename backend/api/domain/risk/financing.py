"""Summarize confirmed debt obligations without modifying portfolio valuation."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class FinancingBalance:
    obligation_id: str
    at: date
    amount_cny: Decimal | None
    evidence: str
    authoritative: bool


@dataclass(frozen=True)
class FinancingSummary:
    confirmed_debt_cny: Decimal
    debt_ratio: Decimal | None
    unresolved: tuple[str, ...]
    balances: tuple[FinancingBalance, ...]

def verified_plan_increment(
    *,
    status: str,
    original_limit_cny: Decimal,
    verified_remaining_cny: Decimal | None,
) -> Decimal | None:
    """Return only explicitly verified incremental financing for an open plan."""
    if status in {"executed", "expired", "cancelled"}:
        return Decimal(0)
    if (status == "open" and verified_remaining_cny is not None
            and original_limit_cny.is_finite() and verified_remaining_cny.is_finite()
            and 0 <= verified_remaining_cny <= original_limit_cny):
        return verified_remaining_cny
    return None


def summarize_financing(
    observations: Iterable[FinancingBalance],
    *,
    expected_obligations: frozenset[str],
    coverage_confirmed: bool,
    as_of: date,
    verified_nav: Decimal | None,
) -> FinancingSummary:
    """Group by proven obligation identity; only authoritative balances contribute."""
    candidates: dict[str, list[FinancingBalance]] = {}
    for observation in observations:
        if observation.at == as_of and observation.authoritative and observation.evidence:
            candidates.setdefault(observation.obligation_id, []).append(observation)
    unresolved = []
    balances = []
    for obligation in sorted(expected_obligations | candidates.keys()):
        rows = candidates.get(obligation, [])
        if (not obligation or not rows or any(
                row.amount_cny is None or not row.amount_cny.is_finite() or row.amount_cny < 0 for row in rows)
                or len({row.amount_cny for row in rows}) != 1):
            unresolved.append(obligation)
        else:
            balances.append(min(rows, key=lambda row: row.evidence))

    debt = sum((row.amount_cny for row in balances if row.amount_cny is not None), Decimal(0))
    ratio = None
    if (coverage_confirmed and not unresolved and verified_nav is not None
            and verified_nav.is_finite() and verified_nav > 0):
        ratio = debt / verified_nav
    return FinancingSummary(debt, ratio, tuple(unresolved), tuple(balances))
