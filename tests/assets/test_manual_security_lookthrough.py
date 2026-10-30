from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys

import pytest

API_ROOT = Path(__file__).resolve().parents[2] / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.assets import lookthrough_state as lt  # noqa: E402


def _position(**overrides):
    return {"account_code": "ibkr", "asset_id": "TEST", "symbol": "TEST", "asset_name": "Example",
            "asset_type": "us_stock", "currency": "SEK", "holding_snapshot_currency": "SEK",
            "holding_snapshot_asset_id": "MANUAL_TEST", "market_value": Decimal("100"),
            "market_value_cny": Decimal("70"), "cost_cny": Decimal("60"),
            "holding_pnl_cny": Decimal("10"), "as_of": "2026-01-01", **overrides}


def _asset(**overrides):
    return {"asset_id": "MANUAL_TEST", "primary_symbol": "TEST", "canonical_name": "Example",
            "display_name": "Example", "asset_type": "欧股个股", "currency": "SEK",
            "source": "manual", "status": "active", "aliases": [], "themes": [],
            "industry": None, **overrides}


def _derive(monkeypatch, position, assets, config=None):
    lookup = {"assets": {a["asset_id"]: a for a in assets}, "alias_to_id": {}}
    calls = []
    def load(cur, *, ensure_schema):
        calls.append(ensure_schema)
        return lookup
    monkeypatch.setattr(lt, "load_asset_master_lookup", load)
    result = lt.derive_lookthrough_payload([deepcopy(position)], config or {"asset_mapping": [], "asset_lookthrough": []},
                                           as_of="2026-01-01", asset_master_cur=object())
    assert calls == [False]
    return result


def test_manual_linked_stock_changes_coverage_without_changing_money(monkeypatch):
    position = _position()
    before = lt.derive_lookthrough_payload([deepcopy(position)], {"asset_mapping": [], "asset_lookthrough": []})
    after = _derive(monkeypatch, position, [_asset()])
    assert before["quality"]["items"][0]["lookthrough_type"] == "PRODUCT_TAG"
    assert after["quality"]["items"][0]["lookthrough_type"] == "DIRECT"
    assert after["quality"]["items"][0]["coverage_ratio"] == 1
    assert before["total_market_value_cny"] == after["total_market_value_cny"] == 70
    assert before["gross_exposure_cny"] == after["gross_exposure_cny"] == 70
    assert sum(x["effectiveMarketValue"] for x in after["underlying_holdings"]) == 70
    assert position["market_value"] == 100 and position["cost_cny"] == 60 and position["holding_pnl_cny"] == 10


@pytest.mark.parametrize("position,assets", [
    (_position(holding_snapshot_asset_id=None), [_asset()]),
    (_position(holding_snapshot_currency=None), [_asset()]),
    (_position(holding_snapshot_currency="USD"), [_asset()]),
    (_position(symbol="OTHER"), [_asset()]),
    (_position(), [_asset(source="system")]),
    (_position(), [_asset(status="inactive")]),
    (_position(), [_asset(asset_type="ETF / 基金")]),
    (_position(), [_asset(asset_type="未知")]),
    (_position(), [_asset(), _asset(asset_id="OTHER_ID", source="system")]),
    (_position(), []),
    (_position(symbol="00001.HK", currency="HKD", holding_snapshot_currency="HKD"),
     [_asset(primary_symbol="000001.SZ", asset_type="A股个股", currency="HKD")]),
])
def test_missing_conflicting_or_non_stock_evidence_does_not_create_direct(monkeypatch, position, assets):
    result = _derive(monkeypatch, position, assets)
    assert result["quality"]["items"][0]["lookthrough_type"] != "DIRECT"
    assert result["total_market_value_cny"] == 70


def test_explicit_fund_mapping_and_lookthrough_take_precedence(monkeypatch):
    config = {"asset_mapping": [{"symbol": "TEST", "asset_name": "Example Fund", "asset_class": "etf_us"}],
              "asset_lookthrough": [{"parent_symbol": "TEST", "underlying_symbol": "CHILD",
                                     "underlying_name": "Child", "underlying_weight": 0.4,
                                     "lookthrough_type": "TOP10"}]}
    result = _derive(monkeypatch, _position(), [_asset()], config)
    item = result["quality"]["items"][0]
    assert item["lookthrough_type"] == "TOP10"
    assert item["coverage_ratio"] == pytest.approx(0.4)
    assert result["total_market_value_cny"] == 70
    assert sum(x["effectiveMarketValue"] for x in result["underlying_holdings"]) == pytest.approx(70)


