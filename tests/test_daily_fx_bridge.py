import sys
from copy import deepcopy
from datetime import date, datetime
from decimal import ROUND_UP, Decimal, Inexact, Rounded, getcontext, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from services.daily_fx_bridge import MAX_CURRENCIES, build_daily_fx_bridge

D = Decimal
OPEN = date(2026, 10, 27)
CLOSE = date(2026, 10, 28)
FIELDS = ("trade_cash", "income", "expenses", "funding", "other")
METRICS = (
    "price_pnl_cny", "position_fx_pnl_cny", "cash_fx_pnl_cny", "net_fx_pnl_cny",
    "income_pnl_cny", "expense_pnl_cny", "computed_pnl_cny",
    "unexplained_cash_cny", "expected_pnl_cny", "bridge_residual_cny",
)


def fx(rate, day):
    return {"status": "confirmed", "rate": D(rate), "as_of": day, "source": "dated_statement"}


def inputs(*, currency="USD", p0="50000", p1=None, c0="-30000", c1=None,
           r0="7.1", r1="7", **activity):
    return {
        "opening_date": OPEN, "closing_date": CLOSE,
        "opening_positions": {currency: D(p0)},
        "closing_positions": {currency: D(p1 if p1 is not None else p0)},
        "opening_cash": {currency: D(c0)},
        "closing_cash": {currency: D(c1 if c1 is not None else c0)},
        "fx_opening": {currency: fx(r0, OPEN)}, "fx_closing": {currency: fx(r1, CLOSE)},
        "activities": {currency: {key: D(activity.get(key, "0")) for key in FIELDS}},
    }


def add_currency(data, **kwargs):
    for key, value in inputs(**kwargs).items():
        if isinstance(value, dict):
            data[key].update(value)
    return data


def test_leverage_uses_net_cash_plus_positions_exposure():
    result = build_daily_fx_bridge(**inputs(), official_pnl_cny=D("-2000"))
    row = result["currencies"][0]
    assert row["position_fx_pnl_cny"] == D("-5000")
    assert row["cash_fx_pnl_cny"] == D("3000")
    assert row["net_fx_pnl_cny"] == D("-2000")
    assert row["price_pnl_cny"] == row["unexplained_native"] == 0
    assert result["status"] == "confirmed" and result["reasons"] == []
    assert result["summary"]["headline_pnl_cny"] == D("-2000")
    assert result["summary"]["expected_pnl_cny"] == D("-2000")
    assert result["summary"]["official_residual_cny"] == 0
    assert "end_of_day" in result["rate_convention"]
    assert result["opening_date"] == OPEN.isoformat()
    assert all(isinstance(row[key], D) for key in METRICS)


@pytest.mark.parametrize("r0,r1", [("7", "7"), ("7.000000000000000001", "7.000000000000000001")])
def test_flat_fx_is_exact_zero(r0, r1):
    result = build_daily_fx_bridge(**inputs(r0=r0, r1=r1))
    assert result["status"] == "confirmed"
    assert result["summary"]["net_fx_pnl_cny"] == 0


def test_cny_fund_returns_do_not_readd_underlying_usd_fx():
    data = inputs(currency="CNY", p0="100", p1="103", c0="0", r0="1", r1="1")
    data["fx_opening"].clear()
    data["fx_closing"].clear()
    result = build_daily_fx_bridge(**data)
    row = result["currencies"][0]
    assert row["opening_fx"] == {"rate": D(1), "as_of": str(OPEN), "source": "cny_identity"}
    assert row["position_fx_pnl_cny"] == row["cash_fx_pnl_cny"] == row["net_fx_pnl_cny"] == 0
    assert result["summary"]["headline_pnl_cny"] == D(3)
    assert result["status"] == "confirmed"


