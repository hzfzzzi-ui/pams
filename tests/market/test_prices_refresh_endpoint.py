from __future__ import annotations

import json
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from endpoints import prices  # noqa: E402
from domain.market.fund_nav_estimates import should_copy_confirmed_nav_to_current_snapshot  # noqa: E402


class DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_stale_confirmed_nav_is_not_copied_to_current_snapshot() -> None:
    assert should_copy_confirmed_nav_to_current_snapshot(date(2026, 6, 22), date(2026, 6, 22))
    assert not should_copy_confirmed_nav_to_current_snapshot(date(2026, 6, 18), date(2026, 6, 22))


def test_a_stock_same_day_rollforward_applies_buy_once():
    previous = {
        "quantity": Decimal("7300"),
        "cost": Decimal("244229.7675"),
    }
    trades = [
        {
            "trade_type": "buy",
            "side": "buy",
            "quantity": Decimal("1000"),
            "price": Decimal("30.43"),
            "gross_amount": Decimal("30430"),
            "fee_amount": Decimal("15.22"),
            "tax_amount": Decimal("0"),
        }
    ]

    expected = prices._roll_forward_a_stock_position(previous, trades)

    assert expected["quantity"] == Decimal("8300")
    assert expected["cost"] == Decimal("274674.9875")
    assert expected["buy_price"].quantize(Decimal("0.000001")) == Decimal("33.093372")
    assert prices._row_matches_rollforward(
        {"quantity": Decimal("8300"), "cost": Decimal("274674.9875")},
        expected,
    )


def test_a_stock_same_day_rollforward_reduces_sell_by_average_cost():
    previous = {
        "quantity": Decimal("8300"),
        "cost": Decimal("277685.9000"),
    }
    trades = [
        {
            "trade_type": "sell",
            "side": "sell",
            "quantity": Decimal("1000"),
            "price": Decimal("30.78"),
            "gross_amount": Decimal("30780"),
            "fee_amount": Decimal("30.78"),
            "tax_amount": Decimal("0"),
        }
    ]

    expected = prices._roll_forward_a_stock_position(previous, trades)

    assert expected["quantity"] == Decimal("7300")
    assert expected["cost"].quantize(Decimal("0.0001")) == Decimal("244229.7675")


def test_a_stock_same_day_rollforward_does_not_resurrect_closed_prior_lot(monkeypatch):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self, **kwargs):
            return FakeCursor()

    key = ("tonghuashun", "601138")
    same_day_buy = {
        "trade_type": "buy",
        "side": "buy",
        "quantity": Decimal("300"),
        "price": Decimal("76.47"),
        "gross_amount": Decimal("22941"),
        "fee_amount": Decimal("11.47"),
        "tax_amount": Decimal("0"),
    }

    monkeypatch.setattr(prices, "_get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        prices,
        "_previous_a_stock_snapshot_rows",
        lambda cur, keys, as_of: {
            key: {
                "quantity": Decimal("400"),
                "cost": Decimal("31209.99"),
                "current_price": Decimal("80.01"),
            }
        },
    )
    monkeypatch.setattr(prices, "_a_stock_trade_net_quantities_before", lambda cur, keys, as_of: {key: Decimal("0")})
    monkeypatch.setattr(prices, "_same_day_a_stock_trades", lambda cur, keys, as_of: {key: [same_day_buy]})

    adjusted = prices._apply_same_day_a_stock_trades(
        [
            {
                "id": 1,
                "account_code": "tonghuashun",
                "symbol": "601138",
                "name": "Industrial Foxconn",
                "major_category": prices._A_STOCK_MAJOR_CATEGORY,
                "minor_category": prices._A_STOCK_MINOR_CATEGORY,
                "quantity": Decimal("700"),
                "cost": Decimal("54162.46"),
                "current_price": Decimal("78.08"),
                "market_value": Decimal("54656"),
                "as_of": date(2026, 6, 18),
            }
        ],
        date(2026, 6, 18),
    )

    assert adjusted[0]["quantity"] == Decimal("300")
    assert adjusted[0]["cost"] == Decimal("22952.47")
    assert adjusted[0]["market_value"] == Decimal("23424.00")


