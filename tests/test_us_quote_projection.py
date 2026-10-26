"""PG16 quote-only regression; requires the explicitly allowlisted test DSN.

Only session-local TEMP tables are created. Reader checks execute each real
service's CTE through ``h``; later DTO enrichment and HTTP are outside scope.
"""
from __future__ import annotations

import os
import socket
import sys
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import Json, RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/api"))

from endpoints import prices
from services import holdings_view, portfolio_state
from services.daily_return_state import (
    quote_snapshot_from_market_daily,
    upsert_quote_snapshot,
)
from services.quote_valuation import quote_valuation_cte

BASE_DAY = date(2026, 10, 23)
QUOTE_DAY = date(2026, 10, 26)
FUTURE_DAY = date(2026, 10, 27)
STAMP = datetime(2026, 10, 23, 22, tzinfo=UTC)
ACCOUNT = "projection_test_broker"
ASSET = "\u8d44\u4ea7"
OVERSEAS = "\u7f8e\u80a1"
US_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "QQQ", "VOO")
CASH_SYMBOLS = {f"CASH_{ccy}" for ccy in ("USD", "HKD", "KRW", "SEK", "CNY")}
MARKS = {symbol: 120.25 + i for i, symbol in enumerate(US_SYMBOLS)}
H_COLUMNS = (
    "id", "symbol", "name", "asset_liability", "currency", "account_code",
    "major_category", "minor_category", "market_value", "cost", "holding_pnl",
    "as_of", "created_at", "updated_at", "quantity", "buy_price", "current_price", "asset_id",
)


def _insert(conn, table, rows):
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, row)),
                    sql.SQL(",").join(sql.Placeholder() for _ in row),
                ),
                [Json(value) if isinstance(value, dict) else value for value in row.values()],
            )


def _position(symbol, currency, quantity, price, *, day=BASE_DAY, **changes):
    quantity, price = Decimal(str(quantity)), Decimal(str(price))
    cash = symbol.startswith("CASH_")
    cost = quantity if cash else quantity * (price - Decimal("3.125"))
    return {
        "symbol": symbol, "name": symbol, "asset_liability": ASSET,
        "currency": currency, "account_code": ACCOUNT,
        "major_category": "\u73b0\u91d1" if cash else OVERSEAS,
        "minor_category": "cash" if cash else "equity",
        "market_value": quantity * price, "cost": cost, "as_of": day,
        "created_at": STAMP, "updated_at": STAMP, "quantity": quantity,
        "buy_price": Decimal(1) if cash else price - Decimal("3.125"),
        "current_price": price, "asset_id": f"TEST_{symbol}", **changes,
    }