@pytest.mark.parametrize("side", ["fx_opening", "fx_closing"])
def test_contradictory_supplied_cny_identity_is_rejected(side):
    data = inputs(currency="CNY", r0="1", r1="1")
    data[side]["CNY"]["rate"] = D("1.01")
    with pytest.raises(ValueError, match="CNY FX identity"):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("trade,p1,c1", [("-10000", "60000", "-40000"), ("10000", "40000", "-20000")])
def test_principal_trades_preserve_net_fx_and_do_not_create_price_pnl(trade, p1, c1):
    result = build_daily_fx_bridge(**inputs(p1=p1, c1=c1, trade_cash=trade))
    assert result["status"] == "confirmed"
    assert result["summary"]["price_pnl_cny"] == 0
    assert result["summary"]["headline_pnl_cny"] == D("-2000")


@pytest.mark.parametrize("funding,c1", [("200", "305"), ("-200", "-95")])
def test_income_fees_and_signed_funding_at_closing_rate(funding, c1):
    data = inputs(p0="1000", p1="1110", c0="200", c1=c1,
                  trade_cash="-100", income="10", expenses="-5", funding=funding)
    result = build_daily_fx_bridge(**data, official_pnl_cny=D("-15"))
    row = result["currencies"][0]
    assert row["price_pnl_cny"] == 70
    assert row["net_fx_pnl_cny"] == -120
    assert row["income_pnl_cny"] == 70 and row["expense_pnl_cny"] == -35
    assert row["unexplained_native"] == row["bridge_residual_cny"] == 0
    assert row["expected_pnl_cny"] == row["computed_pnl_cny"] == -15
    assert result["summary"]["headline_pnl_cny"] == -15


@pytest.mark.parametrize("other", ["0", "10", "-10"])
def test_unknown_cash_is_unexplained_never_pnl(other):
    result = build_daily_fx_bridge(**inputs(c1="-29990", other=other), official_pnl_cny=D("-1930"))
    row = result["currencies"][0]
    assert row["unexplained_native"] == 10 and row["unexplained_cash_cny"] == 70
    assert row["computed_pnl_cny"] == -2000 and row["expected_pnl_cny"] == -1930
    assert row["bridge_residual_cny"] == 0
    assert result["summary"]["official_residual_cny"] == 70
    assert result["summary"]["headline_pnl_cny"] is None
    assert "native_cash_residual" in result["reasons"]
    assert result["status"] == "partial"


def test_nonzero_other_is_retained_even_if_net_cash_residual_is_zero():
    result = build_daily_fx_bridge(**inputs(other="10"))
    assert result["currencies"][0]["activities"]["other"] == 10
    assert result["currencies"][0]["unexplained_native"] == 0
    assert result["reasons"] == ["other_activity_unclassified"]
    assert result["summary"]["headline_pnl_cny"] is None


@pytest.mark.parametrize("residual,confirmed", [("0.01", True), ("-0.01", True),
                                                   ("0.010000000000000001", False), ("-1", False)])
def test_official_residual_is_exposed_not_absorbed(residual, confirmed):
    official = D("-2000") + D(residual)
    result = build_daily_fx_bridge(**inputs(), official_pnl_cny=official)
    assert result["summary"]["official_residual_cny"] == D(residual)
    assert (result["status"] == "confirmed") == confirmed
    assert result["summary"]["computed_pnl_cny"] == -2000
    assert result["summary"]["headline_pnl_cny"] == (D("-2000") if confirmed else None)


@pytest.mark.parametrize("side,day,code", [
    ("fx_opening", date(2026, 10, 26), "opening_fx_stale"),
    ("fx_opening", CLOSE, "opening_fx_future"),
    ("fx_closing", OPEN, "closing_fx_stale"),
    ("fx_closing", date(2026, 10, 29), "closing_fx_future"),
])
def test_wrong_session_fx_is_unavailable_not_used(side, day, code):
    data = inputs()
    data[side]["USD"]["as_of"] = day
    result = build_daily_fx_bridge(**data)
    assert result["summary"]["net_fx_pnl_cny"] is None
    assert result["summary"]["headline_pnl_cny"] is None
    assert code in result["reasons"]
    assert result["currencies"][0]["opening_fx" if side == "fx_opening" else "closing_fx"]["rate"] is None


