"""Non-US closes are dated quote evidence, never replacement account snapshots."""
from __future__ import annotations

import json
import socket
import sys
import urllib.request
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))

from endpoints import prices
from domain.market import overseas_return_calendar
from tests.scheduler.test_asset_refresh_scheduler import _load_scheduler
from tests.market.test_us_quote_projection import (
    BASE_DAY,
    QUOTE_DAY,
    _holdings_bytes,
    _insert,
    _position,
    _project,
    _quote_rows,
    _write_quote,
    pg_projection,  # noqa: F401
)

NOW = datetime(2026, 10, 28, 19, tzinfo=UTC)
CASES = [("kr", "012345.KS", "KRW"), ("hk", "1234.HK", "HKD"),
         ("se", "SIVE", "SEK"), ("ch", "TEST.SW", "CHF")]


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch, request):
    def deny(*args, **kwargs):
        pytest.fail("External network or production database access is forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    if "pg_projection" not in request.fixturenames:
        monkeypatch.setattr(prices, "_get_conn", deny)


def _row(symbol, currency, **changes):
    return {"id": 1, "symbol": symbol, "currency": currency, "name": symbol,
            "major_category": prices._OVERSEAS_MAJOR_CATEGORY, "asset_liability": "资产",
            "account_code": "test", "quantity": Decimal("3.125"), "cost": Decimal("100.13"),
            "buy_price": Decimal("32.0416"), "as_of": BASE_DAY, **changes}


class NumericDB:
    """Mock only OHLCV storage; emulate NUMERIC scale on the database boundary."""
    def __init__(self):
        self.closes = {}
        self.commits = 0
        self.quotes = []
        self.quote_reads = 0
        self.writes = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return self

    def executemany(self, query, payload):
        assert "INSERT INTO pams_ohlcv_daily" in query
        self.writes += len(payload)
        for symbol, market, day, close in payload:
            assert market == "YF" and isinstance(close, Decimal)
            self.closes[(symbol, day)] = close.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    def execute(self, query, params):
        if "FROM (SELECT * FROM market_observations WHERE observation_type = 'quote')" in query:
            for gate in ("quote_date = %s", "market = %s", "return_status = 'confirmed'",
                         "quote_session = 'close'", "price > 0", "'NaN', 'Infinity', '-Infinity'",
                         "valuation_projection_only", "provider = 'yahoo'", "source = 'yahoo_close'",
                         "q.price = current_close.close", "previous_close.close > 0"):
                assert gate in query
            self.quote_reads += 1
            self.selected = None
            self.evidence_dates = params[:2]
            return
        assert "FROM pams_ohlcv_daily" in query and "trade_date = " in query
        self.selected = params

    def fetchall(self):
        if self.selected is None:
            return [row for row in self.quotes if all(self.closes.get((row["symbol"], day), 0) > 0
                                                     for day in self.evidence_dates)]
        symbols, days = self.selected
        days = days if isinstance(days, list) else [days]
        return [{"symbol": symbol, "trade_date": day, "close": self.closes[(symbol, day)]}
                for symbol in symbols for day in days if self.closes.get((symbol, day), 0) > 0]

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize("bucket,symbol,currency", CASES)
def test_scheduled_local_close_uses_calendar_date_and_database_precision(monkeypatch, bucket, symbol, currency):
    db, saved, calls = NumericDB(), [], []
    rows = [_row(symbol, currency), _row(symbol, currency, id=2, account_code="second")]
    before = deepcopy(rows)
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    def holdings(category, **kwargs):
        assert category == prices._OVERSEAS_MAJOR_CATEGORY and kwargs["market_buckets"] == (bucket,)
        return rows
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", holdings)
    monkeypatch.setattr(overseas_return_calendar, "latest_completed_session", lambda market: QUOTE_DAY if market == bucket else None)
    monkeypatch.setattr(prices, "_price_refresh_as_of", lambda *args: pytest.fail("Not the US 07:00 date"))
    monkeypatch.setattr(prices, "_upsert_price_snapshots", lambda *args: pytest.fail("No holdings writes"))
    def fetch(symbols):
        calls.extend(symbols)
        prices._YFINANCE_CLOSE_CACHE.update({provider: [(BASE_DAY, 704.4), (QUOTE_DAY, 712.2000122070312),
                                                      (QUOTE_DAY + timedelta(days=1), 999)] for provider in symbols})
        return {provider: 999.0 for provider in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda cur, quote: saved.append(quote))
    payload = json.loads(prices.refresh_us_stock(market=bucket, price_basis="estimate", scheduled=True).body)
    assert calls == ["SIVE.ST" if bucket == "se" else symbol]
    assert payload["as_of"] == QUOTE_DAY.isoformat() and payload["price_basis"] == "close"
    assert payload["found"] == payload["updated"] == payload["daily_quote_rows"] == 2
    assert payload["valuation_projection_only"] is True and not payload["errors"]
    assert db.commits == 1 and len(db.closes) == 2
    assert all(quote["price"] == Decimal("712.2000") and isinstance(quote["price"], Decimal) for quote in saved)
    assert all(quote["symbol"] == symbol and quote["market"] == bucket.upper()
               and quote["currency"] == currency and quote["return_status"] == "confirmed"
               and quote["quote_date"] == QUOTE_DAY for quote in saved)
    assert rows == before


@pytest.mark.parametrize("closes", [[], [(BASE_DAY, 100)], [(QUOTE_DAY, float("nan"))],
    [(QUOTE_DAY, float("inf"))], [(QUOTE_DAY, float("-inf"))], [(QUOTE_DAY, 0)],
    [(QUOTE_DAY, -1)], [(QUOTE_DAY + timedelta(days=1), 101)], [(QUOTE_DAY, "invalid")]])
def test_missing_or_invalid_target_close_cannot_be_stamped(monkeypatch, closes):
    rows = [_row("TEST.ST", "SEK")]
    assert prices._upsert_scheduled_overseas_quotes(rows, "se", QUOTE_DAY, {"TEST.ST": closes}) == (0, 0, set())


def test_numeric_rounding_to_zero_is_not_a_quote(monkeypatch):
    db = NumericDB()
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda *args: pytest.fail("Zero cannot mark a position"))
    result = prices._upsert_scheduled_overseas_quotes([_row("TEST.ST", "SEK")], "se", QUOTE_DAY,
                                                    {"TEST.ST": [(BASE_DAY, 100), (QUOTE_DAY, 0.00001)]})
    assert result == (2, 0, set())