def test_a_stock_refresh_uses_latest_account_snapshot_bucket():
    assert prices._use_latest_account_snapshot_bucket(
        prices._A_STOCK_MAJOR_CATEGORY,
        (prices._A_STOCK_FUND_MINOR_CATEGORY,),
    )
    assert not prices._use_latest_account_snapshot_bucket(
        prices._A_STOCK_MAJOR_CATEGORY,
        (),
    )
    assert not prices._use_latest_account_snapshot_bucket(
        "US",
        (prices._A_STOCK_FUND_MINOR_CATEGORY,),
    )
    assert prices._use_latest_account_snapshot_bucket(
        prices._OVERSEAS_MAJOR_CATEGORY,
        (),
    )


def test_refresh_investment_prices_runs_requested_public_sources(monkeypatch):
    calls: list[object] = []

    def fake_refresh_category(category, fetch_fn, exclude_minor_categories=(), **kwargs):
        calls.append(category)
        return {"category": category, "found": 2, "updated": 2, "errors": []}

    def fake_fund_refresh(conn, *, use_intraday_estimates=None):
        calls.append(("fund", use_intraday_estimates))
        return {"processed": 3, "inserted_estimate_snapshots": 3, "errors": []}

    monkeypatch.setattr(prices, "_refresh_category", fake_refresh_category)
    monkeypatch.setattr(prices, "_refresh_fund_nav_estimates", fake_fund_refresh)
    monkeypatch.setattr(prices, "_get_conn", lambda: DummyConn())
    monkeypatch.setattr(prices, "_latest_investment_snapshot_as_of", lambda: date(2026, 6, 2))

    response = prices.refresh_investment_prices(
        prices.InvestmentPriceRefreshRequest(scopes=["a_stock", "a_fund"])
    )
    data = _payload(response)

    assert calls == ["A股", ("fund", None)]
    assert data["status"] == "ok"
    assert data["as_of"] == "2026-06-02"
    assert data["total_found"] == 5
    assert data["total_updated"] == 5
    assert [r["scope"] for r in data["results"]] == ["a_stock", "a_fund"]


def test_refresh_investment_prices_defaults_to_all_four_public_scopes(monkeypatch):
    calls: list[str] = []

    def fake_refresh_category(category, fetch_fn, exclude_minor_categories=(), **kwargs):
        calls.append(category)
        return {"category": category, "found": 1, "updated": 1, "errors": []}

    monkeypatch.setattr(prices, "_refresh_category", fake_refresh_category)
    monkeypatch.setattr(
        prices,
        "_refresh_fund_nav_estimates",
        lambda conn, *, use_intraday_estimates=None: {"processed": 1, "inserted_estimate_snapshots": 1, "errors": []},
    )
    monkeypatch.setattr(prices, "_get_conn", lambda: DummyConn())
    monkeypatch.setattr(prices, "_latest_investment_snapshot_as_of", lambda: date(2026, 6, 2))

    data = _payload(prices.refresh_investment_prices())

    assert data["requested_scopes"] == ["a_stock", "us_stock", "a_fund", "precious_metal"]
    assert calls == ["A股", "美股", "贵金属"]


def test_refresh_investment_prices_can_force_fund_intraday_estimates(monkeypatch):
    calls: list[bool | None] = []

    def fake_fund_refresh(conn, *, use_intraday_estimates=None):
        calls.append(use_intraday_estimates)
        return {"processed": 3, "inserted_estimate_snapshots": 3, "errors": []}

    monkeypatch.setattr(prices, "_refresh_fund_nav_estimates", fake_fund_refresh)
    monkeypatch.setattr(prices, "_get_conn", lambda: DummyConn())
    monkeypatch.setattr(prices, "_latest_investment_snapshot_as_of", lambda: date(2026, 6, 2))

    response = prices.refresh_investment_prices(
        prices.InvestmentPriceRefreshRequest(
            scopes=["a_fund"],
            fund_use_intraday_estimate=True,
        )
    )
    data = _payload(response)

    assert calls == [True]
    assert data["status"] == "ok"
    assert data["total_updated"] == 3


