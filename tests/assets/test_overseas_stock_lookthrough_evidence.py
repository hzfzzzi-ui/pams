from __future__ import annotations

import socket
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2] / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.assets import lookthrough_state  # noqa: E402
from domain.investments import portfolio_state  # noqa: E402


class SnapshotCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params):
        assert sql.lstrip().upper().startswith("WITH ")

    def fetchall(self):
        return self.rows


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)


def load_source_positions(monkeypatch, rows):
    monkeypatch.setattr(portfolio_state, "snapshot_tables", lambda cur: {"table_name": "pams_holdings_snapshot"})
    monkeypatch.setattr(portfolio_state, "_latest_fx_rates", lambda cur, base: {"CNY": Decimal("1"), "SEK": Decimal("1"), "USD": Decimal("1")})
    monkeypatch.setattr(portfolio_state, "_cash_like_carry_forward_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(portfolio_state, "_synthetic_broker_cash_positions", lambda **kwargs: [])
    return portfolio_state.load_investment_positions(
        SnapshotCursor(rows), "CNY", {"accounts": {"ibkr": {"include_in_investments": True}}}
    )


def snapshot(symbol, minor, currency="USD"):
    return {
        "account_code": "ibkr", "asset_liability": "资产", "major_category": "美股",
        "minor_category": minor, "symbol": symbol, "name": f"Synthetic {symbol}",
        "currency": currency, "market_value": Decimal("100"), "cost": Decimal("80"),
        "holding_pnl": Decimal("20"), "quantity": Decimal("1"), "as_of": date(2026, 1, 2),
    }


def test_real_loader_preserves_stock_evidence_without_promoting_fund_or_unknown(monkeypatch):
    positions = load_source_positions(monkeypatch, [
        snapshot("EU", "美股个股", "SEK"), snapshot("US", "美股个股"),
        snapshot("FUND", "美股基金"), snapshot("UNKNOWN", ""),
    ])
    assert {p["asset_type"] for p in positions} == {"us_stock"}
    assert [p["minor_category"] for p in positions] == ["美股个股", "美股个股", "美股基金", ""]

    result = lookthrough_state.derive_lookthrough_payload(
        positions, {"asset_mapping": [], "asset_lookthrough": []}, as_of="2026-01-02"
    )
    quality = {item["symbol"]: item for item in result["quality"]["items"]}
    assert [(quality[s]["lookthrough_type"], quality[s]["coverage_ratio"]) for s in ("EU", "US")] == [("DIRECT", 1.0)] * 2
    assert [(quality[s]["lookthrough_type"], quality[s]["coverage_ratio"]) for s in ("FUND", "UNKNOWN")] == [("PRODUCT_TAG", 0.0)] * 2
    assert result["total_market_value_cny"] == 400
    industries = {item["name"]: item["value_cny"] for item in result["exposures"]["industry"]}
    assert industries["未穿透 / 未映射"] == 400


@pytest.mark.parametrize("mapped_class, minor, expected", [
    ("stock_us", "美股个股", "DIRECT"), ("stock_eu", "美股个股", "DIRECT"),
    ("etf_us", "美股基金", "PRODUCT_TAG"),
])
def test_existing_explicit_mapping_remains_authoritative(mapped_class, minor, expected):
    position = {"symbol": "MAPPED", "asset_name": "Mapped", "asset_type": "us_stock",
                "minor_category": minor, "currency": "USD", "market_value_cny": 100}
    config = {"asset_mapping": [{"symbol": "MAPPED", "asset_name": "Mapped", "asset_class": mapped_class}], "asset_lookthrough": []}
    result = lookthrough_state.derive_lookthrough_payload([position], config)
    assert result["quality"]["items"][0]["lookthrough_type"] == expected


def test_conflicting_source_fund_and_master_stock_are_not_promoted_to_direct(monkeypatch):
    positions = load_source_positions(monkeypatch, [snapshot("CONFLICT", "美股基金", "SEK")])
    lookup = {"assets": {"synthetic-id": {
        "asset_id": "synthetic-id", "primary_symbol": "CONFLICT", "asset_type": "欧股个股",
        "currency": "SEK", "aliases": [], "industry": None, "themes": [],
    }}, "alias_to_id": {}}
    monkeypatch.setattr(lookthrough_state, "load_asset_master_lookup", lambda *args, **kwargs: lookup)
    result = lookthrough_state.derive_lookthrough_payload(
        positions, {"asset_mapping": [], "asset_lookthrough": []}, asset_master_cur=object()
    )
    quality = result["quality"]["items"][0]
    assert quality["lookthrough_type"] == "PRODUCT_TAG"
    assert quality["coverage_ratio"] == 0
    assert result["total_market_value_cny"] == 100