def test_fetch_last_price_without_target_history_remains_incomplete(monkeypatch):
    monkeypatch.setattr(prices, "_get_conn", lambda: NumericDB())
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: [_row("TEST.ST", "SEK")])
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda symbols: {symbols[0]: 123})
    monkeypatch.setattr(prices, "_YFINANCE_CLOSE_CACHE", {"TEST.ST": [(QUOTE_DAY, 456)]})
    result = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert result["updated"] == 0 and result["errors"]


def test_complete_matching_evidence_skips_network_and_all_writes(monkeypatch):
    db = NumericDB()
    rows = [_row("FIRST.ST", "SEK"), _row("SECOND.ST", "SEK", id=2),
            _row("FIRST.ST", "SEK", id=3, account_code="second")]
    db.quotes = [{"symbol": row["symbol"], "currency": row["currency"]} for row in rows[:2]]
    db.closes = {(row["symbol"], day): Decimal(100) for row in rows for day in (BASE_DAY, QUOTE_DAY)}
    before_closes = dict(db.closes)
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: rows)
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("Completed evidence must not download"))
    monkeypatch.setattr(prices, "_upsert_scheduled_overseas_quotes", lambda *args: pytest.fail("Completed evidence must not write"))
    result = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert result["status"] == "ok" and result["unchanged"] is True
    assert result["found"] == result["covered"] == 3 and result["updated"] == 0 and not result["errors"]
    assert result["close_rows"] == result["daily_quote_rows"] == db.commits == 0
    assert db.quote_reads == 1 and db.writes == 0 and db.closes == before_closes


@pytest.mark.parametrize("evidence", [[], [{"symbol": "FIRST.ST", "currency": "SEK"}],
    [{"symbol": "FIRST.ST", "currency": "SEK"}, {"symbol": "SECOND.ST", "currency": "USD"}]])
def test_partial_or_wrong_currency_coverage_does_not_skip_refresh(monkeypatch, evidence):
    db, calls = NumericDB(), []
    db.quotes = evidence
    rows = [_row("FIRST.ST", "SEK"), _row("SECOND.ST", "SEK", id=2)]
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: rows)
    def fetch(symbols):
        calls.extend(symbols)
        prices._YFINANCE_CLOSE_CACHE.update({symbol: [(BASE_DAY, 100), (QUOTE_DAY, 101)] for symbol in symbols})
        return {symbol: 101 for symbol in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda *args: None)
    result = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert calls == ["FIRST.ST", "SECOND.ST"] and result["updated"] == 2
    assert result.get("unchanged") is not True and db.commits == 1


def test_empty_holdings_cannot_be_unchanged_success(monkeypatch):
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: [])
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("Empty holdings"))
    result = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert result["found"] == result["updated"] == 0 and result.get("unchanged") is not True


@pytest.mark.parametrize("has_previous", [False, True])
def test_only_target_and_previous_exchange_session_can_be_written(monkeypatch, has_previous):
    db = NumericDB()
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda *args: None)
    history = [(BASE_DAY - timedelta(days=offset), 80 + offset) for offset in range(1, 8)]
    if has_previous:
        history.append((BASE_DAY, 100))
    history.extend([(QUOTE_DAY, 101), (QUOTE_DAY + timedelta(days=1), 999)])
    result = prices._upsert_scheduled_overseas_quotes([_row("TEST.ST", "SEK")], "se", QUOTE_DAY,
                                                    {"TEST.ST": history})
    assert set(db.closes) == ({("TEST.ST", QUOTE_DAY), ("TEST.ST", BASE_DAY)} if has_previous else set())
    assert result == ((2, 1, {"TEST.ST"}) if has_previous else (0, 0, set()))