def test_refresh_investment_prices_can_target_overseas_market_buckets(monkeypatch):
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def fake_refresh_category(category, fetch_fn, exclude_minor_categories=(), market_buckets=(), price_basis="close"):
        calls.append((category, tuple(market_buckets), price_basis))
        return {
            "category": category,
            "found": 1,
            "updated": 1,
            "market_buckets": list(market_buckets),
            "price_basis": price_basis,
            "errors": [],
        }

    monkeypatch.setattr(prices, "_refresh_category", fake_refresh_category)
    monkeypatch.setattr(prices, "_latest_investment_snapshot_as_of", lambda: date(2026, 6, 2))

    response = prices.refresh_investment_prices(
        prices.InvestmentPriceRefreshRequest(
            scopes=["kr", "hk", "usa"],
            overseas_price_basis="estimate",
        )
    )
    data = _payload(response)

    assert calls == [
        ("美股", ("kr",), "estimate"),
        ("美股", ("hk",), "estimate"),
        ("美股", ("us",), "estimate"),
    ]
    assert data["requested_scopes"] == ["kr_stock", "hk_stock", "us_only"]


def test_refresh_us_stock_endpoint_accepts_market_and_price_basis(monkeypatch):
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def fake_refresh_category(category, fetch_fn, exclude_minor_categories=(), market_buckets=(), price_basis="close"):
        calls.append((category, tuple(market_buckets), price_basis))
        return {
            "category": category,
            "found": 1,
            "updated": 1,
            "market_buckets": list(market_buckets),
            "price_basis": price_basis,
            "errors": [],
        }

    monkeypatch.setattr(prices, "_refresh_category", fake_refresh_category)

    data = _payload(prices.refresh_us_stock(market="hk", price_basis="estimate"))

    assert calls == [("美股", ("hk",), "estimate")]
    assert data["price_basis"] == "estimate"


def test_us_stock_estimate_prices_prefer_prepost_intraday_quote(monkeypatch):
    calls: list[dict] = []

    class FakeClose:
        def __init__(self, values=None, items=None):
            self._values = values or []
            self._items = items or []
            self.empty = not (self._values or self._items)

        def dropna(self):
            return self

        @property
        def iloc(self):
            return self

        def __getitem__(self, index):
            return self._values[index]

        def items(self):
            return iter(self._items)

    class FakeFrame:
        def __init__(self, close):
            self._close = close
            self.empty = close.empty

        def __contains__(self, key):
            return key == "Close"

        def __getitem__(self, key):
            if key != "Close":
                raise KeyError(key)
            return self._close

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.fast_info = {"last_price": 98.0}

        def history(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("interval") == "1m":
                return FakeFrame(FakeClose(values=[101.12, 102.34]))
            return FakeFrame(FakeClose(items=[(date(2026, 6, 12), 100.0)]))

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    result = prices._fetch_us_stock_prices_yf(["NVDA"], prefer_live_quote=True)

    assert result == {"NVDA": 102.34}
    assert calls[0]["interval"] == "1m"
    assert calls[0]["prepost"] is True


def test_us_stock_estimate_prices_prefer_yahoo_premarket_quote(monkeypatch):
    calls: list[dict] = []

    class FakeClose:
        empty = False

        def dropna(self):
            return self

        @property
        def iloc(self):
            return self

        def __getitem__(self, index):
            return 989.60

        def items(self):
            return iter([(date(2026, 6, 12), 981.61)])

    class FakeFrame:
        empty = False

        def __contains__(self, key):
            return key == "Close"

        def __getitem__(self, key):
            if key != "Close":
                raise KeyError(key)
            return FakeClose()

    class FakeTicker:
        info = {"marketState": "PRE", "preMarketPrice": 1048.0, "regularMarketPrice": 981.61}
        fast_info = {"last_price": 981.61}

        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            calls.append(kwargs)
            return FakeFrame()

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    result = prices._fetch_us_stock_prices_yf(["MU"], prefer_live_quote=True)

    assert result == {"MU": 1048.0}
    assert calls == [{"period": "7d", "auto_adjust": False}]


def test_us_stock_estimate_prices_prefer_databento_over_yahoo(monkeypatch):
    monkeypatch.setattr(
        prices,
        "_fetch_us_stock_prices_yf",
        lambda symbols, *, prefer_live_quote=False: {"MU": 1033.88, "NVDA": 207.55},
    )

    def fake_databento(symbols):
        prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
            "source": "databento_estimate",
            "provider": "databento",
            "session": "all_hours",
            "quote_type": "last",
            "confidence": "high",
            "price": 1047.0,
        }
        return {"MU": 1047.0}

    monkeypatch.setattr(prices, "_fetch_databento_us_estimate_prices", fake_databento)

    result = prices._fetch_us_stock_estimate_prices(["MU", "NVDA"])

    assert result == {"MU": 1047.0, "NVDA": 207.55}
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["source"] == "databento_estimate"
    assert prices._US_ESTIMATE_QUOTE_CACHE["NVDA"]["source"] == "yahoo_estimate"