@pytest.fixture
def pg_projection(monkeypatch):
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PAMS_TEST_POSTGRES_DSN must explicitly select isolated PG16 at 127.0.0.1:55483/pams")
    parsed = parse_dsn(dsn)
    assert {key: parsed.get(key) for key in ("host", "port", "dbname", "user")} == {
        "host": "127.0.0.1", "port": "55483", "dbname": "pams", "user": "pams",
    }, "Refusing all non-allowlisted databases, including production"
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        assert conn.server_version // 10000 == 16
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp")
            cur.execute("SET statement_timeout TO '10s'")
            cur.execute("SET lock_timeout TO '3s'")
            cur.execute("""
                CREATE TEMP TABLE pams_holdings_snapshot (
                    id BIGSERIAL PRIMARY KEY, symbol TEXT, name TEXT NOT NULL,
                    asset_liability TEXT, currency VARCHAR(10), account_code TEXT,
                    major_category TEXT, minor_category TEXT,
                    market_value NUMERIC(28,10), cost NUMERIC(28,10),
                    holding_pnl NUMERIC(28,10) GENERATED ALWAYS AS (market_value - cost) STORED,
                    as_of DATE NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), quantity NUMERIC(28,10),
                    buy_price NUMERIC(28,10), current_price NUMERIC(28,10), asset_id VARCHAR(120),
                    UNIQUE (account_code, name, as_of)
                )
            """)
            cur.execute("""
                CREATE TEMP TABLE pams_market_quote_snapshot (
                    quote_id BIGSERIAL PRIMARY KEY, quote_date DATE NOT NULL,
                    asset_id VARCHAR(120), symbol TEXT, name TEXT, market TEXT,
                    currency VARCHAR(10) NOT NULL DEFAULT 'CNY', provider TEXT NOT NULL,
                    source TEXT NOT NULL, source_key TEXT NOT NULL,
                    quote_session VARCHAR(40) NOT NULL DEFAULT 'close',
                    return_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    price NUMERIC(28,10), price_time TIMESTAMPTZ,
                    base_currency VARCHAR(10) NOT NULL DEFAULT 'CNY', fx_rate NUMERIC(28,10),
                    source_hash VARCHAR(64), raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CHECK (quote_session IN ('open','close','premarket','intraday','afterhours','overnight','nav','unknown')),
                    CHECK (return_status IN ('pending','estimated','confirmed')),
                    UNIQUE (quote_date, provider, source_key, quote_session)
                )
            """)
            cur.execute("""
                CREATE TEMP TABLE pams_ohlcv_daily (
                    symbol TEXT, market TEXT, trade_date DATE, close NUMERIC(28,10),
                    UNIQUE (symbol, market, trade_date)
                )
            """)
            cur.execute("""
                SELECT count(*) FROM pg_class
                WHERE oid IN ('pams_holdings_snapshot'::regclass,
                              'pams_market_quote_snapshot'::regclass,
                              'pams_ohlcv_daily'::regclass) AND relpersistence = 't'
            """)
            assert cur.fetchone()[0] == 3
        baseline = [_position(symbol, "USD", Decimal(i + 1) + Decimal("0.125"), 100 + i)
                    for i, symbol in enumerate(US_SYMBOLS)]
        baseline += [_position("0700.HK", "HKD", 23, 300),
                     _position("005930.KS", "KRW", 37, 60000),
                     _position("VOLV-B.ST", "SEK", 17, 250)]
        baseline += [_position(f"CASH_{ccy}", ccy, amount, 1)
                     for ccy, amount in (("USD", "-125.75"), ("HKD", "1024.36"),
                                         ("KRW", "300001.25"), ("SEK", "720.13"), ("CNY", "150.01"))]
        _insert(conn, "pams_holdings_snapshot", baseline)
        _insert(conn, "pams_holdings_snapshot", [
            _position("005930.KS", "KRW", 999, 61000, day=QUOTE_DAY, cost=Decimal("777777.13")),
        ])
        conn.commit()

        def deny_network(*args, **kwargs):
            raise AssertionError("External network and additional DB connections are forbidden")

        monkeypatch.setattr(socket.socket, "connect", deny_network)
        monkeypatch.setattr(socket, "create_connection", deny_network)
        monkeypatch.setattr(urllib.request, "urlopen", deny_network)
        monkeypatch.setattr(psycopg2, "connect", deny_network)
        monkeypatch.setattr(prices, "_get_conn", lambda: conn)
        monkeypatch.setattr(prices, "_price_refresh_as_of", lambda *args: QUOTE_DAY)
        monkeypatch.setattr(prices, "_YFINANCE_CLOSE_CACHE", {})
        monkeypatch.setattr(prices, "_US_ESTIMATE_QUOTE_CACHE", {})
        yield conn
    finally:
        conn.rollback()
        conn.close()  # TEMP tables (including committed writer rows) disappear here.


def _holdings_bytes(conn):
    output = BytesIO()
    with conn.cursor() as cur:
        cur.copy_expert("COPY (SELECT * FROM pams_holdings_snapshot ORDER BY id) TO STDOUT WITH BINARY", output)
    return output.getvalue()


def _quote_rows(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM pams_market_quote_snapshot ORDER BY quote_id")
        return [dict(row) for row in cur.fetchall()]


def _business_rows(rows):
    # Real ON CONFLICT updates only this timestamp even for identical evidence.
    return [{key: value for key, value in row.items() if key != "updated_at"} for row in rows]


def _project(conn, enabled=True, as_of=QUOTE_DAY):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"""
            WITH {holdings_view.COMPLETE_ACCOUNT_SNAPSHOT_CTE}, source_holdings AS (
                SELECT s.* FROM pams_holdings_snapshot s JOIN latest_account la
                  ON la.account_code = s.account_code AND la.account_as_of = s.as_of
            ), {quote_valuation_cte(enabled)}
            SELECT h.* FROM h ORDER BY id
        """, (as_of,))
        assert tuple(column.name for column in cur.description) == H_COLUMNS
        return {row["symbol"]: dict(row) for row in cur.fetchall()}