@pytest.mark.parametrize("bucket", ["kr", "hk", "se", "ch"])
def test_no_completed_calendar_session_skips_without_fetch_or_write(monkeypatch, bucket):
    monkeypatch.setattr(overseas_return_calendar, "latest_completed_session", lambda market: None)
    monkeypatch.setattr(prices, "_refresh_scheduled_overseas_quotes", lambda *args: pytest.fail("No completed session"))
    payload = json.loads(prices.refresh_us_stock(market=bucket, scheduled=True).body)
    assert payload == {"status": "skipped", "reason": "no_completed_overseas_session", "market": bucket}


@pytest.mark.parametrize("symbol,currency,bucket", [("TEST.ST", "USD", "se"), ("TEST.SW", "USD", "ch"),
    ("OTHER", "SEK", "se"), ("OTHER", "CHF", "ch"), ("AAPL", "USD", "us"), ("700", "USD", "hk")])
def test_market_filter_uses_listing_before_currency(symbol, currency, bucket):
    assert prices._overseas_market_bucket(_row(symbol, currency)) == bucket
    if bucket != "us":
        assert prices._normalize_overseas_market_filter(bucket) == (bucket,)
        if "." in symbol:
            assert not prices._looks_like_us_stock_symbol(symbol)


@pytest.mark.parametrize("symbol,bucket,expected", [("SIVE", "se", "SIVE.ST"), ("GENERIC", "se", "GENERIC.ST"),
    ("GENERIC", "ch", "GENERIC.SW"), ("005930", "kr", "005930.KS"), ("700", "hk", "0700.HK"),
    ("VOLV-B.ST", "se", "VOLV-B.ST"), ("AAPL", "us", "AAPL"), ("SIVE", None, "SIVE")])
def test_provider_symbol_normalization_is_market_based(symbol, bucket, expected):
    assert prices._yfinance_quote_symbol(symbol, bucket) == expected


@pytest.mark.parametrize("failure", ["kr", "hk", "se", "ch"])
def test_polling_isolated_failures_preserve_other_jobs_and_rate_limit(monkeypatch, tmp_path, failure):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "refresh.json"
    old = {"status": "waiting", "recent_runs": []}
    path.write_text(json.dumps({"version": 1, "jobs": {"ibkr_flex": old, "asia": old, "us_quotes": old}}))
    monkeypatch.setattr(module, "STATE_PATH", path)
    calls = []
    def call(endpoint, label):
        market = endpoint.split("market=", 1)[1].split("&", 1)[0]
        calls.append(market)
        if market == failure:
            raise ValueError("token=secret")
        return {"as_of": QUOTE_DAY.isoformat(), "found": 1, "updated": 1}
    monkeypatch.setattr(module, "_call", call)
    monkeypatch.setattr(module, "run_flex_confirmation", lambda: pytest.fail("No Flex dependency"))
    monkeypatch.setattr(module, "refresh_investment_return_snapshots", lambda *args: pytest.fail("No PnL publication"))
    result = module.refresh_overseas_quotes(NOW)
    assert calls == ["kr", "hk", "se", "ch"]
    assert result[failure]["status"] == "error"
    assert all(job["status"] == "success" for market, job in result.items() if market != failure)
    assert all(job["reason"] == "quote_poll_not_due" for job in module.refresh_overseas_quotes(NOW + timedelta(minutes=4)).values())
    assert len(calls) == 4
    restarted = _load_scheduler(monkeypatch)
    monkeypatch.setattr(restarted, "STATE_PATH", path)
    monkeypatch.setattr(restarted, "_call", call)
    restarted.refresh_overseas_quotes(NOW + timedelta(minutes=5))
    assert calls == ["kr", "hk", "se", "ch"] * 2
    jobs = json.loads(path.read_text())["jobs"]
    assert jobs["ibkr_flex"] == jobs["asia"] == jobs["us_quotes"] == old
    assert "secret" not in path.read_text() and not jobs[f"{failure}_quotes"].get("last_success_date")


@pytest.mark.parametrize("response,status", [(None, "blocked"), ({}, "blocked"),
    ({"found": 2, "updated": 1, "as_of": QUOTE_DAY.isoformat()}, "blocked"),
    ({"found": 1, "updated": 1}, "blocked"),
    ({"found": 1, "updated": 1, "as_of": "invalid"}, "error"),
    ({"status": "skipped", "reason": "no_completed_overseas_session"}, "idle"),
    ({"found": 0, "updated": 0, "as_of": QUOTE_DAY.isoformat()}, "idle")])
def test_polling_incomplete_results_never_advance_success(monkeypatch, tmp_path, response, status):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "_call", lambda *args: response)
    results = module.refresh_overseas_quotes(NOW)
    assert all(job["status"] == status and not job.get("last_success_date") for job in results.values())