def test_us_stock_estimate_prices_prefer_databento_over_ibkr_fallback(monkeypatch):
    monkeypatch.setattr(
        prices,
        "_fetch_us_stock_prices_yf",
        lambda symbols, *, prefer_live_quote=False: {"MU": 1033.88, "NVDA": 207.55},
    )
    monkeypatch.setenv("PAMS_IBKR_ESTIMATE_FALLBACK_ENABLED", "true")

    def fake_tws(symbols):
        prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
            "source": "ibkr_tws_estimate",
            "provider": "ibkr_tws",
            "session": "overnight",
            "quote_type": "last",
            "confidence": "high",
            "price": 1047.0,
        }
        return {"MU": 1047.0}

    def fake_client_portal(symbols):
        prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
            "source": "ibkr_client_portal_estimate",
            "provider": "ibkr_client_portal",
            "session": "extended_or_overnight",
            "quote_type": "last",
            "confidence": "high",
            "price": 1048.0,
        }
        return {"MU": 1048.0}

    def fake_databento(symbols):
        prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
            "source": "databento_estimate",
            "provider": "databento",
            "session": "all_hours",
            "quote_type": "DBEQ.BASIC:trades:price",
            "confidence": "high",
            "price": 1050.0,
        }
        return {"MU": 1050.0}

    monkeypatch.setattr(prices, "_fetch_ibkr_tws_estimate_prices", fake_tws)
    monkeypatch.setattr(prices, "_fetch_ibkr_client_portal_estimate_prices", fake_client_portal)
    monkeypatch.setattr(prices, "_fetch_databento_us_estimate_prices", fake_databento)

    result = prices._fetch_us_stock_estimate_prices(["MU", "NVDA"])

    assert result == {"MU": 1050.0, "NVDA": 207.55}
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["source"] == "databento_estimate"
    assert prices._US_ESTIMATE_QUOTE_CACHE["NVDA"]["source"] == "yahoo_estimate"


def test_us_stock_estimate_prices_uses_tws_when_enabled_and_databento_unavailable(monkeypatch):
    monkeypatch.setattr(
        prices,
        "_fetch_us_stock_prices_yf",
        lambda symbols, *, prefer_live_quote=False: {"MU": 1033.88, "NVDA": 207.55},
    )
    monkeypatch.setenv("PAMS_IBKR_ESTIMATE_FALLBACK_ENABLED", "true")

    def fake_tws(symbols):
        prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
            "source": "ibkr_tws_estimate",
            "provider": "ibkr_tws",
            "session": "overnight",
            "quote_type": "last",
            "confidence": "high",
            "price": 1047.0,
        }
        return {"MU": 1047.0}

    monkeypatch.setattr(prices, "_fetch_ibkr_tws_estimate_prices", fake_tws)
    monkeypatch.setattr(prices, "_fetch_ibkr_client_portal_estimate_prices", lambda symbols: {})
    monkeypatch.setattr(prices, "_fetch_databento_us_estimate_prices", lambda symbols: {})

    result = prices._fetch_us_stock_estimate_prices(["MU", "NVDA"])

    assert result == {"MU": 1047.0, "NVDA": 207.55}
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["source"] == "ibkr_tws_estimate"
    assert prices._US_ESTIMATE_QUOTE_CACHE["NVDA"]["source"] == "yahoo_estimate"


