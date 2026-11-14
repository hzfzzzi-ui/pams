from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.investments.holding_return_display import attach_dated_holding_performance, attach_recent_us_returns
from domain.investments.investment_view_date import daily_pnl_window_date, market_data_candidate_date


DAY = date(2026, 10, 27)
ACTIVE = date(2026, 10, 28)


def holding(**changes):
    return {"account": "broker", "symbol": "EXAMPLE", "major_category": "美股", "currency": "USD",
            "quantity": "10", "cost_cny": "900", "market_value_cny": "9999",
            "snapshot_as_of": str(DAY), "daily_pnl_stale": True,
            "daily_return_status": "pending", "daily_pnl_cny": "0", **changes}


def confirmed(**changes):
    fields = dict(daily_pnl_stale=False, daily_return_status="confirmed",
                   daily_return_status_as_of=str(DAY), daily_return_status_source="yahoo_close",
                   daily_pnl="6", daily_pnl_native="6", daily_pnl_cny="42.6", daily_fx_pnl_cny="-1.6",
                   daily_total_pnl_cny="41", previous_price="10", price_change_pct="0.06",
                   price_change_amount="0.6")
    fields.update(changes)
    return holding(**fields)


class Cursor:
    def __init__(self, day=DAY, exists=True):
        self.day, self.exists, self.queries = day, exists, []

    def execute(self, sql, params=None):
        assert sql.lstrip().startswith("SELECT"), "Display must never write"
        self.queries.append((sql, params))

    def fetchone(self):
        return {"exists": self.exists} if len(self.queries) == 1 else {"as_of": self.day}


def attach(rows=None, historical=None, day=DAY, active=ACTIVE):
    payload = {"as_of": str(DAY), "rows": rows if rows is not None else [holding()]}
    history = {"rows": historical if historical is not None else [confirmed()]}
    snapshot = deepcopy((payload, history))
    cur, calls = Cursor(day), []
    def load(value):
        calls.append(value)
        return history
    result = attach_recent_us_returns(cur, payload, active_date=active, load_date=load)
    assert (payload, history) == snapshot
    assert len(calls) <= 1, "No per-security historical queries"
    return result, cur, calls


def test_0700_boundary_keeps_current_bucket_separate_from_dated_close():
    zone = ZoneInfo("Asia/Shanghai")
    before = datetime(2026, 10, 28, 6, 59, tzinfo=zone)
    after = datetime(2026, 10, 28, 7, 1, tzinfo=zone)
    assert daily_pnl_window_date(now=before) == DAY
    assert daily_pnl_window_date(now=after) == ACTIVE
    assert market_data_candidate_date(now=after) == DAY
    result, _, calls = attach(active=daily_pnl_window_date(now=after))
    row = result["rows"][0]
    assert row["daily_pnl_cny"] == "0" and row["daily_return_status"] == "pending"
    recent = row["recent_confirmed_return"]
    assert recent["as_of"] == str(DAY) and recent["current_as_of"] == str(ACTIVE)
    assert recent["fields"]["daily_total_pnl_cny"] == "41"
    assert recent["fields"]["daily_fx_pnl_cny"] == "-1.6"
    assert calls == [DAY]
    aggregate = result["recent_confirmed_returns"]["by_account"]["broker"]
    assert aggregate["amount_cny"] == "41" and aggregate["label"] == "10/27收益"


def test_no_replay_when_current_estimate_or_close_is_available():
    for status in ("confirmed", "estimated"):
        result, cur, calls = attach(rows=[holding(daily_pnl_stale=False, daily_return_status=status)])
        assert not cur.queries and not calls and "recent_confirmed_returns" not in result


@pytest.mark.parametrize("changes", [
    {"currency": "KRW"}, {"major_category": "A股"}, {"is_closed": True},
    {"symbol": "CASH_USD"}, {"quantity": "0"}, {"quantity": None}, {"quantity": "NaN"},
    {"symbol": None}, {"account": None},
])
def test_other_markets_cash_closed_or_invalid_holdings_are_not_relabelled(changes):
    _, cur, calls = attach(rows=[holding(**changes)])
    assert not cur.queries and not calls


@pytest.mark.parametrize("field,value", [
    ("daily_return_status", "estimated"), ("daily_pnl_stale", True),
    ("daily_return_status_as_of", "2026-10-26"), ("snapshot_as_of", "2026-10-26"),
    ("daily_return_status_source", None), ("daily_total_pnl_cny", None),
    ("daily_fx_pnl_cny", "NaN"), ("daily_pnl_native", "Infinity"), ("previous_price", "0"),
])
def test_incomplete_or_wrong_day_evidence_is_not_filled_with_zero(field, value):
    row = confirmed()
    row[field] = value
    result, _, _ = attach(historical=[row])
    assert "recent_confirmed_return" not in result["rows"][0]


@pytest.mark.parametrize("day", [None, ACTIVE, date(2026, 10, 29)])
def test_missing_same_day_or_future_close_is_not_a_previous_confirmation(day):
    result, _, calls = attach(day=day)
    assert not calls and "recent_confirmed_returns" not in result