def test_polling_corrupt_checkpoint_prevents_all_writes(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path = tmp_path / "refresh.json"
    path.write_text("invalid")
    monkeypatch.setattr(module, "STATE_PATH", path)
    monkeypatch.setattr(module, "_call", lambda *args: pytest.fail("Invalid checkpoint"))
    assert all(job["reason"] == "checkpoint_unavailable" for job in module.refresh_overseas_quotes(NOW).values())


def test_busy_market_lock_does_not_block_other_markets(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    path, calls = tmp_path / "state.json", []
    monkeypatch.setattr(module, "STATE_PATH", path)
    monkeypatch.setattr(module, "_call", lambda endpoint, label: calls.append(endpoint) or
                        {"as_of": QUOTE_DAY.isoformat(), "found": 1, "updated": 1})
    with module.job_lock(path, "kr_quotes"):
        result = module.refresh_overseas_quotes(NOW)
    assert result["kr"] == {"status": "deferred", "reason": "job_busy"}
    assert len(calls) == 3 and all("market=kr" not in endpoint for endpoint in calls)
    assert all(result[market]["status"] == "success" for market in ("hk", "se", "ch"))


def test_independent_poll_is_registered_and_runs_before_flex_at_startup(monkeypatch):
    module = _load_scheduler(monkeypatch)
    scheduled, calls = [], []
    class Registration:
        @property
        def day(self):
            return self
        friday = minutes = day
        def at(self, *args):
            return self
        def do(self, callback):
            scheduled.append(callback)
    monkeypatch.setattr(module.schedule, "every", lambda *args: Registration())
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    monkeypatch.setattr(module, "refresh_us_quotes", lambda: calls.append("us"))
    monkeypatch.setattr(module, "refresh_overseas_quotes", lambda: calls.append("overseas"))
    monkeypatch.setattr(module, "refresh_crypto", lambda: calls.append("crypto"))
    class StopStartup(Exception):
        pass
    def flex():
        calls.append("flex")
        raise StopStartup
    monkeypatch.setattr(module, "run_flex_confirmation", flex)
    with pytest.raises(StopStartup):
        module.main()
    assert calls == ["us", "overseas", "crypto", "flex"]
    assert module.refresh_overseas_quotes in scheduled and module.refresh_us_quotes in scheduled


@pytest.fixture
def overseas_pg(pg_projection):  # noqa: F811
    _insert(pg_projection, "pams_holdings_snapshot", [_position("SIVE", "SEK", 7, 100), _position("TEST.SW", "CHF", 11, 100)])
    pg_projection.commit()
    return pg_projection


@pytest.mark.parametrize("bucket,symbol,currency", [("kr", "005930.KS", "KRW"), ("hk", "0700.HK", "HKD"),
    ("se", "SIVE", "SEK"), ("ch", "TEST.SW", "CHF")])
def test_pg16_quote_refresh_preserves_full_account_and_persisted_precision(overseas_pg, monkeypatch, bucket, symbol, currency):
    conn = overseas_pg
    physical, before = _holdings_bytes(conn), _project(conn)
    def fetch(symbols):
        prices._YFINANCE_CLOSE_CACHE.update({provider: [(BASE_DAY, 704.4), (QUOTE_DAY, 712.2000122070312)] for provider in symbols})
        return {provider: 712.2 for provider in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    result = prices._refresh_scheduled_overseas_quotes(bucket, QUOTE_DAY)
    assert result["found"] == result["updated"] and result["found"] > 0 and not result["errors"]
    after = _project(conn)
    assert set(after) == set(before) and len(after) == 18
    # An actual same-day holding takes precedence over a quote-only mark.
    expected_price = before[symbol]["current_price"] if bucket == "kr" else Decimal("712.2000")
    assert after[symbol]["current_price"] == expected_price
    assert after[symbol]["market_value"] == (expected_price * before[symbol]["quantity"]).quantize(Decimal("0.01"))
    for key in after:
        for column in ("quantity", "cost", "buy_price", "account_code", "asset_id"):
            assert after[key][column] == before[key][column]
        if key.startswith("CASH_"):
            assert after[key]["market_value"] == before[key]["market_value"]
    assert _holdings_bytes(conn) == physical
    quotes = _quote_rows(conn)
    assert all(q["market"] == bucket.upper() and q["currency"] == currency and q["price"] == Decimal("712.2000") for q in quotes)
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("Completed session must not download again"))
    monkeypatch.setattr(prices, "_upsert_scheduled_overseas_quotes", lambda *args: pytest.fail("Completed session must not write again"))
    repeated = prices._refresh_scheduled_overseas_quotes(bucket, QUOTE_DAY)
    assert repeated["unchanged"] is True and repeated["found"] == repeated["covered"] == result["found"]
    assert repeated["updated"] == 0
    assert repeated["close_rows"] == repeated["daily_quote_rows"] == 0
    assert _quote_rows(conn) == quotes
    assert _holdings_bytes(conn) == physical


@pytest.mark.parametrize("symbol,market,currency", [("0700.HK", "HK", "HKD"), ("005930.KS", "KR", "KRW"),
    ("SIVE", "SE", "SEK"), ("TEST.SW", "CH", "CHF")])
@pytest.mark.parametrize("changes", [{"close_price": Decimal("NaN")}, {"close_price": Decimal("Infinity")},
    {"close_price": Decimal("-Infinity")}, {"close_price": Decimal(0)}, {"close_price": Decimal(-1)},
    {"as_of": BASE_DAY}, {"as_of": QUOTE_DAY + timedelta(days=1)}, {"market": "US"},
    {"currency": "USD"}, {"raw_json": {}}, {"return_status": "pending"}])
def test_pg16_non_us_projection_retains_all_quote_guards(overseas_pg, symbol, market, currency, changes):
    conn = overseas_pg
    before, physical = _project(conn), _holdings_bytes(conn)
    _write_quote(conn, symbol, **{"market": market, "currency": currency, **changes})
    assert _project(conn) == before
    assert _holdings_bytes(conn) == physical


def test_pg16_quote_failure_rolls_back_ohlcv_and_quotes(overseas_pg, monkeypatch):
    conn = overseas_pg
    rows = [_row("SIVE", "SEK"), _row("TEST.ST", "SEK", id=2)]
    before, physical = _quote_rows(conn), _holdings_bytes(conn)
    conn.commit()
    real_upsert, calls = prices.upsert_quote_snapshot, []
    def fail_second(cur, quote):
        calls.append(quote["symbol"])
        if len(calls) == 2:
            raise ValueError("injected")
        return real_upsert(cur, quote)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", fail_second)
    with pytest.raises(ValueError, match="injected"):
        prices._upsert_scheduled_overseas_quotes(rows, "se", QUOTE_DAY,
            {row["symbol"]: [(BASE_DAY, 704.4), (QUOTE_DAY, 712.2000122070312)] for row in rows})
    assert _quote_rows(conn) == before and _holdings_bytes(conn) == physical
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pams_ohlcv_daily")
        assert cur.fetchone()[0] == 0


def test_pg16_new_kr_quote_marks_request_day_without_new_account_snapshot(overseas_pg, monkeypatch):
    conn = overseas_pg
    original_day, quote_day = date(2026, 10, 27), date(2026, 10, 28)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pams_holdings_snapshot")
    baseline = [_position(symbol, currency, 3, 100, day=original_day)
                for symbol, currency in (("AAPL", "USD"), ("0700.HK", "HKD"), ("005930.KS", "KRW"),
                                         ("SIVE", "SEK"), ("TEST.SW", "CHF"))]
    baseline += [_position(f"CASH_{ccy}", ccy, amount, 1, day=original_day)
                 for ccy, amount in (("USD", "-125.75"), ("HKD", "1024.36"), ("KRW", "300001.25"),
                                     ("SEK", "720.13"), ("CHF", "150.01"))]
    _insert(conn, "pams_holdings_snapshot", baseline)
    conn.commit()
    before, physical = _project(conn, as_of=original_day), _holdings_bytes(conn)
    def fetch(symbols):
        prices._YFINANCE_CLOSE_CACHE.update({symbol: [(original_day, 100), (quote_day, 712.2000122070312)] for symbol in symbols})
        return {symbol: 712.2 for symbol in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    assert prices._refresh_scheduled_overseas_quotes("kr", quote_day)["updated"] == 1
    today = _project(conn, as_of=quote_day)
    assert set(today) == set(before) and len(today) == 10
    assert today["005930.KS"]["current_price"] == Decimal("712.2000")
    assert all(row["as_of"] == quote_day for row in today.values())
    assert _project(conn, as_of=original_day) == before
    for symbol in before:
        for column in ("quantity", "cost", "buy_price", "account_code", "asset_id"):
            assert today[symbol][column] == before[symbol][column]
        if symbol != "005930.KS":
            for column in ("market_value", "current_price", "holding_pnl"):
                assert today[symbol][column] == before[symbol][column]
    assert _holdings_bytes(conn) == physical


@pytest.mark.parametrize("symbol", ["OTHER.ST", "OTHER.SW"])
def test_pg16_us_quote_cannot_mark_non_us_listing_even_with_usd_currency(overseas_pg, symbol):
    conn = overseas_pg
    _insert(conn, "pams_holdings_snapshot", [_position(symbol, "USD", 7, 100)])
    conn.commit()
    before = _project(conn)
    _write_quote(conn, symbol, market="US", currency="USD")
    assert _project(conn) == before


@pytest.mark.parametrize("changes,complete", [({}, True),
    ({"close_price": Decimal("NaN")}, False), ({"close_price": Decimal("Infinity")}, False),
    ({"close_price": Decimal("-Infinity")}, False), ({"close_price": Decimal(0)}, False),
    ({"close_price": Decimal(-1)}, False), ({"return_status": "estimated"}, False),
    ({"return_status": "pending"}, False), ({"source": "yahoo_intraday_close"}, False),
    ({"source": "yahoo_nav_close"}, False), ({"as_of": BASE_DAY}, False),
    ({"as_of": QUOTE_DAY + timedelta(days=1)}, False), ({"market": "CH"}, False),
    ({"currency": "CHF"}, False), ({"raw_json": {}}, False),
    ({"raw_json": {"valuation_projection_only": False}}, False),
    ({"raw_json": {"valuation_projection_only": "true"}}, False),
    ({"provider": "unknown"}, False), ({"source": "databento_estimate"}, False),
    ({"close_price": Decimal(101)}, False)])
def test_pg16_completed_quote_gate_requires_all_matching_close_evidence(overseas_pg, changes, complete):
    conn = overseas_pg
    _insert(conn, "pams_ohlcv_daily", [{"symbol": "SIVE", "market": "YF", "trade_date": day,
                                      "close": close} for day, close in ((BASE_DAY, 100), (QUOTE_DAY, Decimal("999999.99")))])
    conn.commit()
    _write_quote(conn, "SIVE", **{"market": "SE", "currency": "SEK", "source": "yahoo_close", "provider": "yahoo",
                                 "return_status": "confirmed", **changes})
    assert prices._has_completed_overseas_quotes([_row("SIVE", "SEK")], "se", QUOTE_DAY) is complete


def test_pg16_partial_evidence_refreshes_then_complete_evidence_is_read_only(overseas_pg, monkeypatch):
    conn, calls = overseas_pg, []
    _write_quote(conn, "SIVE", market="SE", currency="SEK", source="yahoo_close", return_status="confirmed")
    physical = _holdings_bytes(conn)
    def fetch(symbols):
        calls.extend(symbols)
        prices._YFINANCE_CLOSE_CACHE.update({symbol: [(BASE_DAY, 100), (QUOTE_DAY, 101)] for symbol in symbols})
        return {symbol: 101 for symbol in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    partial = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert set(calls) == {"SIVE.ST", "VOLV-B.ST"}
    assert partial["found"] == partial["updated"] == 2 and partial.get("unchanged") is not True
    quotes = _quote_rows(conn)
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("Complete evidence must not download"))
    monkeypatch.setattr(prices, "_upsert_scheduled_overseas_quotes", lambda *args: pytest.fail("Complete evidence must not write"))
    complete = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert complete["status"] == "ok" and complete["unchanged"] is True
    assert complete["found"] == complete["covered"] == 2 and complete["updated"] == 0
    assert _quote_rows(conn) == quotes and _holdings_bytes(conn) == physical


def test_pg16_normal_refresh_leaves_unrelated_history_untouched(overseas_pg):
    conn = overseas_pg
    old_days = [BASE_DAY - timedelta(days=offset) for offset in range(1, 8)]
    _insert(conn, "pams_ohlcv_daily", [{"symbol": "SIVE", "market": "YF", "trade_date": day,
                                      "close": Decimal("88.1234")} for day in old_days])
    conn.commit()
    history = [(day, 99.0) for day in old_days]
    history += [(BASE_DAY, 704.4), (QUOTE_DAY, 712.2000122070312), (QUOTE_DAY + timedelta(days=1), 999.0)]
    result = prices._upsert_scheduled_overseas_quotes([_row("SIVE", "SEK")], "se", QUOTE_DAY, {"SIVE": history})
    assert result == (2, 1, {"SIVE"})
    with conn.cursor() as cur:
        cur.execute("SELECT trade_date, close FROM pams_ohlcv_daily WHERE symbol='SIVE'")
        stored = dict(cur.fetchall())
    assert set(stored) == set(old_days) | {BASE_DAY, QUOTE_DAY}
    assert all(stored[day] == Decimal("88.1234") for day in old_days)
    assert stored[BASE_DAY] == Decimal("704.4000") and stored[QUOTE_DAY] == Decimal("712.2000")


@pytest.mark.parametrize("bucket,symbol,expected", [("ch", "TEST.SW", "CHF"), ("hk", "TEST.HK", "HKD"),
    ("hk", "700", "HKD"), ("kr", "TEST.KS", "KRW"), ("se", "TEST.ST", "SEK")])
def test_currency_mismatch_rejects_scheduled_batch_before_network_or_writes(monkeypatch, bucket, symbol, expected):
    rows = [_row(symbol, "USD")]
    before = deepcopy(rows)
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: rows)
    monkeypatch.setattr(overseas_return_calendar, "latest_completed_session", lambda market: QUOTE_DAY)
    monkeypatch.setattr(prices, "_has_completed_overseas_quotes", lambda *args: pytest.fail("Invalid evidence cannot short-circuit"))
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("No FX guessing"))
    result = json.loads(prices.refresh_us_stock(market=bucket, scheduled=True).body)
    assert result["status"] == "needs_review" and result["reason"] == "currency_mismatch"
    assert result["found"] == 1 and result["updated"] == result["covered"] == result["daily_quote_rows"] == 0
    assert result["errors"] == ["currency_mismatch"]
    assert result["manual_review_items"] == [{"reason": "currency_mismatch", "symbol": symbol,
        "account_code": "test", "currency": "USD", "expected_currency": expected, "market": bucket}]
    assert rows == before


@pytest.mark.parametrize("bucket,symbol", [("ch", "TEST.SW"), ("hk", "TEST.HK"), ("hk", "700"),
    ("kr", "TEST.KS"), ("se", "TEST.ST")])
def test_direct_writer_and_evidence_gate_reject_wrong_currency_without_db_access(bucket, symbol):
    rows = [_row(symbol, "USD")]
    with pytest.raises(ValueError, match="currency_mismatch: needs_review"):
        prices._upsert_scheduled_overseas_quotes(rows, bucket, QUOTE_DAY, {symbol: [(QUOTE_DAY, 101.0)]})
    assert prices._has_completed_overseas_quotes(rows, bucket, QUOTE_DAY) is False


def test_mixed_currency_batch_is_fail_closed(monkeypatch):
    rows = [_row("VALID.SW", "CHF"), _row("OTHER.SW", "USD", id=2)]
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: rows)
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("Batch needs review"))
    result = prices._refresh_scheduled_overseas_quotes("ch", QUOTE_DAY)
    assert result["found"] == 2 and result["updated"] == 0 and result["status"] == "needs_review"
    assert len(result["manual_review_items"]) == 1
    with pytest.raises(ValueError, match="currency_mismatch"):
        prices._upsert_scheduled_overseas_quotes(rows, "ch", QUOTE_DAY,
            {row["symbol"]: [(QUOTE_DAY, 101.0)] for row in rows})