def test_fetch_databento_us_estimate_prices_records_quote_source(monkeypatch):
    import pandas as pd

    captured = {}

    class FakeResponse:
        def to_df(self, price_type="fixed"):
            captured["price_type"] = price_type
            return pd.DataFrame(
                [
                    {"symbol": "MU", "ts_event": "2026-06-24T20:00:00Z", "price": 1160.0},
                    {"symbol": "MU", "ts_event": "2026-06-24T20:05:00Z", "price": 1166.0},
                ]
            )

    class FakeTimeseries:
        def get_range(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeHistorical:
        def __init__(self, key):
            captured["key"] = key
            self.timeseries = FakeTimeseries()

    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    monkeypatch.setenv("PAMS_DATABENTO_DATASET", "DBEQ.BASIC")
    monkeypatch.setenv("PAMS_DATABENTO_SCHEMA", "trades")
    monkeypatch.setitem(sys.modules, "databento", types.SimpleNamespace(Historical=FakeHistorical))

    prices._US_ESTIMATE_QUOTE_CACHE.clear()
    result = prices._fetch_databento_us_estimate_prices(["MU"])

    assert result == {"MU": 1166.0}
    assert captured["key"] == "test-key"
    assert captured["dataset"] == "DBEQ.BASIC"
    assert captured["schema"] == "trades"
    assert captured["symbols"] == ["MU"]
    assert captured["price_type"] == "float"
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["source"] == "databento_estimate"
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["provider"] == "databento"
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["confidence"] == "high"


def test_ibkr_estimate_filter_rejects_yahoo_fallback_for_ibkr_holdings():
    prices._US_ESTIMATE_QUOTE_CACHE.clear()
    prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
        "source": "yahoo_estimate",
        "provider": "yahoo_finance",
        "confidence": "medium",
    }
    prices._US_ESTIMATE_QUOTE_CACHE["NVDA"] = {
        "source": "ibkr_tws_estimate",
        "provider": "ibkr_tws",
        "confidence": "high",
    }

    filtered, skipped = prices._filter_us_estimate_prices_for_holdings(
        [
            {"symbol": "MU", "name": "Micron", "account_code": "ibkr"},
            {"symbol": "NVDA", "name": "NVIDIA", "account_code": "ibkr"},
            {"symbol": "AAPL", "name": "Apple", "account_code": "manual_us"},
        ],
        {"MU": 1151.95, "NVDA": 210.33, "AAPL": 200.0},
    )

    assert filtered == {"NVDA": 210.33, "AAPL": 200.0}
    assert skipped == [
        {
            "symbol": "MU",
            "name": "Micron",
            "account_code": "ibkr",
            "source": "yahoo_estimate",
            "reason": "ibkr_estimate_requires_high_confidence_quote",
        }
    ]


def test_ibkr_estimate_filter_keeps_plausible_project_price_scale():
    prices._US_ESTIMATE_QUOTE_CACHE.clear()
    prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
        "source": "ibkr_tws_estimate",
        "provider": "ibkr_tws",
        "confidence": "high",
    }

    filtered, skipped = prices._filter_us_estimate_prices_for_holdings(
        [
            {
                "symbol": "MU",
                "name": "Micron",
                "account_code": "ibkr",
                "current_price": Decimal("1133.99"),
            }
        ],
        {"MU": 1170.16},
    )

    assert skipped == []
    assert filtered == {"MU": 1170.16}
    assert "scale_correction" not in prices._US_ESTIMATE_QUOTE_CACHE["MU"]


def test_ibkr_estimate_filter_accepts_databento_quote_for_ibkr_holdings():
    prices._US_ESTIMATE_QUOTE_CACHE.clear()
    prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
        "source": "databento_estimate",
        "provider": "databento",
        "confidence": "high",
    }

    filtered, skipped = prices._filter_us_estimate_prices_for_holdings(
        [
            {
                "symbol": "MU",
                "name": "Micron",
                "account_code": "ibkr",
                "current_price": Decimal("1133.99"),
            }
        ],
        {"MU": 1166.0},
    )

    assert skipped == []
    assert filtered == {"MU": 1166.0}


def test_overseas_price_refresh_uses_daily_pnl_window_date(monkeypatch):
    monkeypatch.setattr(prices, "_daily_pnl_window_date", lambda: date(2026, 6, 22))
    monkeypatch.setattr(prices, "_today_cst", lambda: date(2026, 6, 23))

    assert prices._price_refresh_as_of("美股", "estimate") == date(2026, 6, 22)
    assert prices._price_refresh_as_of("A股", "close") == date(2026, 6, 23)


def test_overseas_refresh_query_groups_latest_snapshot_by_market_bucket(monkeypatch):
    executed: list[str] = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append(query)

        def fetchall(self):
            return []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self, cursor_factory=None):
            return FakeCursor()

    monkeypatch.setattr(prices, "_get_conn", lambda: FakeConn())

    assert prices._get_holdings_with_symbol("美股", market_buckets=("us",)) == []
    assert "market_bucket" in executed[0]


