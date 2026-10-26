"""Read-only overseas quote coverage must not be inferred from the newest peer."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import os
import sys
from zoneinfo import ZoneInfo

import pytest
import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/api"))

from endpoints import sync_center
from services import sync_health


NOW = datetime(2026, 10, 27, 4, 45, tzinfo=ZoneInfo("Asia/Shanghai"))


def holding(symbol="QQQ", **changes):
    return {
        "account_code": "test_broker", "symbol": symbol, "name": symbol,
        "currency": "USD", "market": "美股", "listing_region": "美国",
        "as_of": date(2026, 10, 26), "updated_at": NOW,
        "current_price": Decimal("123.45"), "quote_price": Decimal("123.45"),
        "quote_date": date(2026, 10, 26), "return_status": "confirmed",
        "source": "yahoo_close", "quote_session": "close", "close_evidence": True, **changes,
    }


def test_korean_latest_date_does_not_hide_old_us_positions():
    old = date(2026, 10, 23)
    rows = [holding(symbol, as_of=old, quote_date=old) for symbol in ("QQQ", "MU", "NVDA")]
    rows.append(holding("005930.KS", currency="KRW", market="韩股", listing_region="韩国"))
    before = deepcopy(rows)
    result = sync_health.overseas_quote_health(rows, NOW)
    assert result["status"] == "review"
    assert result["as_of"] == date(2026, 10, 26)
    assert result["coverage"]["expected"] == 4
    assert result["coverage"]["covered"] == 1
    assert result["coverage"]["stale"] == 3
    assert "1/4" in result["details"]
    assert all(symbol in result["details"] for symbol in ("QQQ", "MU", "NVDA"))
    assert rows == before  # Diagnostics must not zero or relabel financial facts.


@pytest.mark.parametrize("change,issue", [
    ({"current_price": None}, "missing_price"),
    ({"quote_price": None}, "missing_price"),
    ({"quote_price": 0}, "missing_price"),
    ({"quote_price": "NaN"}, "missing_price"),
    ({"quote_price": "Infinity"}, "missing_price"),
    ({"quote_date": None}, "missing_date"),
    ({"as_of": None}, "missing_date"),
    ({"quote_date": "invalid"}, "missing_date"),
    ({"as_of": "2026-10-23"}, "stale"),
    ({"quote_date": "2026-10-23"}, "stale"),
    ({"as_of": "2026-10-28"}, "future_date"),
    ({"quote_date": "2026-10-28"}, "future_date"),
    ({"return_status": "pending"}, "unconfirmed_quote"),
    ({"return_status": None, "source": "unknown", "quote_session": None}, "unconfirmed_quote"),
    ({"confidence": "needs_review"}, "unconfirmed_quote"),
])
def test_partial_or_invalid_evidence_is_review(change, issue):
    result = sync_health.overseas_quote_health([holding(), holding("MU", **change)], NOW)
    assert result["status"] == "review"
    assert result["coverage"]["covered"] == 1
    assert issue in result["coverage"]["items"][1]["issues"]


@pytest.mark.parametrize("status,source,session", [
    ("confirmed", "yahoo_close", "close"),
    ("estimated", "databento_estimate", "overnight"),
    (None, "ibkr_tws_estimate", "premarket"),
    (None, "unknown", "intraday"),
])
def test_complete_confirmed_or_estimated_quotes_are_healthy(status, source, session):
    result = sync_health.overseas_quote_health([
        holding(return_status=status, source=source, quote_session=session,
                close_evidence=status == "confirmed", quote_time="2026-10-27T04:40:00+08:00"),
    ], NOW)
    assert result["status"] == "ok"
    assert result["coverage"]["covered"] == 1
    assert result["coverage"]["items"][0]["return_status"] == (status or "estimated")


def test_empty_coverage_is_review_not_zero_valued_financial_fact():
    result = sync_health.overseas_quote_health([], NOW)
    assert result["status"] == "review"
    assert result["rows"] == 0
    assert result["as_of"] is None
    assert "market_value" not in result


@pytest.mark.parametrize("row", [
    holding(quantity=0), holding("CASH_USD", quantity=100),
    holding("CASH_USD", quantity=-100), holding(asset_type="现金"),
    holding(name="Cash USD", quantity=100),
])
def test_cash_and_closed_rows_do_not_require_security_quotes(row):
    assert sync_health.overseas_quote_health([row], NOW)["coverage"]["expected"] == 0


@pytest.mark.parametrize("changes,issue", [
    ({"return_status": "estimated", "source": "databento_estimate", "quote_session": "intraday",
      "close_evidence": False}, "missing_quote_time"),
    ({"return_status": "estimated", "close_evidence": False,
      "quote_time": "2026-10-23T15:00:00-04:00"}, "invalid_quote_time"),
    ({"return_status": "confirmed", "close_evidence": False}, "unverified_close"),
    ({"quote_time": "2026-10-23T16:00:00-04:00"}, "invalid_quote_time"),
])
def test_write_time_and_date_labels_do_not_replace_quote_time_or_close_evidence(changes, issue):
    result = sync_health.overseas_quote_health([holding(**changes)], NOW)
    assert result["status"] == "review"
    assert issue in result["coverage"]["items"][0]["issues"]


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-26T15:59:00-04:00", "2026-10-26"),
    ("2026-10-26T16:01:00-04:00", "2026-10-26"),
    ("2026-10-24T12:00:00-04:00", "2026-10-23"),
    ("2026-12-25T18:00:00-05:00", "2026-12-24"),
    ("2026-11-27T12:59:00-05:00", "2026-11-27"),
    ("2026-11-27T13:01:00-05:00", "2026-11-27"),
    ("2026-12-01T16:01:00-05:00", "2026-12-01"),
])
def test_calendar_handles_session_close_weekend_holiday_half_day_and_dst(stamp, expected):
    assert sync_health._overseas_due_date(holding(), datetime.fromisoformat(stamp)) == date.fromisoformat(expected)


def test_reuses_symbol_market_identification_not_usd_equals_america():
    row = holding("005930.KS", currency="USD", market=None, listing_region=None)
    now = datetime.fromisoformat("2026-10-26T08:00:00+00:00")
    assert sync_health._overseas_due_date(row, now) == date(2026, 10, 26)
    assert sync_health._overseas_due_date(holding(), now) == date(2026, 10, 26)


def test_us_listed_foreign_economic_exposure_uses_listing_calendar():
    row = holding("BABA", economic_region="中国大陆")
    assert sync_health._overseas_due_date(row, NOW) == date(2026, 10, 26)


def test_listed_fund_uses_existing_security_market_inference():
    row = holding("QQQ", market="基金", listing_region="全球", asset_type="ETF / 基金")
    assert sync_health._overseas_due_date(row, NOW) == date(2026, 10, 26)
    row = holding("005930.KS", market="基金", currency="USD", listing_region=None)
    assert sync_health._overseas_due_date(row, datetime.fromisoformat("2026-10-26T08:00:00Z")) == date(2026, 10, 26)


def test_unspecified_europe_is_not_assumed_to_be_sweden_or_us():
    row = holding("TEST", currency="USD", market="欧股", listing_region="欧洲")
    result = sync_health.overseas_quote_health([row], NOW)
    assert result["status"] == "review"
    assert result["coverage"]["items"][0]["issues"] == ["unknown_market_calendar"]


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows


def test_sql_contract_ranks_all_positions_before_excluding_closed_and_reads_only():
    cur = Cursor([holding()])
    assert sync_health.read_overseas_quote_health(cur, NOW)["status"] == "ok"
    assert len(cur.calls) == 1
    query, params = cur.calls[0]
    assert params == (NOW.date(), "美股", "美股")
    assert "PARTITION BY account_code, currency, COALESCE(NULLIF(symbol, ''), name)" in query
    assert "COALESCE(NULLIF(symbol, ''), name)" in query
    assert "ORDER BY as_of DESC, updated_at DESC, id DESC" in query
    assert "h.rn = 1 AND h.quantity IS DISTINCT FROM 0" in query
    assert "complete_account_snapshot AS" in query and "JOIN current_members cm" in query
    assert "pams_market_quote_snapshot" in query and "pams_asset_market_daily" in query
    assert "o.market = 'YF'" in query
    assert "m.asset_id = h.asset_id OR m.primary_symbol = h.symbol" in query
    assert "s.currency = h.currency" in query and "d.currency = h.currency" in query
    assert not set(query.upper().split()) & {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}


def test_explicit_market_freshness_bypasses_three_day_grace_only_when_requested(monkeypatch):
    monkeypatch.setattr(sync_center, "_now_local", lambda: NOW)
    kwargs = dict(id="market-us-stock", source="test", sync_type="行情刷新", latest_date="2026-10-19",
                  action="test", href="test")
    assert sync_center._row(**kwargs)["status"] == "review"
    assert sync_center._row(**kwargs, freshness_checked=True)["status"] == "ok"
    assert sync_center._row(**{**kwargs, "latest_date": "2026-10-23"})["status"] == "ok"


def test_center_changes_only_overseas_row_and_derived_health_counts(monkeypatch):
    from services import ibkr_performance

    class CenterCursor(Cursor):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def fetchone(self):
            return {"as_of": date(2026, 10, 26), "updated_at": NOW, "rows": 4,
                    "market_value": Decimal("999.99"), "review_rows": 0}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self, **_):
            return CenterCursor([])

    quality = dict(status="ok", label="正常", statement_as_of="2026-10-26", active_row_count=4,
                   expected_statement_as_of="2026-10-26", last_attempt_status="success")
    monkeypatch.setattr(sync_center, "get_conn", Connection)
    monkeypatch.setattr(sync_center, "_now_local", lambda: NOW)
    monkeypatch.setattr(sync_center, "read_ibkr_reconciliation", lambda: dict(status="ok", label="正常", details=""))
    monkeypatch.setattr(sync_center, "_latest_sync_run", lambda *_: None)
    monkeypatch.setattr(ibkr_performance, "published_report_status", lambda _: quality)
    monkeypatch.setattr(sync_center, "scheduler_health", lambda _: dict(status="ok", label="正常", jobs={}))
    healthy = sync_health.overseas_quote_health([holding()], NOW)
    partial = sync_health.overseas_quote_health([holding(quote_date="2026-10-23")], NOW)
    monkeypatch.setattr(sync_center, "read_overseas_quote_health", lambda *_: healthy)
    before = sync_center.sync_center()
    monkeypatch.setattr(sync_center, "read_overseas_quote_health", lambda *_: partial)
    after = sync_center.sync_center()
    assert [r for r in before["rows"] if r["id"] != "market-us-stock"] == [
        r for r in after["rows"] if r["id"] != "market-us-stock"
    ]
    us = next(r for r in after["rows"] if r["id"] == "market-us-stock")
    assert us["status"] == "review" and us["coverage"]["stale"] == 1
    assert set(us) == {"id", "source", "sync_type", "latest_date", "latest_time", "rows", "status",
                       "status_label", "action", "href", "details", "trigger_endpoint", "trigger_body", "coverage"}
    assert after["summary"]["ok"] == before["summary"]["ok"] - 1
    assert after["summary"]["reviews"] == before["summary"]["reviews"] + 1
    assert after["summary"]["last_investment_date"] == before["summary"]["last_investment_date"]


@pytest.fixture
def pg_health():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PAMS_TEST_POSTGRES_DSN must identify an isolated PostgreSQL server")
    conn = psycopg2.connect(dsn, connect_timeout=5)
    definitions = {
        "pams_holdings_snapshot": "id bigserial, account_code text, asset_id text, symbol text, name text, currency text, "
            "as_of date, updated_at timestamptz, current_price numeric, quantity numeric, "
            "asset_liability text, major_category text, minor_category text",
        "pams_asset_master": "asset_id text, primary_symbol text, status text, market text, asset_type text, listing_region text",
        "pams_market_quote_snapshot": "quote_id bigserial, quote_date date, price numeric, return_status text, source text, "
            "quote_session text, raw_json jsonb, market text, price_time timestamptz, updated_at timestamptz, currency text, "
            "asset_id text, symbol text",
        "pams_asset_market_daily": "as_of date, estimated_unit_nav numeric, close_price numeric, unit_nav numeric, "
            "source text, confidence text, market text, updated_at timestamptz, raw_json jsonb, source_key text, "
            "currency text, asset_id text, symbol text",
        "pams_ohlcv_daily": "trade_date date, close numeric, symbol text, market text",
    }
    with conn.cursor() as cur:
        for table, fields in definitions.items():
            cur.execute(sql.SQL("CREATE TEMP TABLE {} ({})").format(sql.Identifier(table), sql.SQL(fields)))
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def insert_rows(conn, table, rows):
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, row)),
                sql.SQL(",").join(sql.Placeholder() for _ in row)),
                [Json(value) if isinstance(value, dict) else value for value in row.values()])


def pg_position(symbol="QQQ", day="2026-10-26", **changes):
    return {"account_code": "test_broker", "asset_id": symbol, "symbol": symbol, "name": symbol,
            "currency": "USD", "as_of": day, "updated_at": NOW, "current_price": 123.45, "quantity": 1,
            "asset_liability": "资产", "major_category": "美股", "minor_category": "美股个股", **changes}


def pg_quote(symbol="QQQ", day="2026-10-26", **changes):
    return {"asset_id": symbol, "symbol": symbol, "currency": "USD", "quote_date": day, "price": 123.45,
            "return_status": "confirmed", "source": "yahoo_close", "quote_session": "close", "raw_json": {},
            "updated_at": NOW, **changes}


def pg_read_only_health(conn, now=NOW):
    conn.commit()
    conn.set_session(readonly=True)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()["transaction_read_only"] == "on"
        cur.execute("SELECT jsonb_agg(to_jsonb(h) ORDER BY id) AS rows FROM pams_holdings_snapshot h")
        before = cur.fetchone()["rows"]
        result = sync_health.read_overseas_quote_health(cur, now)
        cur.execute("SELECT jsonb_agg(to_jsonb(h) ORDER BY id) AS rows FROM pams_holdings_snapshot h")
        assert cur.fetchone()["rows"] == before
        return result


@pytest.mark.parametrize("provider", ["snapshot", "daily", "ohlcv"])
def test_postgres_complete_membership_partial_refresh_and_all_quote_sources(pg_health, provider):
    conn = pg_health
    old = [pg_position(symbol, "2026-10-23") for symbol in ("QQQ", "MU", "SOLD", "CLOSED", "005930.KS")]
    current = [pg_position(symbol) for symbol in ("QQQ", "MU", "005930.KS")]
    current += [pg_position("CLOSED", quantity=0), pg_position("CASH_USD", quantity=200),
                pg_position("CASH_KRW", quantity=-100, currency="KRW")]
    insert_rows(conn, "pams_holdings_snapshot", old + current + [
        pg_position("QQQ", "2026-10-27"),  # Partial quote refresh is not a new complete account universe.
        pg_position("QQQ", "2026-10-23", account_code="other_broker"),
    ])
    for symbol, day in (("QQQ", "2026-10-26"), ("005930.KS", "2026-10-23")):
        if provider == "snapshot":
            insert_rows(conn, "pams_market_quote_snapshot", [pg_quote(symbol, day)])
        elif provider == "daily":
            insert_rows(conn, "pams_asset_market_daily", [dict(symbol=symbol, asset_id=symbol, as_of=day,
                currency="USD", close_price=123.45, source="yahoo_close", source_key=symbol, raw_json={})])
        else:
            insert_rows(conn, "pams_ohlcv_daily", [dict(symbol=symbol, trade_date=day, market="YF", close=123.45)])
    if provider == "snapshot":
        insert_rows(conn, "pams_market_quote_snapshot", [pg_quote("MU", currency="KRW")])
    result = pg_read_only_health(conn)
    coverage = result["coverage"]
    assert result["status"] == "review"
    assert (coverage["expected"], coverage["covered"], coverage["missing"], coverage["stale"]) == (4, 1, 1, 3)
    assert {item["symbol"] for item in coverage["items"]} == {"QQQ", "MU", "005930.KS"}
    assert sum(item["symbol"] == "QQQ" for item in coverage["items"]) == 2


@pytest.mark.parametrize("kind", ["empty", "closed", "cash", "negative_cash"])
def test_postgres_empty_closed_and_cash_only_universe(pg_health, kind):
    if kind == "closed":
        insert_rows(pg_health, "pams_holdings_snapshot", [pg_position("OLD", "2026-10-23"), pg_position("OLD", quantity=0)])
    elif kind in {"cash", "negative_cash"}:
        insert_rows(pg_health, "pams_holdings_snapshot", [pg_position("CASH_USD", quantity=100 if kind == "cash" else -100)])
    result = pg_read_only_health(pg_health)
    assert result["status"] == "review" and result["coverage"]["expected"] == 0


@pytest.mark.parametrize("stamp,symbol,currency,quote_day,source,session,quote_time,expected,issue", [
    ("2026-10-26T12:00:00-04:00", "QQQ", "USD", "2026-10-23", "yahoo_close", "close", None, "review", "stale"),
    ("2026-10-26T12:00:00-04:00", "QQQ", "USD", "2026-10-26", "databento_estimate", "intraday", "2026-10-26T11:59:00-04:00", "ok", None),
    ("2026-10-26T12:00:00-04:00", "QQQ", "USD", "2026-10-26", "databento_estimate", "intraday", None, "review", "missing_quote_time"),
    ("2026-10-26T12:00:00-04:00", "QQQ", "USD", "2026-10-26", "yahoo_close", "close", None, "review", "premature_close"),
    ("2026-12-25T12:00:00-05:00", "QQQ", "USD", "2026-12-24", "yahoo_close", "close", None, "ok", None),
    ("2026-10-26T11:00:00+09:00", "005930.KS", "KRW", "2026-10-23", "yahoo_close", "close", None, "review", "stale"),
    ("2026-10-26T11:00:00+08:00", "3690.HK", "HKD", "2026-10-23", "yahoo_close", "close", None, "review", "stale"),
    ("2026-10-26T09:15:00+08:00", "3690.HK", "HKD", "2026-10-23", "yahoo_estimate", "premarket", "2026-10-23T09:15:00+08:00", "review", "stale"),
])
def test_postgres_intraday_and_holiday_evidence(pg_health, stamp, symbol, currency, quote_day, source, session, quote_time, expected, issue):
    now = datetime.fromisoformat(stamp)
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    insert_rows(pg_health, "pams_holdings_snapshot", [pg_position(symbol, day, currency=currency)])
    insert_rows(pg_health, "pams_market_quote_snapshot", [pg_quote(symbol, quote_day, currency=currency,
        source=source, quote_session=session, price_time=quote_time,
        return_status="estimated" if "estimate" in source else "confirmed")])
    result = pg_read_only_health(pg_health, now)
    assert result["status"] == expected
    if issue:
        assert issue in result["coverage"]["items"][0]["issues"]