@pytest.mark.parametrize("changes,success", [({}, True), ({"covered": 1}, False), ({"covered": None}, False),
    ({"covered": "2"}, False), ({"covered": True}, False), ({"found": True}, False),
    ({"found": 0, "covered": 0}, False), ({"updated": 2}, False), ({"updated": False}, False),
    ({"unchanged": "true"}, False), ({"as_of": None}, False), ({"status": "needs_review"}, False),
    ({"errors": ["currency_mismatch"]}, False), ({"error": "source_failure"}, False),
    ({"manual_review_items": [{"reason": "currency_mismatch"}]}, False),
    ({"skipped_estimates": [{"symbol": "TEST"}]}, False)])
def test_only_non_us_poll_accepts_complete_unchanged_coverage(monkeypatch, tmp_path, changes, success):
    module = _load_scheduler(monkeypatch)
    response = {"status": "ok", "as_of": QUOTE_DAY.isoformat(), "found": 2, "updated": 0,
                "covered": 2, "unchanged": True, **changes}
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "_call", lambda *args: response)
    issues = module.result_issues(response)
    if not changes:
        assert issues == ["partial_update"]
    result = module._refresh_overseas_market_quotes("hk", NOW)
    assert (result["status"] == "success") is success
    if success:
        assert result["updated"] == 0 and result["covered"] == 2 and result["unchanged"] is True
        assert result["last_success_date"] == QUOTE_DAY.isoformat()
    else:
        assert not result.get("last_success_date")
        if response.get("errors") or response.get("error") or response.get("manual_review_items"):
            assert "partial_update" in result["issues"]
    us = module.refresh_us_quotes(NOW)
    if not changes:
        assert us["status"] == "blocked" and us["issues"] == ["partial_update"]