def _refresh(conn):
    def fake_market_network(symbols):
        assert set(symbols) == set(US_SYMBOLS)
        prices._YFINANCE_CLOSE_CACHE.update({
            symbol: [(BASE_DAY, 100 + i), (QUOTE_DAY, MARKS[symbol])]
            for i, symbol in enumerate(US_SYMBOLS)
        })
        return dict(MARKS)

    result = prices._refresh_category(
        OVERSEAS, fake_market_network, market_buckets=("us",),
        price_basis="close", require_current_close=True,
    )
    assert result["found"] == result["updated"] == result["daily_quote_rows"] == 8
    assert result["errors"] == []
    assert result["valuation_projection_only"] is True
    return result


class _CapturedProjection(Exception):
    pass


def _reader_projection(conn, monkeypatch, reader):
    """Execute real reader membership/overlay SQL, not downstream DTO joins."""
    result = {}

    class ReaderCursor:
        def __init__(self, cur):
            self.cur = cur

        def execute(self, query, params=None):
            overlay = quote_valuation_cte(True)
            if "source_holdings AS (" in query and overlay in query:
                prefix = query.split(overlay, 1)[0] + overlay
                self.cur.execute(prefix + " SELECT h.* FROM h ORDER BY id", tuple(params[:prefix.count("%s")]))
                assert tuple(column.name for column in self.cur.description) == H_COLUMNS
                result.update({row["symbol"]: dict(row) for row in self.cur.fetchall()})
                raise _CapturedProjection
            return self.cur.execute(query, params)

        def fetchone(self):
            return self.cur.fetchone()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        spy = ReaderCursor(cur)
        with pytest.raises(_CapturedProjection):
            if reader == "holdings":
                holdings_view.build_holdings_latest_payload(
                    spy, limit=500, include_closed=False, view_as_of=QUOTE_DAY,
                    excluded_accounts=set(), owner_ratios={}, default_owner_ratio=Decimal(1),
                )
            else:
                monkeypatch.setattr(portfolio_state, "_table_exists", lambda *args: True)
                monkeypatch.setattr(portfolio_state, "_latest_fx_rates", lambda *args: {})
                monkeypatch.setattr(portfolio_state, "_view_as_of", lambda: QUOTE_DAY)
                portfolio_state.load_investment_positions(spy, "CNY", {})
    return result


def _assert_marked(baseline, marked):
    assert len(baseline) == len(marked) == 16
    assert set(marked) == set(baseline)
    changed = set()
    for symbol, original in baseline.items():
        row = marked[symbol]
        for field in H_COLUMNS:
            if field not in {"current_price", "market_value", "holding_pnl", "as_of", "updated_at"}:
                assert row[field] == original[field], (symbol, field, row[field], original[field])
        if symbol in US_SYMBOLS:
            assert row["current_price"] == Decimal(str(MARKS[symbol]))
            assert row["market_value"] == (original["quantity"] * Decimal(str(MARKS[symbol]))).quantize(Decimal("0.01"))
            assert row["holding_pnl"] == row["market_value"] - original["cost"]
            assert all(row[field] != original[field] for field in ("current_price", "market_value", "holding_pnl"))
            changed.add(symbol)
        else:
            for field in ("current_price", "market_value", "holding_pnl"):
                assert row[field] == original[field], (symbol, field)
        assert row["as_of"] == QUOTE_DAY
    assert changed == set(US_SYMBOLS)
    assert len(CASH_SYMBOLS & marked.keys()) == 5
    assert marked["005930.KS"]["quantity"] == Decimal(37)
    assert marked["005930.KS"]["cost"] == baseline["005930.KS"]["cost"] != Decimal("777777.13")