@pytest.mark.parametrize("field,value,reason", [
    ("status", "estimated", "closing_fx_unconfirmed"),
    ("rate", None, "closing_fx_missing_rate"),
    ("source", " ", "closing_fx_missing_source"),
    ("as_of", None, "closing_fx_missing_date"),
])
def test_incomplete_fx_evidence_cannot_confirm(field, value, reason):
    data = inputs()
    data["fx_closing"]["USD"][field] = value
    result = build_daily_fx_bridge(**data)
    assert result["status"] == "pending"
    assert reason in result["reasons"]
    assert result["summary"]["computed_pnl_cny"] is None


def test_missing_fx_preserves_known_currency_partial_sum_without_headline():
    data = add_currency(inputs(), currency="CNY", p0="10", p1="13", c0="0", r0="1", r1="1")
    data["fx_opening"].pop("USD")
    data["fx_closing"].pop("USD")
    result = build_daily_fx_bridge(**data, official_pnl_cny=D("-1997"))
    assert result["status"] == "partial"
    assert result["summary"]["computed_pnl_cny"] is None
    assert result["summary"]["headline_pnl_cny"] is None
    assert result["summary"]["official_residual_cny"] is None
    assert result["known_partial"]["computed_pnl_cny"] == 3
    assert result["known_partial"]["net_fx_pnl_cny"] == 0
    assert result["coverage"]["currencies_with_complete_calculation"] == 1
    assert result["currencies"][1]["computed_pnl_cny"] is None


def test_missing_opening_fx_keeps_only_closing_rate_components():
    data = inputs(p1="50010", income="2", expenses="-1", c1="-29999")
    data["fx_opening"].clear()
    result = build_daily_fx_bridge(**data)
    assert result["status"] == "partial"
    assert result["summary"]["price_pnl_cny"] == 70
    assert result["summary"]["income_pnl_cny"] == 14
    assert result["summary"]["expense_pnl_cny"] == -7
    assert result["summary"]["cash_fx_pnl_cny"] is None
    assert result["known_partial"]["computed_pnl_cny"] is None


def test_no_rates_or_currencies_are_pending_not_zero():
    for data in (inputs(p0="0", c0="0"), inputs()):
        data["fx_opening"].clear()
        data["fx_closing"].clear()
        result = build_daily_fx_bridge(**data)
        assert result["status"] == "pending"
        assert all(result["summary"][key] is None for key in METRICS)
        assert all(result["known_partial"][key] is None for key in METRICS)
    empty = {key: {} if isinstance(value, dict) else value for key, value in inputs().items()}
    result = build_daily_fx_bridge(**empty)
    assert result["status"] == "pending" and result["reasons"] == ["no_currencies"]


def test_incomplete_non_fx_evidence_blocks_only_headline_not_diagnostics():
    result = build_daily_fx_bridge(**inputs(), evidence_complete=False)
    assert result["status"] == result["currencies"][0]["status"] == "partial"
    assert result["summary"]["computed_pnl_cny"] == -2000
    assert result["summary"]["headline_pnl_cny"] is None
    assert result["reasons"] == ["evidence_incomplete"]


@pytest.mark.parametrize("field", ["opening_positions", "closing_positions", "opening_cash", "closing_cash", "activities"])
def test_missing_currency_coverage_is_rejected(field):
    data = inputs()
    data[field].clear()
    with pytest.raises(ValueError, match="coverage"):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("field", FIELDS)
def test_all_activity_categories_must_be_explicit(field):
    data = inputs()
    del data["activities"]["USD"][field]
    with pytest.raises(ValueError, match="five explicit"):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("value", [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"),
                                   D("1e19"), D("1e-37"), D("0e999999"),
                                   D("1." + "0" * 100), 0, True, 1.25, "7", None])