def test_unchanged_journal_marker_does_not_leak_into_next_written_refresh(monkeypatch, tmp_path):
    module = _load_scheduler(monkeypatch)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    response = {"status": "ok", "as_of": QUOTE_DAY.isoformat(), "found": 2, "updated": 0,
                "covered": 2, "unchanged": True}
    monkeypatch.setattr(module, "_call", lambda *args: response)
    assert module._refresh_overseas_market_quotes("hk", NOW)["unchanged"] is True
    response.update(updated=2, unchanged=False)
    result = module._refresh_overseas_market_quotes("hk", NOW + timedelta(minutes=5))
    assert result["status"] == "success" and result["updated"] == result["covered"] == 2 and result["unchanged"] is False


def test_pg16_numeric_hk_listing_precedes_usd_fallback_in_both_sql_paths(overseas_pg):
    conn = overseas_pg
    symbols = ["7", "70", "700", "0700", "12345"]
    with conn.cursor() as cur:
        cur.execute(f"""SELECT symbol, {prices._overseas_market_bucket_case()} AS bucket
                        FROM unnest(%s::text[], %s::text[]) AS s(symbol, currency)""",
                    (symbols, ["USD"] * len(symbols)))
        assert dict(cur.fetchall()) == {symbol: "hk" for symbol in symbols}
    _insert(conn, "pams_holdings_snapshot", [_position(symbol, "USD", 7, 100) for symbol in symbols])
    conn.commit()
    baseline = _project(conn)
    physical = _holdings_bytes(conn)
    for symbol in symbols:
        _write_quote(conn, symbol, market="US", currency="USD")
    assert _project(conn) == baseline and _holdings_bytes(conn) == physical
    hk_symbols = {row["symbol"] for row in prices._get_holdings_with_symbol(prices._OVERSEAS_MAJOR_CATEGORY, market_buckets=("hk",))}
    us_symbols = {row["symbol"] for row in prices._get_holdings_with_symbol(prices._OVERSEAS_MAJOR_CATEGORY, market_buckets=("us",))}
    assert set(symbols) <= hk_symbols and not (set(symbols) & us_symbols)