def test_explicit_fund_mapping_without_holdings_stays_product_tag(monkeypatch):
    config = {"asset_mapping": [{"symbol": "TEST", "asset_class": "etf_us"}], "asset_lookthrough": []}
    result = _derive(monkeypatch, _position(), [_asset()], config)
    assert result["quality"]["items"][0]["lookthrough_type"] == "PRODUCT_TAG"
    assert result["quality"]["items"][0]["coverage_ratio"] == 0


def test_same_name_a_h_and_fund_stay_distinct_and_value_is_conserved(monkeypatch):
    assets = [_asset(asset_id="CN", primary_symbol="600000.SH", asset_type="A股个股", currency="CNY"),
              _asset(asset_id="HK", primary_symbol="00001.HK", asset_type="港股个股", currency="HKD"),
              _asset(asset_id="FUND", primary_symbol="ETF", asset_type="ETF / 基金", currency="USD")]
    lookup = {"assets": {a["asset_id"]: a for a in assets}, "alias_to_id": {}}
    monkeypatch.setattr(lt, "load_asset_master_lookup", lambda *args, **kwargs: lookup)
    positions = [
        _position(symbol="600000.SH", asset_id="600000.SH", asset_type="a_share_stock", currency="CNY",
                  holding_snapshot_currency="CNY", holding_snapshot_asset_id="CN", asset_name="Shared", market_value_cny=30),
        _position(symbol="00001.HK", asset_id="00001.HK", currency="HKD", holding_snapshot_currency="HKD",
                  holding_snapshot_asset_id="HK", asset_name="Shared", market_value_cny=40),
        _position(symbol="ETF", asset_id="ETF", currency="USD", holding_snapshot_currency="USD",
                  holding_snapshot_asset_id="FUND", asset_name="Fund", market_value_cny=50),
    ]
    before = lt.derive_lookthrough_payload(deepcopy(positions), {"asset_mapping": [], "asset_lookthrough": []})
    after = lt.derive_lookthrough_payload(deepcopy(positions), {"asset_mapping": [], "asset_lookthrough": []},
                                         asset_master_cur=object())
    items = {item["symbol"]: item for item in after["quality"]["items"]}
    assert items["600000.SH"]["lookthrough_type"] == "DIRECT"
    assert items["00001.HK"]["lookthrough_type"] == "DIRECT"
    assert items["ETF"]["lookthrough_type"] == "PRODUCT_TAG"
    assert before["total_market_value_cny"] == after["total_market_value_cny"] == 120
    assert sum(x["market_value_cny"] for x in after["quality"]["items"]) == pytest.approx(120)
    # The existing API intentionally omits unmapped product-tag-only funds from underlying holdings.
    assert sum(x["effectiveMarketValue"] for x in before["underlying_holdings"]) == pytest.approx(30)
    assert sum(x["effectiveMarketValue"] for x in after["underlying_holdings"]) == pytest.approx(70)


def test_asset_master_read_failure_still_propagates(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("schema unavailable")
    monkeypatch.setattr(lt, "load_asset_master_lookup", fail)
    with pytest.raises(RuntimeError, match="schema unavailable"):
        lt.derive_lookthrough_payload([_position()], {"asset_mapping": [], "asset_lookthrough": []}, asset_master_cur=object())


def test_forged_direct_marker_is_ignored_without_master_cursor():
    position = _position(_manual_direct_security=True)
    result = lt.derive_lookthrough_payload([position], {"asset_mapping": [], "asset_lookthrough": []})
    assert result["quality"]["items"][0]["lookthrough_type"] == "PRODUCT_TAG"


def test_forged_direct_marker_is_ignored_when_master_identity_is_untrusted(monkeypatch):
    result = _derive(monkeypatch, _position(_manual_direct_security=True), [_asset(source="system")])
    assert result["quality"]["items"][0]["lookthrough_type"] == "PRODUCT_TAG"


def test_forged_direct_marker_is_ignored_on_explicit_sync(monkeypatch):
    seen = []
    def sync(cur, **kwargs):
        seen.extend(kwargs["positions"])
        return {"assets": {}, "alias_to_id": {}}
    monkeypatch.setattr(lt, "sync_asset_master_from_analysis", sync)
    result = lt.derive_lookthrough_payload([_position(_manual_direct_security=True)],
                                           {"asset_mapping": [], "asset_lookthrough": []},
                                           asset_master_cur=object(), sync_asset_master=True)
    assert result["quality"]["items"][0]["lookthrough_type"] == "PRODUCT_TAG"
    assert seen and "_manual_direct_security" not in seen[0]