def test_pg16_old_eight_holding_writes_flip_partial_into_complete(pg_projection, monkeypatch):
    conn = pg_projection
    physical = _holdings_bytes(conn)
    baseline = _project(conn, enabled=False)
    assert len(baseline) == 16
    assert {row["as_of"] for row in baseline.values()} == {BASE_DAY}

    class SavepointConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self, *args, **kwargs):
            return conn.cursor(*args, **kwargs)

        def commit(self):
            pass  # Legacy writer is deliberately contained in this savepoint.

    rows = prices._get_holdings_with_symbol(OVERSEAS, market_buckets=("us",))
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT legacy_eight_quotes")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(prices, "_get_conn", lambda: SavepointConnection())
            assert prices._upsert_price_snapshots(rows, MARKS, QUOTE_DAY) == 8
        flipped = _project(conn, enabled=False)
        assert len(flipped) == 9
        assert set(flipped) == set(US_SYMBOLS) | {"005930.KS"}
        assert set(baseline) - set(flipped) == CASH_SYMBOLS | {"0700.HK", "VOLV-B.ST"}
        assert flipped["005930.KS"]["quantity"] == Decimal(999)
        assert flipped["005930.KS"]["cost"] == Decimal("777777.13")
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT legacy_eight_quotes")
            cur.execute("RELEASE SAVEPOINT legacy_eight_quotes")
    assert _holdings_bytes(conn) == physical
    assert _project(conn, enabled=False) == baseline


@pytest.mark.parametrize("reader", ["holdings", "portfolio"])
def test_pg16_scheduled_close_preserves_full_account_and_is_idempotent(pg_projection, monkeypatch, reader):
    conn = pg_projection
    physical = _holdings_bytes(conn)
    baseline = _project(conn)
    _refresh(conn)
    assert _holdings_bytes(conn) == physical
    quotes = _quote_rows(conn)
    assert len(quotes) == 8
    assert {row["symbol"] for row in quotes} == set(US_SYMBOLS)
    assert all(row["raw_json"]["valuation_projection_only"] is True for row in quotes)
    assert all(row["quote_date"] == QUOTE_DAY and row["currency"] == "USD"
               and row["return_status"] == "confirmed" for row in quotes)
    projected = _reader_projection(conn, monkeypatch, reader)
    _assert_marked(baseline, projected)
    assert _project(conn, enabled=False) == baseline
    _refresh(conn)
    assert _holdings_bytes(conn) == physical
    assert _business_rows(_quote_rows(conn)) == _business_rows(quotes)
    repeated = _reader_projection(conn, monkeypatch, reader)
    assert _business_rows(list(repeated.values())) == _business_rows(list(projected.values()))


@pytest.mark.parametrize("preexisting", [False, True])
def test_pg16_second_quote_db_failure_rolls_back_entire_batch(pg_projection, monkeypatch, preexisting):
    conn = pg_projection
    if preexisting:
        _refresh(conn)
    physical = _holdings_bytes(conn)
    quotes = _quote_rows(conn)
    projected = _project(conn)
    conn.commit()
    actual_upsert = prices.upsert_quote_snapshot
    calls = []

    def fail_second(cur, quote):
        calls.append(quote["symbol"])
        if len(calls) == 2:
            quote = {**quote, "return_status": "injected_invalid"}
        else:
            quote = {**quote, "price": Decimal("4444.44")}
        result = actual_upsert(cur, quote)
        assert result["price"] == Decimal("4444.44")
        return result

    monkeypatch.setattr(prices, "upsert_quote_snapshot", fail_second)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _refresh(conn)
    assert len(calls) == 2
    # No test-side rollback: the actual writer context must have done it.
    assert conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    assert _quote_rows(conn) == quotes
    assert _holdings_bytes(conn) == physical
    assert _project(conn) == projected


def _write_quote(conn, symbol="AAPL", **changes):
    payload = {
        "as_of": QUOTE_DAY, "symbol": symbol, "asset_id": f"TEST_{symbol}",
        "name": symbol, "market": "US", "currency": "USD", "provider": "test",
        "source": "databento_estimate", "source_key": f"test:{symbol}",
        "close_price": Decimal("999999.99"), "return_status": "estimated",
        "raw_json": {"valuation_projection_only": True}, **changes,
    }
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        upsert_quote_snapshot(cur, quote_snapshot_from_market_daily(payload))
    conn.commit()