@pytest.mark.parametrize("bucket,symbol", [("ch", "USD_TEST.SW"), ("hk", "USD_TEST.HK"), ("hk", "700")])
def test_pg16_currency_mismatch_cannot_write_or_pass_existing_bad_quote(overseas_pg, monkeypatch, bucket, symbol):
    conn = overseas_pg
    _insert(conn, "pams_holdings_snapshot", [_position(symbol, "USD", 7, 100)])
    conn.commit()
    _write_quote(conn, symbol, market=bucket.upper(), currency="USD", source="yahoo_close", return_status="confirmed")
    physical, quotes = _holdings_bytes(conn), _quote_rows(conn)
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", lambda *args: pytest.fail("No currency conversion"))
    result = prices._refresh_scheduled_overseas_quotes(bucket, QUOTE_DAY)
    assert result["status"] == "needs_review" and result["reason"] == "currency_mismatch"
    assert result["updated"] == 0 and result.get("unchanged") is not True
    with pytest.raises(ValueError, match="currency_mismatch"):
        prices._upsert_scheduled_overseas_quotes([_row(symbol, "USD")], bucket, QUOTE_DAY,
                                                {symbol: [(QUOTE_DAY, 101.0)]})
    assert _holdings_bytes(conn) == physical and _quote_rows(conn) == quotes
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pams_ohlcv_daily")
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize("previous", [None, 0, -1, float("nan"), float("inf"), float("-inf"), 0.00001])
def test_missing_or_invalid_previous_close_keeps_refresh_incomplete(monkeypatch, previous):
    db = NumericDB()
    db.closes[("SIVE", BASE_DAY - timedelta(days=1))] = Decimal(99)
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "_get_holdings_with_symbol", lambda *args, **kwargs: [_row("SIVE", "SEK")])
    def fetch(symbols):
        history = [(QUOTE_DAY, 101.0)]
        if previous is not None:
            history.append((BASE_DAY, previous))
        prices._YFINANCE_CLOSE_CACHE.update({symbol: history for symbol in symbols})
        return {symbol: 101.0 for symbol in symbols}
    monkeypatch.setattr(prices, "_fetch_us_stock_prices_yf", fetch)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda *args: pytest.fail("Missing yesterday cannot confirm today's return"))
    result = prices._refresh_scheduled_overseas_quotes("se", QUOTE_DAY)
    assert result["updated"] == result["covered"] == result["daily_quote_rows"] == 0
    assert result.get("unchanged") is not True and "previous-session" in result["errors"][0]


