"""Synthetic-only checks for extracted core API routing and origin guard."""
import json
import socket
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient


class Cursor:
    def __init__(self, statements):
        self.statements = statements
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params=None):
        self.statements.append((sql, params))


class Conn:
    def __init__(self, statements):
        self.statements = statements
        self.committed = False
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def set_session(self, **kwargs):
        assert kwargs == {'readonly': True, 'isolation_level': 'REPEATABLE READ'}
    def cursor(self, **kwargs):
        return Cursor(self.statements)
    def commit(self):
        self.committed = True


@pytest.fixture
def prepared(monkeypatch):
    import main
    statements = []
    connection = Conn(statements)
    monkeypatch.setattr(main, "get_conn", lambda: connection)
    monkeypatch.setattr(main, "_holdings_market_view_as_of", lambda cur: date(2026, 1, 2))
    monkeypatch.setattr(main, "_owner_ratio_case_sql", lambda alias: ("synthetic", [1]))
    monkeypatch.setattr(main, "_build_accounts_payload", lambda cur, **kwargs: {"active": kwargs["active"]})
    monkeypatch.setattr(main, "_build_account_detail_payload", lambda cur, **kwargs: {"account": kwargs["account_code"]} if kwargs["account_code"] == "synthetic" else None)
    monkeypatch.setattr(main, "_build_categories_payload", lambda cur: {"categories": ["synthetic"]})
    monkeypatch.setattr(main, "_build_api_holdings_latest_payload", lambda cur, **kwargs: {"limit": kwargs["limit"], "view_date": str(kwargs["view_as_of"])})
    monkeypatch.setattr(main, "_build_holdings_summary_payload", lambda cur, **kwargs: {"base_currency": kwargs["base_currency"]})
    monkeypatch.setattr(main, "_build_holdings_by_account_payload", lambda cur, **kwargs: {"base_currency": kwargs["base_currency"]})
    monkeypatch.setattr(main, "_build_latest_fx_rates_payload", lambda cur, **kwargs: {"base_currency": kwargs["base_currency"]})
    def forbid_socket(*args, **kwargs):
        raise AssertionError("real network connection forbidden")
    monkeypatch.setattr(socket, "create_connection", forbid_socket)
    monkeypatch.setattr(socket, "getaddrinfo", forbid_socket)
    return main, connection, statements, TestClient(main.app)


def test_account_holdings_and_fx_routes_preserve_contract(prepared):
    main, _, _, client = prepared
    assert client.get("/api/accounts?active=false").json() == {"active": False}
    assert client.get("/api/accounts/synthetic").json() == {"account": "synthetic"}
    assert client.get("/api/accounts/missing").status_code == 404
    assert client.get("/api/categories").json() == {"categories": ["synthetic"]}
    assert client.get("/api/holdings/latest?limit=3").json() == {"limit": 3, "view_date": "2026-01-02"}
    assert client.get("/api/holdings/latest?limit=5001").status_code == 422
    assert client.get("/api/holdings/summary?base_currency=USD").json() == {"base_currency": "USD"}
    assert client.get("/api/holdings/by-account?base_currency=MXN").json() == {"base_currency": "MXN"}
    assert client.get("/api/fx-rates/latest?base_currency=USD").json() == {"base_currency": "USD"}


def test_fx_refresh_preserves_origin_guard_and_synthetic_write(prepared, monkeypatch):
    from collectors import open_exchange_rates

    _, conn, statements, client = prepared
    requested = []
    rates = {"CNY": 7, "USD": 1, "MXN": 20, "HKD": 8, "KRW": 1400, "SEK": 10, "JPY": 150, "CHF": 0.8}
    timestamp = int(datetime(2026, 1, 2, 23, tzinfo=timezone.utc).timestamp())

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"base": "USD", "timestamp": timestamp, "rates": rates}).encode()

    def provider(request, *, timeout):
        requested.append(urlsplit(request.full_url).path)
        return Response()

    monkeypatch.setenv("PAMS_OPEN_EXCHANGE_RATES_APP_ID", "synthetic")
    monkeypatch.setattr(open_exchange_rates, "urlopen", provider)
    denied = client.post("/api/fx-rates/refresh", headers={"origin": "https://external.invalid"})
    assert denied.status_code == 403
    assert statements == []
    result = client.post("/api/fx-rates/refresh?as_of=2026-01-02")
    assert result.status_code == 200
    assert requested == ["/api/historical/2026-01-02.json"]
    assert result.json()["source"] == "openexchangerates"
    assert result.json()["stored"] == 64
    assert len(statements) == 64 and conn.committed
    assert all("INSERT INTO pams_fx_rates_daily" in sql for sql, _ in statements)
    quotes = {(params[1], params[2]): params[3] for _, params in statements}
    assert quotes["CNY", "USD"] == Decimal("7")
    assert quotes["CNY", "MXN"] == Decimal("0.35")
    assert quotes["USD", "CNY"] == Decimal("0.14285714")

    stale = client.post("/api/fx-rates/refresh?as_of=2026-01-03")
    assert stale.status_code == 502
    assert len(statements) == 64


def test_fx_refresh_requires_configured_source(prepared, monkeypatch):
    from collectors import open_exchange_rates

    _, _, statements, client = prepared
    monkeypatch.delenv("PAMS_OPEN_EXCHANGE_RATES_APP_ID", raising=False)
    monkeypatch.setattr(open_exchange_rates, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected provider call"))
    assert client.post("/api/fx-rates/refresh").status_code == 503
    assert statements == []


def test_main_keeps_public_python_function_aliases(prepared):
    main, _, _, _ = prepared
    names = ("get_accounts", "get_account_detail", "get_categories", "holdings_latest",
             "holdings_summary", "holdings_by_account", "get_latest_fx_rates", "refresh_fx_rates",
             "summary2_latest", "holdings_latest_compat", "holdings_by_category",
             "holdings_history_by_category", "nav_series", "investment_section_return_series",
             "refresh_investment_section_return_series_snapshot", "nav_weekly", "investment_return_series",
             "refresh_investment_return_series_snapshot", "nav_drawdown", "cashflow_series", "risk_latest")
    endpoints = {route.endpoint for route in main.app.routes if hasattr(route, "endpoint")}
    assert all(getattr(main, name) in endpoints for name in names)