def test_unsafe_or_non_decimal_amounts_rejected(value):
    data = inputs()
    data["opening_cash"]["USD"] = value
    with pytest.raises(ValueError if isinstance(value, D) else TypeError):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("location", ["closing_positions", "closing_cash", "income", "other", "official", "fx"])
def test_every_numeric_path_is_validated(location):
    data = inputs()
    if location == "official":
        data["official_pnl_cny"] = D("NaN")
    elif location == "fx":
        data["fx_opening"]["USD"]["rate"] = D("NaN")
    elif location in FIELDS:
        data["activities"]["USD"][location] = D("NaN")
    else:
        data[location]["USD"] = D("NaN")
    with pytest.raises(ValueError):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("rate", ["0", "-1", "1e7", "1e-13"])
def test_fx_rate_bounds(rate):
    with pytest.raises(ValueError):
        build_daily_fx_bridge(**inputs(r1=rate))


@pytest.mark.parametrize("currency", ["usd", "USD ", "USDT", "ZZZ", "XXX", "XAU", "US\u00d0", "", 1, None])
def test_bad_currency_codes_rejected(currency):
    with pytest.raises(ValueError, match="currency"):
        build_daily_fx_bridge(**inputs(currency=currency))


def test_excessive_currencies_rejected_before_calculation():
    data = inputs()
    data["opening_positions"] = {f"C{index:02}": D(0) for index in range(MAX_CURRENCIES + 1)}
    with pytest.raises(ValueError, match="at most"):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("opening,closing", [(CLOSE, OPEN), (OPEN, OPEN), ("2026-10-27", CLOSE),
                                             (datetime(2026, 10, 27), CLOSE)])
def test_invalid_date_window_rejected(opening, closing):
    data = inputs()
    data.update(opening_date=opening, closing_date=closing)
    with pytest.raises(ValueError):
        build_daily_fx_bridge(**data)


@pytest.mark.parametrize("field,value", [("income", "-1"), ("expenses", "1")])
def test_activity_direction_enforced(field, value):
    with pytest.raises(ValueError, match="income >= 0"):
        build_daily_fx_bridge(**inputs(**{field: value}))


def test_deterministic_immutable_and_decimal_context_isolated():
    data = add_currency(inputs(p1="50000.123456789012345678"),
                        currency="CNY", p0="1", c0="0", r0="1", r1="1")
    original = deepcopy(data)
    expected = build_daily_fx_bridge(**data)
    reordered = {key: dict(reversed(list(value.items()))) if isinstance(value, dict) else value
                 for key, value in data.items()}
    assert build_daily_fx_bridge(**reordered) == expected
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.flags[Inexact] = True
        before = str(getcontext())
        assert build_daily_fx_bridge(**data) == expected
        assert str(getcontext()) == before
        bad = deepcopy(data)
        bad["opening_cash"]["USD"] = D("sNaN")
        with pytest.raises(ValueError):
            build_daily_fx_bridge(**bad)
        assert str(getcontext()) == before
    assert data == original
    expected["currencies"][0]["activities"]["other"] = D(9)
    assert data == original


def test_upper_bounds_and_fractional_extremes_reconcile_exactly():
    data = inputs(p0="1000000000000000000", p1="-1000000000000000000",
                  c0="-1000000000000000000", c1="1000000000000000000",
                  r0="0.000000000001", r1="1000000")
    row = build_daily_fx_bridge(**data)["currencies"][0]
    assert row["bridge_residual_cny"] == 0
    assert all(row[key].is_finite() for key in METRICS)
    tiny = inputs(p0="0.000000000000000001", c0="0.000000000000000001", r0="0.000000000001", r1="0.000000000002")
    assert build_daily_fx_bridge(**tiny)["summary"]["headline_pnl_cny"] == D("2e-30")