@pytest.mark.parametrize("day,active", [(date(2026, 10, 23), date(2026, 10, 26)),
                                       (date(2026, 10, 30), date(2026, 11, 2))])
def test_weekend_and_dst_calendar_gap_uses_evidenced_business_date(day, active):
    old = confirmed(snapshot_as_of=str(day), daily_return_status_as_of=str(day))
    result, _, calls = attach(day=day, active=active, historical=[old])
    assert calls == [day]
    assert result["rows"][0]["recent_confirmed_return"]["as_of"] == str(day)


def test_partial_and_multiple_accounts_use_server_decimal_sums_without_mixing_dates():
    rows = [holding(), holding(symbol="MISSING"), holding(currency="HKD", symbol="HK"), holding(account="second")]
    source = [confirmed(), confirmed(account="second", daily_total_pnl_cny="-0.25")]
    result, cur, calls = attach(rows=rows, historical=source)
    accounts = result["recent_confirmed_returns"]["by_account"]
    assert accounts["broker"]["covered"] == 1 and accounts["broker"]["expected"] == 3
    assert accounts["broker"]["partial"] and accounts["broker"]["amount_cny"] == "41"
    assert accounts["second"]["amount_cny"] == "-0.25"
    assert len(cur.queries) == 2 and calls == [DAY]
    assert "quote_date < %s" in cur.queries[1][0]


def test_missing_quote_table_preserves_legacy_payload():
    payload = {"rows": [holding()]}
    result = attach_recent_us_returns(Cursor(exists=False), payload, active_date=ACTIVE,
                                     load_date=lambda _: pytest.fail("Unexpected replay"))
    assert result is payload


def test_confirmed_zero_is_not_missing():
    row = confirmed()
    row.update({field: "0" for field in ("daily_pnl", "daily_pnl_native", "daily_pnl_cny", "daily_fx_pnl_cny", "daily_total_pnl_cny")})
    result, _, _ = attach(historical=[row])
    assert Decimal(result["rows"][0]["recent_confirmed_return"]["fields"]["daily_total_pnl_cny"]) == 0


def test_optional_history_failure_keeps_current_holdings_and_does_not_log_raw_details(caplog):
    payload = {"rows": [holding()]}
    def unavailable(_):
        raise RuntimeError("private-details-must-not-be-logged")
    result = attach_recent_us_returns(Cursor(), payload, active_date=ACTIVE, load_date=unavailable)
    assert result is payload
    assert "RuntimeError" in caplog.text and "private-details" not in caplog.text

@pytest.mark.parametrize("complete_close", [True, False])
def test_a_stock_card_keeps_dated_close_separate_and_does_not_require_account_cash(complete_close):
    def stock(row):
        return {**row, "account": "example", "currency": "CNY", "asset_liability": "\u8d44\u4ea7",
                "holding_pnl_cny": "-24.50"}
    current = [stock(holding()), stock(holding(symbol="SECOND")),
               stock(holding(is_closed=True)), holding(account="other")]
    historical = [stock(confirmed()), stock(confirmed(symbol="SECOND",
                    daily_return_status="confirmed" if complete_close else "estimated"))]
    payload = {"rows": current}
    original = deepcopy(payload)
    calls = []
    def load(day):
        calls.append(day)
        return {"rows": historical}

    found = attach_dated_holding_performance(payload, account_code="example",
                                               active_date=ACTIVE, load_date=load, limit=500)
    assert calls == [DAY]
    assert payload == original and found["rows"] == original["rows"]
    assert found["holding_performance"] == {
        "cumulative_pnl_cny": "-49.00",
        "recent_daily_pnl_cny": "82" if complete_close else None,
        "recent_daily_as_of": str(DAY) if complete_close else None,
    }


def test_fund_daily_card_uses_one_common_confirmed_nav_date():
    def fund(symbol, day):
        return holding(account="fund", symbol=symbol, asset_liability="\u8d44\u4ea7",
                       snapshot_as_of=str(day), holding_pnl_cny="-2")
    current = [fund("A", DAY), fund("B", ACTIVE)]
    history = [{**fund(symbol, DAY), "daily_pnl_stale": False, "daily_total_pnl_cny": "-3",
                "fund_nav_raw_status": "confirmed", "fund_nav_confirmed_date": str(DAY),
                "fund_nav_confirmed_unit_nav": "1.02", "current_price": "1.02",
                "previous_price": "1.05"} for symbol in ("A", "B")]
    calls = []

    def load(day):
        calls.append(day)
        return {"rows": history}

    result = attach_dated_holding_performance(
        {"rows": current}, account_code="fund", active_date=ACTIVE, load_date=load,
        limit=500, allow_mixed_snapshot_days=True,
    )["holding_performance"]
    assert calls == [DAY]
    assert result["recent_daily_pnl_cny"] == "-6"
    assert result["recent_daily_as_of"] == str(DAY)