def test_ibkr_tws_ports_default_includes_gateway_local_port(monkeypatch):
    monkeypatch.delenv("PAMS_IBKR_TWS_PORTS", raising=False)
    monkeypatch.delenv("IBKR_TWS_PORTS", raising=False)

    assert prices._ibkr_tws_ports() == [4000, 4001, 4002, 7497, 7496]


def test_ibkr_tws_connection_reuses_persistent_socket(monkeypatch):
    created = []

    class FakeIB:
        def __init__(self):
            self.connected = False
            self.connect_calls = []
            created.append(self)

        def connect(self, host, port, clientId, timeout, readonly):
            self.connected = True
            self.connect_calls.append((host, port, clientId, timeout, readonly))

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    monkeypatch.setenv("PAMS_IBKR_TWS_PERSISTENT", "true")
    monkeypatch.setattr(prices, "_IBKR_TWS_CONNECTION", None)
    monkeypatch.setattr(prices, "_IBKR_TWS_CONNECTION_KEY", None)

    first, first_port, first_persistent = prices._get_ibkr_tws_connection(
        FakeIB,
        host="127.0.0.1",
        ports=[4000, 7497],
        client_id=62017,
        timeout=2.0,
    )
    second, second_port, second_persistent = prices._get_ibkr_tws_connection(
        FakeIB,
        host="127.0.0.1",
        ports=[4000, 7497],
        client_id=62017,
        timeout=2.0,
    )

    assert first is second
    assert first_port == 4000
    assert second_port == 4000
    assert first_persistent is True
    assert second_persistent is True
    assert len(created) == 1
    assert created[0].connect_calls == [("127.0.0.1", 4000, 62017, 2.0, True)]


def test_ibkr_tws_estimate_prices_request_overnight_contract(monkeypatch):
    requested_contracts = []
    cancelled_contracts = []

    class FakeContract:
        def __init__(self, symbol, exchange, currency, primaryExchange=None):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.primaryExchange = primaryExchange or ""
            self.conId = None

    class FakeTicker:
        last = 1047.0
        bid = None
        ask = None
        close = None

    class FakeIB:
        def connect(self, host, port, clientId, timeout, readonly):
            self.host = host
            self.port = port
            self.clientId = clientId
            self.timeout = timeout
            self.readonly = readonly

        def reqMarketDataType(self, market_data_type):
            self.market_data_type = market_data_type

        def qualifyContracts(self, contract):
            contract.conId = 8314
            if contract.exchange == "SMART":
                contract.primaryExchange = "NASDAQ"
            return [contract]

        def reqMktData(self, contract, genericTickList, snapshot, regulatorySnapshot):
            requested_contracts.append(contract)
            return FakeTicker()

        def sleep(self, seconds):
            self.sleep_seconds = seconds

        def cancelMktData(self, contract):
            cancelled_contracts.append(contract)

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setenv("PAMS_IBKR_TWS_ENABLED", "true")
    monkeypatch.setenv("PAMS_IBKR_TWS_HOST", "127.0.0.1")
    monkeypatch.setenv("PAMS_IBKR_TWS_PORTS", "7497")
    monkeypatch.setenv("PAMS_IBKR_TWS_SNAPSHOT_WAIT_SECONDS", "0.2")
    monkeypatch.setitem(
        sys.modules,
        "ib_insync",
        types.SimpleNamespace(IB=FakeIB, Stock=FakeContract),
    )

    result = prices._fetch_ibkr_tws_estimate_prices(["MU"])

    assert result == {"MU": 1047.0}
    assert requested_contracts[0].exchange == "OVERNIGHT"
    assert requested_contracts[0].primaryExchange == "NASDAQ"
    assert cancelled_contracts == requested_contracts
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["source"] == "ibkr_tws_estimate"
    assert prices._US_ESTIMATE_QUOTE_CACHE["MU"]["raw"]["contract"]["exchange"] == "OVERNIGHT"


def test_ibkr_tws_ticker_price_normalizes_overnight_fixed_point_ticks():
    class FakeTicker:
        last = 10707600.0
        bid = None
        ask = None
        close = None

    assert prices._ibkr_tws_ticker_price(FakeTicker()) == (1070.76, "last")


