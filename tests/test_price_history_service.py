from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services import price_history  # noqa: E402


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
