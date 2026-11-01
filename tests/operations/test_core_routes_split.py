"""Synthetic-only checks for extracted core API routing and origin guard."""
import json
import socket
from datetime import date

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
    import urllib.request
    main, conn, statements, client = prepared
    currencies = ("CNY", "USD", "MXN", "HKD", "KRW", "SEK")
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"result": "success", "rates": {c: 2.0 for c in currencies}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response())
    denied = client.post("/api/fx-rates/refresh", headers={"origin": "https://external.invalid"})
    assert denied.status_code == 403
    assert statements == []
    result = client.post("/api/fx-rates/refresh")
    assert result.status_code == 200
    assert result.json()["stored"] == 36
    assert len(statements) == 36 and conn.committed
    assert all("INSERT INTO pams_fx_rates_daily" in sql for sql, _ in statements)


def test_main_keeps_public_python_function_aliases(prepared):
    main, _, _, _ = prepared
    names = ("get_accounts", "get_account_detail", "get_categories", "holdings_latest",
             "holdings_summary", "holdings_by_account", "get_latest_fx_rates", "refresh_fx_rates",
             "summary2_latest", "holdings_latest_compat", "holdings_by_category", "account_performance",
             "holdings_history_by_category", "nav_series", "investment_section_return_series",
             "refresh_investment_section_return_series_snapshot", "nav_weekly", "investment_return_series",
             "refresh_investment_return_series_snapshot", "nav_drawdown", "cashflow_series", "risk_latest")
    endpoints = {route.endpoint for route in main.app.routes if hasattr(route, "endpoint")}
    assert all(getattr(main, name) in endpoints for name in names)