def test_upsert_us_estimate_market_daily_writes_quote_source(monkeypatch):
    executed: list[tuple[str, list[tuple]]] = []
    committed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def executemany(self, query, payload):
            executed.append((query, list(payload)))

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            committed.append(True)

    monkeypatch.setattr(prices, "_get_conn", lambda: FakeConn())
    prices._US_ESTIMATE_QUOTE_CACHE.clear()
    prices._US_ESTIMATE_QUOTE_CACHE["MU"] = {
        "source": "ibkr_client_portal_estimate",
        "provider": "ibkr_client_portal",
        "quote_type": "last",
        "confidence": "high",
        "conid": "8314",
        "price": 1047.0,
    }

    count = prices._upsert_us_estimate_market_daily(
        [
            {
                "asset_id": "ASSET_MU",
                "symbol": "MU",
                "name": "Micron",
                "currency": "USD",
            }
        ],
        {"MU": 1047.0},
        date(2026, 6, 17),
    )

    assert count == 1
    assert committed == [True]
    assert "pams_asset_market_daily" in executed[0][0]
    payload = executed[0][1][0]
    assert payload[1] == "ibkr_client_portal_estimate"
    assert payload[6] == "US_EXTENDED"
    assert payload[8] == 1047.0


def test_gold_proxy_refresh_uses_sge_realtime_and_repairs_history(monkeypatch):
    stored: list[tuple[str, list[tuple]]] = []
    repaired: list[tuple[date, date]] = []

    history = [
        (date(2026, 6, 2), 982.0, 989.0, 970.1, 987.45),
        (date(2026, 6, 3), 987.0, 987.0, 972.4, 972.8),
    ]

    monkeypatch.setattr(prices, "_fetch_sge_au9999_history", lambda as_of: history)
    monkeypatch.setattr(prices, "_fetch_sge_au9999_realtime", lambda as_of: (date(2026, 6, 4), 975.2))
    monkeypatch.setattr(
        prices,
        "_upsert_gold_spot_closes",
        lambda rows, source="akshare_sge_daily": stored.append((source, list(rows))) or len(rows),
    )
    monkeypatch.setattr(
        prices,
        "_sync_gold_accum_snapshots_from_spot",
        lambda start, end: repaired.append((start, end)) or 2,
    )
    monkeypatch.setattr(prices, "_latest_gold_proxy_price", lambda as_of=None, exact=False: None)

    quote = prices._gold_proxy_price_for_refresh(date(2026, 6, 4))

    assert quote == 975.2
    assert stored == [
        ("akshare_sge_daily", history),
        ("akshare_sge_realtime", [(date(2026, 6, 4), None, None, None, 975.2)]),
    ]
    assert repaired == [(date(2026, 6, 2), date(2026, 6, 3))]


def test_gold_proxy_refresh_falls_back_to_latest_sge_history(monkeypatch):
    stored: list[tuple[str, list[tuple]]] = []
    repaired: list[tuple[date, date]] = []
    history = [(date(2026, 6, 3), 987.0, 987.0, 972.4, 972.8)]

    monkeypatch.setattr(prices, "_fetch_sge_au9999_history", lambda as_of: history)
    monkeypatch.setattr(prices, "_fetch_sge_au9999_realtime", lambda as_of: None)
    monkeypatch.setattr(
        prices,
        "_upsert_gold_spot_closes",
        lambda rows, source="akshare_sge_daily": stored.append((source, list(rows))) or len(rows),
    )
    monkeypatch.setattr(
        prices,
        "_sync_gold_accum_snapshots_from_spot",
        lambda start, end: repaired.append((start, end)) or 1,
    )
    monkeypatch.setattr(prices, "_latest_gold_proxy_price", lambda as_of=None, exact=False: None)

    quote = prices._gold_proxy_price_for_refresh(date(2026, 6, 4))

    assert quote == 972.8
    assert stored == [("akshare_sge_daily", history)]
    assert repaired == [(date(2026, 6, 3), date(2026, 6, 3))]


def test_gold_proxy_refresh_falls_back_to_stored_spot_when_sge_unavailable(monkeypatch):
    calls: list[tuple[date | None, bool]] = []

    monkeypatch.setattr(prices, "_fetch_sge_au9999_history", lambda as_of: [])
    monkeypatch.setattr(prices, "_fetch_sge_au9999_realtime", lambda as_of: None)
    monkeypatch.setattr(
        prices,
        "_latest_gold_proxy_price",
        lambda as_of=None, exact=False: calls.append((as_of, exact)) or 974.41,
    )

    quote = prices._gold_proxy_price_for_refresh(date(2026, 6, 3))

    assert quote == 974.41
    assert calls == [(date(2026, 6, 3), True)]