def test_valid_persisted_previous_close_can_complete_target_only_history(monkeypatch):
    db, saved = NumericDB(), []
    db.closes[("SIVE", BASE_DAY)] = Decimal("704.4000")
    monkeypatch.setattr(prices, "_get_conn", lambda: db)
    monkeypatch.setattr(prices, "upsert_quote_snapshot", lambda cur, quote: saved.append(quote))
    result = prices._upsert_scheduled_overseas_quotes([_row("SIVE", "SEK")], "se", QUOTE_DAY,
                                                    {"SIVE": [(QUOTE_DAY, 712.2000122070312)]})
    assert result == (1, 1, {"SIVE"}) and db.writes == 1
    assert saved[0]["price"] == Decimal("712.2000") and db.closes[("SIVE", BASE_DAY)] == Decimal("704.4000")


@pytest.mark.parametrize("previous", [None, Decimal(0), Decimal(-1), Decimal("NaN")])
def test_pg16_completion_gate_cannot_use_missing_or_invalid_previous_sample(overseas_pg, previous):
    conn = overseas_pg
    samples = [{"symbol": "SIVE", "market": "YF", "trade_date": QUOTE_DAY, "close": Decimal("999999.99")}]
    if previous is not None:
        samples.append({"symbol": "SIVE", "market": "YF", "trade_date": BASE_DAY, "close": previous})
    _insert(conn, "pams_ohlcv_daily", samples)
    conn.commit()
    _write_quote(conn, "SIVE", market="SE", currency="SEK", source="yahoo_close", provider="yahoo", return_status="confirmed")
    assert prices._has_completed_overseas_quotes([_row("SIVE", "SEK")], "se", QUOTE_DAY) is False


def test_pg16_missing_previous_does_not_confirm_but_persisted_previous_is_usable(overseas_pg):
    conn = overseas_pg
    row = _row("SIVE", "SEK")
    history = {"SIVE": [(QUOTE_DAY, 712.2000122070312)]}
    assert prices._upsert_scheduled_overseas_quotes([row], "se", QUOTE_DAY, history) == (0, 0, set())
    assert _quote_rows(conn) == []
    _insert(conn, "pams_ohlcv_daily", [{"symbol": "SIVE", "market": "YF", "trade_date": BASE_DAY, "close": Decimal("704.4")}])
    conn.commit()
    assert prices._upsert_scheduled_overseas_quotes([row], "se", QUOTE_DAY, history) == (1, 1, {"SIVE"})
    assert _quote_rows(conn)[0]["price"] == Decimal("712.2000")
    assert prices._has_completed_overseas_quotes([row], "se", QUOTE_DAY) is True
