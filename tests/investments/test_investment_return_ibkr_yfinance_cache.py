from __future__ import annotations

import socket
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

API_ROOT = Path(__file__).resolve().parents[2] / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments import investment_return_ibkr as ibkr  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache_and_network(monkeypatch):
    monkeypatch.setattr(ibkr, "_YFINANCE_CLOSE_CACHE", {})

    def deny_socket(*args, **kwargs):
        raise AssertionError("network access is forbidden in synthetic Yahoo tests")

    monkeypatch.setattr(socket, "socket", deny_socket)


def fetch(symbols):
    return ibkr._fetch_yfinance_closes(
        symbols, start_date=date(2026, 1, 2), end_date=date(2026, 1, 3)
    )


def test_empty_result_preserves_warning_on_repeated_cache_hits(monkeypatch):
    calls = []

    def download(tickers, **kwargs):
        calls.append(tickers)
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    expected = ({}, ["missing Yahoo close: AAA, BBB"])
    assert fetch(["BBB", "AAA"]) == expected
    assert fetch(["AAA", "BBB"]) == expected
    assert fetch(["BBB", "AAA"]) == expected
    assert len(calls) == 3  # one batch and one fallback per symbol


def test_partial_result_preserves_prices_and_warning_without_redownload(monkeypatch):
    calls = []

    def download(tickers, **kwargs):
        calls.append(tickers)
        if isinstance(tickers, list):
            return pd.DataFrame({("AAA", "Close"): [101.0]}, index=pd.to_datetime(["2026-01-02"]))
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    closes, warnings = fetch(["BBB", "AAA"])
    assert closes == {"AAA": {date(2026, 1, 2): Decimal("101.0")}}
    assert warnings == ["missing Yahoo close: BBB"]
    warnings.clear()
    assert fetch(["AAA", "BBB"]) == (closes, ["missing Yahoo close: BBB"])
    assert len(calls) == 2


def test_download_failure_is_not_cached_and_next_request_can_recover(monkeypatch):
    calls = []

    def download(tickers, **kwargs):
        calls.append(tickers)
        if len(calls) == 1:
            raise RuntimeError("synthetic failure")
        return pd.DataFrame({"Close": [123.0]}, index=pd.to_datetime(["2026-01-02"]))

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    assert fetch(["AAA"]) == ({}, ["yfinance download failed: synthetic failure"])
    expected = ({"AAA": {date(2026, 1, 2): Decimal("123.0")}}, [])
    assert fetch(["AAA"]) == expected
    assert fetch(["AAA"]) == expected
    assert len(calls) == 2