def test_currency_residuals_cannot_cancel_to_confirm_account():
    data = add_currency(inputs(c1="-29999"), currency="EUR", c1="-30001")
    result = build_daily_fx_bridge(**data)
    assert result["summary"]["unexplained_cash_cny"] == 0
    assert result["coverage"]["currencies_with_zero_cash_residual"] == 0
    assert result["status"] == "partial"
    assert result["summary"]["headline_pnl_cny"] is None


def test_published_output_contract():
    result = build_daily_fx_bridge(**inputs())
    assert set(result) == {
        "opening_date", "closing_date", "rate_convention", "status", "summary",
        "known_partial", "currencies", "coverage", "reasons",
    }
    assert set(result["summary"]) == set(METRICS) | {
        "headline_pnl_cny", "official_pnl_cny", "official_residual_cny",
    }
    assert set(result["known_partial"]) == set(METRICS)
    assert set(result["currencies"][0]) == set(METRICS) | {
        "currency", "status", "reasons", "opening_positions", "closing_positions",
        "opening_cash", "closing_cash", "activities", "opening_fx", "closing_fx",
        "unexplained_native",
    }
    assert set(result["coverage"]) == {
        "currencies_total", "currencies_with_opening_fx", "currencies_with_closing_fx",
        "currencies_with_complete_calculation", "currencies_with_zero_cash_residual",
        "all_fx_exact_dated", "evidence_complete", "official_pnl_available",
    }
    assert result["summary"]["official_residual_cny"] is None


@pytest.mark.parametrize("field,value", [("as_of", OPEN), ("status", "estimated"), ("rate", None)])
def test_supplied_cny_evidence_is_not_silently_replaced(field, value):
    data = inputs(currency="CNY", r0="1", r1="1")
    data["fx_closing"]["CNY"][field] = value
    result = build_daily_fx_bridge(**data)
    assert result["currencies"][0]["closing_fx"]["rate"] is None
    assert result["summary"]["headline_pnl_cny"] is None


@pytest.mark.parametrize("field,value", [("source", "x" * 257), ("source", 1),
                                        ("as_of", str(CLOSE)), ("rate", "7")])
def test_malformed_fx_metadata_rejected(field, value):
    data = inputs()
    data["fx_closing"]["USD"][field] = value
    with pytest.raises((ValueError, TypeError)):
        build_daily_fx_bridge(**data)


def test_unknown_activity_categories_and_extra_fx_currencies_rejected():
    data = inputs()
    data["activities"]["USD"]["unknown"] = D(1)
    with pytest.raises(ValueError, match="five explicit"):
        build_daily_fx_bridge(**data)
    data = inputs()
    data["fx_opening"]["EUR"] = fx("7", OPEN)
    with pytest.raises(ValueError, match="outside state coverage"):
        build_daily_fx_bridge(**data)