@pytest.mark.parametrize("changes", [
    {"raw_json": {}},
    {"raw_json": {"valuation_projection_only": False}},
    {"raw_json": {"valuation_projection_only": "true"}},
    {"as_of": FUTURE_DAY},
    {"as_of": BASE_DAY},
    {"currency": "HKD"},
    {"return_status": "pending"},
    {"close_price": Decimal(0)},
    {"close_price": Decimal(-1)},
], ids=["unmarked", "false-mark", "string-mark", "future", "same-day", "wrong-currency", "pending", "zero", "negative"])
def test_pg16_ineligible_quotes_never_change_valuation(pg_projection, changes):
    conn = pg_projection
    physical = _holdings_bytes(conn)
    baseline = _project(conn)
    _write_quote(conn, **changes)
    assert len(_quote_rows(conn)) == 1
    assert _project(conn) == baseline
    assert _holdings_bytes(conn) == physical


@pytest.mark.parametrize("symbol,currency", [("CASH_USD", "USD"), ("005930.KS", "KRW")])
def test_pg16_cash_and_partial_non_us_member_ignore_marked_quotes(pg_projection, symbol, currency):
    conn = pg_projection
    physical = _holdings_bytes(conn)
    baseline = _project(conn)
    _write_quote(conn, symbol, currency=currency)
    projected = _project(conn)
    assert projected[symbol]["quantity"] == baseline[symbol]["quantity"]
    assert projected[symbol]["cost"] == baseline[symbol]["cost"]
    assert projected[symbol]["current_price"] == baseline[symbol]["current_price"]
    assert projected == baseline
    assert _holdings_bytes(conn) == physical


@pytest.mark.parametrize("reader", ["holdings", "portfolio"])
def test_pg16_official_same_day_complete_snapshot_beats_old_estimate(pg_projection, monkeypatch, reader):
    conn = pg_projection
    baseline = _project(conn)
    for symbol in US_SYMBOLS:
        _write_quote(conn, symbol)
    estimated = _project(conn)
    assert estimated["AAPL"]["current_price"] == Decimal("999999.99")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pams_holdings_snapshot (
                symbol, name, asset_liability, currency, account_code, major_category, minor_category,
                market_value, cost, as_of, quantity, buy_price, current_price, asset_id
            )
            SELECT symbol, name, asset_liability, currency, account_code, major_category, minor_category,
                   CASE WHEN currency = 'USD' AND major_category = %s
                        THEN (quantity + 2) * 111.25 ELSE market_value END,
                   CASE WHEN currency = 'USD' AND major_category = %s THEN cost + 13 ELSE cost END,
                   %s, CASE WHEN currency = 'USD' AND major_category = %s THEN quantity + 2 ELSE quantity END,
                   buy_price, CASE WHEN currency = 'USD' AND major_category = %s THEN 111.25 ELSE current_price END,
                   asset_id
            FROM pams_holdings_snapshot WHERE as_of = %s
            ON CONFLICT (account_code, name, as_of) DO UPDATE SET
                quantity = EXCLUDED.quantity, cost = EXCLUDED.cost,
                market_value = EXCLUDED.market_value, current_price = EXCLUDED.current_price
        """, (OVERSEAS, OVERSEAS, QUOTE_DAY, OVERSEAS, OVERSEAS, BASE_DAY))
    conn.commit()
    physical = _holdings_bytes(conn)
    official = _project(conn, enabled=False)
    assert len(official) == 16
    assert official["005930.KS"]["quantity"] == baseline["005930.KS"]["quantity"]
    assert official["AAPL"]["quantity"] == baseline["AAPL"]["quantity"] + 2
    assert official["AAPL"]["cost"] == baseline["AAPL"]["cost"] + 13
    assert official["AAPL"]["current_price"] == Decimal("111.25")
    assert _reader_projection(conn, monkeypatch, reader) == official
    # Even a later upsert/updated_at of the same-day old estimate cannot win.
    for symbol in US_SYMBOLS:
        _write_quote(conn, symbol)
    assert _reader_projection(conn, monkeypatch, reader) == official
    assert _holdings_bytes(conn) == physical
