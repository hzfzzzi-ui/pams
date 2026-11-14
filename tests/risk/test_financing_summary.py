from dataclasses import replace
from datetime import date
from decimal import Decimal as D

from domain.risk.financing import FinancingBalance, summarize_financing, verified_plan_increment


DAY = date(2024, 3, 5)
BROKER = FinancingBalance("broker", DAY, D(50), "cash-statement", True)
LOAN = FinancingBalance("share-loan", DAY, D(50), "loan-statement", True)


def summarize(rows, *, nav=D(500), coverage=True):
    return summarize_financing(rows, expected_obligations=frozenset({"broker", "share-loan"}),
                               coverage_confirmed=coverage, as_of=DAY, verified_nav=nav)


def test_same_obligation_is_counted_once_and_equal_independent_loans_add() -> None:
    same = replace(BROKER, evidence="duplicate")
    lower_authority = replace(LOAN, amount_cny=D(30), authoritative=False)
    result = summarize([BROKER, same, LOAN, lower_authority])
    assert result.confirmed_debt_cny == D(100)
    assert result.debt_ratio == D("0.2")
    assert len(result.balances) == 2


def test_conflicting_or_stale_obligation_preserves_known_subtotal_without_ratio() -> None:
    conflict = summarize([BROKER, LOAN, replace(LOAN, amount_cny=D(100))])
    assert conflict.confirmed_debt_cny == D(50)
    assert conflict.debt_ratio is None
    assert conflict.unresolved == ("share-loan",)
    stale = summarize([BROKER, replace(LOAN, at=date(2024, 3, 4))])
    assert stale == conflict


def test_partial_coverage_or_missing_net_value_keeps_ratio_unknown() -> None:
    for result in (summarize([BROKER, LOAN], nav=None), summarize([BROKER, LOAN], coverage=False)):
        assert result.confirmed_debt_cny == D(100)
        assert result.debt_ratio is None


def test_only_verified_open_plan_remainder_can_increase_projected_debt() -> None:
    assert verified_plan_increment(status="executed", original_limit_cny=D(80),
                                   verified_remaining_cny=D(5)) == 0
    assert verified_plan_increment(status="draft", original_limit_cny=D(80),
                                   verified_remaining_cny=None) is None
    assert verified_plan_increment(status="open", original_limit_cny=D(80),
                                   verified_remaining_cny=None) is None
    assert verified_plan_increment(status="open", original_limit_cny=D(80),
                                   verified_remaining_cny=D(5)) == D(5)