def test_full_32_currency_bound_reconciles_without_rounding():
    codes = ("AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM",
             "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD",
             "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "COP", "CRC", "CUP", "CVE")
    data = inputs(currency=codes[0], p0="1000000000000000000", c0="1000000000000000000", r0="0.000000000001", r1="1000000")
    for code in codes[1:]:
        add_currency(data, currency=code, p0="1000000000000000000", c0="1000000000000000000", r0="0.000000000001", r1="1000000")
    result = build_daily_fx_bridge(**data)
    assert result["coverage"]["currencies_total"] == MAX_CURRENCIES
    assert result["status"] == "confirmed"
    assert result["summary"]["bridge_residual_cny"] == 0
    assert result["summary"]["headline_pnl_cny"] == D("63999999999999999936000000")


@pytest.mark.parametrize("places", [23, 30, 36])
def test_extended_fractional_amounts_and_official_pnl_are_not_rounded(places):
    amount = D("1234." + "123456789012345678901234567890123456"[:places])
    data = inputs(currency="CNY", p0="0", p1=str(amount), c0="0", r0="1", r1="1")
    snapshot = deepcopy(data)
    result = build_daily_fx_bridge(**data, official_pnl_cny=amount)
    assert result["status"] == "confirmed"
    assert result["summary"]["headline_pnl_cny"].as_tuple() == amount.as_tuple()
    assert result["summary"]["official_pnl_cny"].as_tuple() == amount.as_tuple()
    assert result["summary"]["official_residual_cny"] == 0
    assert result["known_partial"]["computed_pnl_cny"].as_tuple() == amount.as_tuple()
    assert data == snapshot


@pytest.mark.parametrize("conversion", ["1", "0.12345678"])
def test_official_nav_and_fx_products_accept_23_or_more_fractional_places(conversion):
    with localcontext() as reference:
        reference.prec = 256
        reference.traps[Inexact] = True
        reference.traps[Rounded] = True
        rate = D(conversion) * D("7.1234567890")
        official = D("1234.1234567890123") * rate
    assert official.as_tuple().exponent <= -23
    data = inputs(p0="0", p1="1234.1234567890123", c0="0", r0=str(rate), r1=str(rate))
    with localcontext() as caller:
        caller.prec = 2
        caller.Emax = 2
        caller.Emin = -2
        caller.traps[Inexact] = True
        caller.traps[Rounded] = True
        before = str(getcontext())
        result = build_daily_fx_bridge(**data, official_pnl_cny=official)
        assert str(getcontext()) == before
    assert result["status"] == "confirmed"
    assert result["summary"]["official_pnl_cny"].as_tuple() == official.as_tuple()
    assert result["summary"]["computed_pnl_cny"] == official
    assert result["summary"]["official_residual_cny"] == 0
    assert result["currencies"][0]["closing_fx"]["rate"].as_tuple() == rate.as_tuple()


def test_72_fractional_place_products_are_exact_and_context_isolated():
    data = inputs(
        p0="-999999999999999999.123456789012345678901234567890123456",
        p1="999999999999999999.987654321098765432109876543210987654",
        c0="0", r0="999999.123456789012345678901234567890123456",
        r1="999999.987654321098765432109876543210987654",
    )
    with localcontext() as reference:
        reference.prec = 256
        reference.traps[Inexact] = True
        reference.traps[Rounded] = True
        p0, p1 = data["opening_positions"]["USD"], data["closing_positions"]["USD"]
        r0, r1 = data["fx_opening"]["USD"]["rate"], data["fx_closing"]["USD"]["rate"]
        price = (p1 - p0) * r1
        position_fx = p0 * (r1 - r0)
        expected = p1 * r1 - p0 * r0
    assert price.as_tuple().exponent == -72
    assert len(price.as_tuple().digits) > 96
    with localcontext() as caller:
        caller.prec = 2
        caller.Emax = 2
        caller.Emin = -2
        caller.rounding = ROUND_UP
        caller.traps[Inexact] = True
        caller.traps[Rounded] = True
        caller.flags[Inexact] = True
        before = str(getcontext())
        result = build_daily_fx_bridge(**data)
        assert str(getcontext()) == before
    assert result["status"] == "confirmed"
    assert result["summary"]["price_pnl_cny"].as_tuple() == price.as_tuple()
    assert result["summary"]["position_fx_pnl_cny"] == position_fx
    assert result["summary"]["headline_pnl_cny"] == expected
    assert result["summary"]["bridge_residual_cny"] == 0


def test_smallest_accepted_unknown_cash_is_preserved_not_absorbed():
    data = inputs(p0="0", c0="0", c1="1e-36", r0="7", r1="7", other="1e-36")
    result = build_daily_fx_bridge(**data, official_pnl_cny=D("7e-36"))
    assert result["currencies"][0]["unexplained_native"] == D("1e-36")
    assert result["summary"]["unexplained_cash_cny"] == D("7e-36")
    assert result["summary"]["official_residual_cny"] == D("7e-36")
    assert result["summary"]["computed_pnl_cny"] == 0
    assert result["summary"]["headline_pnl_cny"] is None
    assert result["reasons"] == ["native_cash_residual", "other_activity_unclassified"]
