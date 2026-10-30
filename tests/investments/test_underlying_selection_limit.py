"""Bounded attribution selection keeps same-name security groups observable."""
from __future__ import annotations

import sys
import copy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))
from domain.investments.return_attribution import _underlying_rows  # noqa: E402
import domain.investments.return_attribution as attribution  # noqa: E402


def unit(symbol, pnl, name, market="A股"):
    return {"name": name, "symbol": symbol, "market": market, "currency": "CNY",
            "pnl_cny": pnl, "daily_pnl_cny": pnl / 2, "daily_price_pnl_cny": pnl / 3,
            "daily_fx_pnl_cny": pnl / 6, "market_value_cny": abs(pnl) * 10}


def select(units):
    meta = {}
    rows = _underlying_rows(units, total_pnl=sum(u["pnl_cny"] for u in units),
                            total_daily_pnl=0, total_market_value=0,
                            preserve_names=True, selection_meta=meta)
    return rows, meta


@pytest.mark.parametrize("ordinary_count,a,h", [(199, 2, 0.1), (200, 1.6, 1.6)])
def test_old_top_name_survives_when_one_or_both_split_rows_fall_outside_200(ordinary_count, a, h):
    ordinary = [unit(f"X{i}", 3, f"资产{i}") for i in range(ordinary_count)]
    siblings = [unit("600001", a, "同名"), unit("01234.HK", h, "同名", "港股")]
    for order in (ordinary + siblings, list(reversed(ordinary + siblings))):
        result, meta = select(order)
        selected = [row for row in result if row["name"] == "同名"]
        assert {row["symbol"] for row in selected} == {"600001", "01234.HK"}
        assert sum(row["pnl_cny"] for row in selected) == pytest.approx(a + h)
        assert meta["required_omitted_count"] == 0
        assert not meta["truncated"]
        assert len(result) <= 500
        assert all("_group_key" not in row for row in result)
        assert [abs(row["pnl_cny"]) for row in result] == sorted((abs(row["pnl_cny"]) for row in result), reverse=True)
        for field in ("pnl_cny", "daily_pnl_cny", "daily_price_pnl_cny", "daily_fx_pnl_cny", "market_value_cny"):
            assert sum(row[field] for row in result) == pytest.approx(sum(row[field] for row in order))


def test_underlying_keeps_old_limit_for_unrelated_names_and_holding_is_unchanged():
    units = [unit(f"X{i}", 1000 - i, f"资产{i}") for i in range(201)]
    holding = _underlying_rows(units, total_pnl=0, total_daily_pnl=0, total_market_value=0)
    underlying, meta = select(units)
    assert len(holding) == len(underlying) == 200
    assert [row["symbol"] for row in holding] == [row["symbol"] for row in underlying]
    assert meta["total_count"] == 201 and meta["returned_count"] == 200 and meta["truncated"]
    assert meta["required_omitted_count"] == 0 and meta["incomplete_name_count"] == 0


def test_source_name_aliases_connect_all_related_securities():
    ordinary = [unit(f"X{i}", 3, f"资产{i}") for i in range(199)]
    same_security = [unit("600001", 2, "同名"), unit("600001", 0, "另一名称")]
    second = unit("01234.HK", 0.1, "另一名称", "港股")
    result, meta = select(ordinary + same_security + [second])
    assert {row["symbol"] for row in result if row.get("symbol") in {"600001", "01234.HK"}} == {"600001", "01234.HK"}
    assert meta["required_omitted_count"] == 0


def test_extreme_same_name_group_is_bounded_and_explicitly_marked_incomplete():
    units = [unit(f"X{i}", 1, "同名") for i in range(800)]
    result, meta = select(units)
    assert len(result) == 500 and meta["hard_limit"] == 500
    assert meta["total_count"] == 800 and meta["required_omitted_count"] == 300
    assert meta["incomplete_name_count"] == 1 and meta["truncated"]
    assert sum(row["pnl_cny"] for row in result) == pytest.approx(500)


def test_complete_synthetic_selection_conserves_all_financial_fields():
    units = [unit(f"X{i}", (-1 if i % 2 else 1) * (i + 1), f"资产{i}") for i in range(150)]
    for ordered in (units, list(reversed(units))):
        result, meta = select(ordered)
        assert not meta["truncated"]
        for field in ("pnl_cny", "daily_pnl_cny", "daily_price_pnl_cny", "daily_fx_pnl_cny", "market_value_cny"):
            assert sum(row[field] for row in result) == pytest.approx(sum(row[field] for row in units))


def test_old_consumer_fields_and_holding_payload_stay_identical(monkeypatch):
    positions = [{"asset_name": "合成股票", "symbol": "600001", "asset_type": "stock_cn",
                  "account_code": "one", "currency": "CNY", "market_value_cny": 100,
                  "holding_pnl_cny": 12, "daily_pnl_cny": 2}]
    config = {"asset_mapping": [], "asset_lookthrough": []}
    current = attribution.build_return_attribution_from_positions(copy.deepcopy(positions), copy.deepcopy(config))
    assert current["quality"]["underlying_selection"]["truncated"] is False
    actual = attribution._underlying_rows

    def legacy(*args, **kwargs):
        kwargs.pop("preserve_names", None)
        kwargs.pop("selection_meta", None)
        return actual(*args, **kwargs)

    monkeypatch.setattr(attribution, "_underlying_rows", legacy)
    previous = attribution.build_return_attribution_from_positions(copy.deepcopy(positions), copy.deepcopy(config))
    current["quality"].pop("underlying_selection")
    previous["quality"].pop("underlying_selection")
    assert current == previous
