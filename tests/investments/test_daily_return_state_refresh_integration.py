from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints import prices  # noqa: E402
from domain.market import fund_nav_estimates  # noqa: E402
from domain.investments.daily_return_state import (  # noqa: E402
    RETURN_STATUS_CONFIRMED,
    RETURN_STATUS_ESTIMATED,
)


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> dict[str, bool]:
        return {"ok": True}

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeConn:
    def __init__(self, cur: FakeCursor) -> None:
        self.cur = cur
        self.commits = 0

    def cursor(self, *_args: object, **_kwargs: object) -> FakeCursor:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_fund_nav_estimate_is_mirrored_to_daily_quote_state() -> None:
    cur = FakeCursor()

    fund_nav_estimates.upsert_asset_market_daily_nav(
        cur,
        holding={
            "asset_id": "FUND_001412",
            "symbol": "001412",
            "name": "\u534e\u590f\u79d1\u521b50ETF\u8054\u63a5C",
        },
        as_of=date(2026, 6, 25),
        unit_nav=Decimal("1.4889"),
        estimated_unit_nav=Decimal("1.5012"),
        source="tiantian_fundgz_estimate",
        raw={"gz": "1.5012"},
    )

    assert len(cur.calls) == 2
    market_daily_sql, _market_daily_params = cur.calls[0]
    quote_sql, quote_params = cur.calls[1]

    assert "market_observations" in market_daily_sql and "'daily'" in market_daily_sql
    assert "market_observations" in quote_sql and "'quote'" in quote_sql
    assert quote_params is not None
    assert quote_params["source"] == "tiantian_fundgz_estimate"
    assert quote_params["return_status"] == RETURN_STATUS_ESTIMATED
    assert quote_params["quote_session"] == "nav"
    assert quote_params["price"] == Decimal("1.5012")


def test_cn_a_refresh_is_mirrored_to_daily_quote_state(monkeypatch) -> None:
    cur = FakeCursor()
    conn = FakeConn(cur)
    monkeypatch.setattr(prices, "_get_conn", lambda: conn)

    written = prices._upsert_daily_return_quote_snapshots(
        [
            {
                "asset_id": "SSE_601899",
                "symbol": "601899",
                "name": "\u7d2b\u91d1\u77ff\u4e1a",
                "currency": "CNY",
            }
        ],
        {"601899": 30.44},
        date(2026, 6, 25),
        source="tencent_a_stock_close",
        market="CN_A",
    )

    assert written == 1
    assert conn.commits == 1
    assert len(cur.calls) == 1
    quote_sql, quote_params = cur.calls[0]

    assert "market_observations" in quote_sql and "'quote'" in quote_sql
    assert quote_params is not None
    assert quote_params["source"] == "tencent_a_stock_close"
    assert quote_params["source_key"] == "tencent_a_stock_close:sh601899:2026-06-25"
    assert quote_params["return_status"] == RETURN_STATUS_CONFIRMED
    assert quote_params["quote_session"] == "close"
    assert quote_params["price"] == Decimal("30.44")
