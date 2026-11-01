from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.market import price_history  # noqa: E402


def test_normalize_price_instruments_for_analysis_and_trade_review():
    a_stock = price_history.normalize_price_instrument("601899")
    assert a_stock.market == "CN_A"
    assert a_stock.symbol == "sh601899"
    assert a_stock.role == "instrument"

    benchmark = price_history.normalize_price_instrument("000300.SH", benchmark=True)
    assert benchmark.market == "CN_INDEX"
    assert benchmark.symbol == "sh000300"
    assert benchmark.query_symbol == "000300.SH"
    assert benchmark.label == "CSI 300"
    assert benchmark.role == "benchmark"

    benchmark_short = price_history.normalize_price_instrument("000905", benchmark=True)
    assert benchmark_short.market == "CN_INDEX"
    assert benchmark_short.symbol == "sh000905"
    assert benchmark_short.query_symbol == "000905.SH"

    gold_etf = price_history.normalize_price_instrument("518880.SH")
    assert (gold_etf.market, gold_etf.symbol) == ("CN_A", "sh518880")
    benchmark_etf = price_history.normalize_price_instrument("518880.SH", benchmark=True)
    assert (benchmark_etf.market, benchmark_etf.symbol) == ("CN_A", "sh518880")
    explicit_etf = price_history.normalize_price_instrument("159919.SZ", market="CN_ETF")
    assert (explicit_etf.market, explicit_etf.symbol) == ("CN_A", "sz159919")
    sh_index = price_history.normalize_price_instrument("000001.SH")
    assert (sh_index.market, sh_index.symbol) == ("CN_INDEX", "sh000001")
    explicit_yf = price_history.normalize_price_instrument("518880.SH", market="YF")
    assert (explicit_yf.market, explicit_yf.symbol) == ("YF", "518880.SH")

    us_stock = price_history.normalize_price_instrument("NVDA")
    assert us_stock.market == "YF"
    assert us_stock.symbol == "NVDA"

    hk_stock = price_history.normalize_price_instrument("3690", market="HK")
    assert hk_stock.market == "YF"
    assert hk_stock.symbol == "3690.HK"

    kr_stock = price_history.normalize_price_instrument("005930", market="KR")
    assert kr_stock.market == "YF"
    assert kr_stock.symbol == "005930.KS"

    se_stock = price_history.normalize_price_instrument("SIVE", market="SE_STOCK")
    assert se_stock.market == "YF"
    assert se_stock.symbol == "SIVE.ST"


def test_rows_to_price_curve_normalizes_close_and_return():
    rows = [
        {"trade_date": date(2026, 1, 2), "close": Decimal("10")},
        {"trade_date": date(2026, 1, 3), "close": Decimal("11")},
    ]

    curve = price_history.rows_to_price_curve(rows)

    assert [item["date"] for item in curve] == ["2026-01-02", "2026-01-03"]
    assert Decimal(curve[0]["normalized"]) == Decimal("100")
    assert Decimal(curve[1]["normalized"]) == Decimal("110")
    assert Decimal(curve[1]["return_pct"]) == Decimal("10")


class FakeCursor:
    def __init__(self):
        self.executed_payload = None

    def executemany(self, _sql, payload):
        self.executed_payload = payload


class EmptyHistoryCursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))

    def fetchall(self):
        return []


def test_missing_instrument_history_remains_missing():
    cur = EmptyHistoryCursor()
    instrument = price_history.normalize_price_instrument('SYNTH', market='YF')
    payload = price_history.build_price_series_payload(
        cur, instruments=[instrument], start=date(2025, 1, 1), end=date(2025, 1, 2))
    assert payload['series'][0]['coverage'] == 'missing'
    assert payload['series'][0]['rows'] == []
    assert len(cur.queries) == 1


def test_missing_index_history_checks_both_sources():
    cur = EmptyHistoryCursor()
    instrument = price_history.normalize_price_instrument('000001.SH', benchmark=True)
    payload = price_history.build_price_series_payload(
        cur, instruments=[instrument], start=date(2025, 1, 1), end=date(2025, 1, 2))
    assert payload['series'][0]['coverage'] == 'missing'
    assert payload['series'][0]['rows'] == []
    assert len(cur.queries) == 2


def test_ambiguous_etf_market_is_not_inferred_as_cn_stock():
    instrument = price_history.normalize_price_instrument('SYNTH', market='ETF')
    assert (instrument.market, instrument.symbol) == ('ETF', 'SYNTH')


def test_malformed_suffixed_symbol_is_not_inferred_as_cn_stock():
    instrument = price_history.normalize_price_instrument('123456extra.SH')
    assert instrument.market == 'YF'


def test_index_history_fills_missing_dates_without_overwriting_pit():
    class Cursor:
        def __init__(self):
            self.rows = iter([
                [{"trade_date": date(2026, 6, 1), "close": Decimal("100")}],
                [{"trade_date": date(2026, 6, 1), "close": Decimal("99")},
                 {"trade_date": date(2026, 6, 2), "close": Decimal("101")}],
            ])

        def execute(self, _query, _params):
            pass

        def fetchall(self):
            return next(self.rows)

    instrument = price_history.normalize_price_instrument("000001.SH", benchmark=True)
    rows = price_history._query_ohlcv_rows(Cursor(), instrument, date(2026, 6, 1), date(2026, 6, 2))
    assert [(row["trade_date"], row["close"]) for row in rows] == [
        (date(2026, 6, 1), Decimal("100")),
        (date(2026, 6, 2), Decimal("101")),
    ]


def test_index_history_retains_single_available_source():
    class Cursor:
        def __init__(self, values):
            self.values = iter(values)

        def execute(self, _query, _params):
            pass

        def fetchall(self):
            return next(self.values)

    instrument = price_history.normalize_price_instrument("000001.SH", benchmark=True)
    row = {"trade_date": date(2026, 6, 1), "close": Decimal("100")}
    for inputs in ([[], [row]], [[row], []]):
        assert price_history._query_ohlcv_rows(
            Cursor(inputs), instrument, date(2026, 6, 1), date(2026, 6, 2)
        ) == [row]


def test_upsert_yfinance_history_persists_to_ohlcv(monkeypatch):
    def fake_fetch(symbols, *, start, end):
        assert symbols == ["NVDA"]
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 3)
        return {"NVDA": {date(2026, 1, 2): Decimal("100.5")}}, []

    cur = FakeCursor()
    monkeypatch.setattr(price_history, "fetch_yfinance_history", fake_fetch)

    result = price_history.upsert_yfinance_history(
        cur,
        symbols=["NVDA"],
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
    )

    assert result["rows"] == 1
    assert cur.executed_payload == [("NVDA", "YF", date(2026, 1, 2), Decimal("100.5"))]
