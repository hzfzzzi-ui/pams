from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import services.return_attribution as return_attribution  # noqa: E402
from services.exposure_engine import UNMAPPED_INDUSTRY  # noqa: E402
from services.return_attribution import (  # noqa: E402
    _daily_row_to_position,
    _is_return_attribution_row,
    _merge_daily_positions,
    _merge_realized_trade_positions,
    _return_reconciliation_units,
    build_return_attribution_from_positions,
)


def _row(rows: list[dict], name: str) -> dict:
    return next(item for item in rows if item["name"] == name)


def _mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gbk")


def test_daily_closed_row_repairs_mojibake_category_for_return_attribution() -> None:
    position = _daily_row_to_position(
        {
            "account_code": "ibkr",
            "symbol": "000660.KS",
            "name": "000660.KS - SK HYNIX INC",
            "major_category": _mojibake("美股"),
            "minor_category": _mojibake("美股基金"),
            "currency": "KRW",
            "market_value_cny": 0.0,
            "holding_pnl_cny": 3885.76,
            "daily_pnl_cny": 0.0,
        }
    )

    assert position["major_category"] == "美股"
    assert position["minor_category"] == "美股基金"
    assert position["asset_type"] == "us_stock"
    assert _is_return_attribution_row(position)


def test_return_attribution_payload_uses_latest_holdings_without_reconciliation(monkeypatch) -> None:
    calls: dict[str, object] = {}
    derive_kwargs: dict[str, object] = {}

    class FakeCursor:
        pass

    def fake_holdings_latest(cur, **kwargs):
        calls.update(kwargs)
        return {
            "as_of": "2026-06-12",
            "rows": [
                {
                    "account_code": "tonghuashun",
                    "symbol": "600487",
                    "name": "Hengtong",
                    "major_category": "A股",
                    "minor_category": "A股",
                    "currency": "CNY",
                    "market_value_cny": 100.0,
                    "cost_cny": 90.0,
                    "holding_pnl_cny": 12.0,
                    "daily_pnl_cny": 3.0,
                    "daily_fx_pnl_cny": 0.0,
                    "as_of": "2026-06-12",
                },
                {
                    "account_code": "cash_account",
                    "symbol": "CASH_CNY",
                    "name": "现金",
                    "major_category": "现金",
                    "minor_category": "现金/活期",
                    "currency": "CNY",
                    "market_value_cny": 20.0,
                    "cost_cny": 20.0,
                    "holding_pnl_cny": 0.0,
                    "as_of": "2026-06-12",
                },
                {
                    "account_code": "loan_account",
                    "symbol": "LOAN_OUT",
                    "name": "他人外借待归还",
                    "major_category": "应收款",
                    "minor_category": "他人借款",
                    "currency": "CNY",
                    "market_value_cny": 30.0,
                    "cost_cny": 30.0,
                    "holding_pnl_cny": 0.0,
                    "as_of": "2026-06-12",
                },
            ],
        }

    def fake_derive_lookthrough(positions, config, **kwargs):
        derive_kwargs.update(kwargs)
        return {
            "as_of": kwargs.get("as_of"),
            "normalized_holdings": [
                {
                    **position,
                    "name": position.get("asset_name"),
                    "account": position.get("account_code"),
                    "marketValue": position.get("market_value_cny"),
                    "assetType": position.get("asset_type"),
                    "industryBucket": UNMAPPED_INDUSTRY,
                    "themeExposures": [],
                }
                for position in positions
            ],
            "underlying_holdings": [],
        }

    def fake_daily_positions(cur, account_cfg):
        return [
            {
                "account_code": "ibkr",
                "symbol": "000660.KS",
                "asset_name": "SK HYNIX INC",
                "asset_type": "us_stock",
                "major_category": "美股",
                "minor_category": "美股个股",
                "currency": "KRW",
                "market_value_cny": 0.0,
                "cost_cny": 0.0,
                "holding_pnl_cny": -5.0,
                "daily_pnl_cny": 0.0,
                "daily_price_pnl_cny": 0.0,
                "daily_fx_pnl_cny": 0.0,
                "daily_total_pnl_cny": 0.0,
                "as_of": "2026-06-12",
                "source_type": "holdings_view_closed",
            }
        ]

    monkeypatch.setattr(return_attribution, "_load_accounts_config", lambda: {"defaults": {"owner_ratio": 1}, "accounts": {}})
    monkeypatch.setattr(return_attribution, "_latest_snapshot_date", lambda cur: "2026-06-12")
    monkeypatch.setattr(return_attribution, "_load_sw_l1_lookup", lambda cur: {})
    monkeypatch.setattr(return_attribution, "build_holdings_latest_payload", fake_holdings_latest)
    monkeypatch.setattr(return_attribution, "_load_daily_attribution_positions", fake_daily_positions)
    monkeypatch.setattr(return_attribution, "load_lookthrough_config", lambda config=None: {"asset_mapping": [], "asset_lookthrough": []})
    monkeypatch.setattr(
        return_attribution,
        "load_account_balances",
        lambda cur, base_currency, account_cfg: [
            {"amount_cny": 120.0, "include_in_nav": True},
            {"amount_cny": 999.0, "include_in_nav": False},
        ],
    )
    monkeypatch.setattr(return_attribution, "derive_lookthrough_payload", fake_derive_lookthrough)

    payload = return_attribution.build_return_attribution_payload(FakeCursor())

    assert calls["limit"] == 5000
    assert calls["include_closed"] is False
    assert payload["as_of"] == "2026-06-12"
    assert payload["portfolio_nav_cny"] == pytest.approx(120.0)
    assert payload["gross_exposure_cny"] == pytest.approx(100.0)
    assert payload["total_market_value_cny"] == pytest.approx(100.0)
    assert payload["total_pnl_cny"] == pytest.approx(7.0)
    assert payload["daily_pnl_cny"] == pytest.approx(3.0)
    assert _row(payload["dimensions"]["holding"], "Hengtong")["pnl_cny"] == pytest.approx(12.0)
    closed = _row(payload["dimensions"]["holding"], "SK HYNIX INC")
    assert closed["market_value_cny"] == pytest.approx(0.0)
    assert closed["pnl_cny"] == pytest.approx(-5.0)
    assert not any("RETURN_RECONCILIATION" in str(row.get("symbol")) for row in payload["dimensions"]["holding"])
    assert "return_reconciliation" not in payload["quality"]
    assert derive_kwargs["sync_asset_master"] is False


def test_return_attribution_forwards_explicit_asset_master_sync(monkeypatch) -> None:
    derive_kwargs: dict[str, object] = {}

    def fake_derive_lookthrough(positions, config, **kwargs):
        derive_kwargs.update(kwargs)
        return {
            "as_of": kwargs.get("as_of"),
            "normalized_holdings": [],
            "underlying_holdings": [],
            "portfolio_nav_cny": kwargs.get("portfolio_nav_cny"),
        }

    monkeypatch.setattr(return_attribution, "derive_lookthrough_payload", fake_derive_lookthrough)

    build_return_attribution_from_positions(
        [
            {
                "asset_name": "NVIDIA",
                "symbol": "NVDA",
                "asset_type": "stock_us",
                "currency": "USD",
                "market_value_cny": 100.0,
                "holding_pnl_cny": 10.0,
            }
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        asset_master_cur=object(),
        sync_asset_master=True,
        portfolio_nav_cny=100.0,
    )

    assert derive_kwargs["sync_asset_master"] is True


def test_daily_overlay_does_not_override_snapshot_total_value_or_pnl() -> None:
    merged = _merge_daily_positions(
        [
            {
                "account_code": "acct",
                "asset_name": "华为 ESOP",
                "symbol": "HUAWEI_ESOP",
                "asset_type": "esop",
                "market_value_cny": 318000.0,
                "holding_pnl_cny": 0.0,
            }
        ],
        [
            {
                "account_code": "acct",
                "asset_name": "华为 ESOP",
                "symbol": "HUAWEI_ESOP",
                "market_value_cny": 318000.0,
                "holding_pnl_cny": 37120.0,
                "daily_pnl_cny": 12.0,
                "daily_price_pnl_cny": 10.0,
                "daily_fx_pnl_cny": 2.0,
                "daily_total_pnl_cny": 12.0,
            }
        ],
    )

    assert len(merged) == 1
    assert merged[0]["market_value_cny"] == 318000.0
    assert merged[0]["holding_pnl_cny"] == 0.0
    assert merged[0]["daily_total_pnl_cny"] == 12.0
    assert merged[0]["daily_price_pnl_cny"] == 10.0
    assert merged[0]["daily_fx_pnl_cny"] == 2.0


def test_return_reconciliation_units_close_section_target_without_exposure_pollution() -> None:
    corrections, meta = _return_reconciliation_units(
        [
            {
                "account": "tonghuashun",
                "market_value_cny": 207439.375,
                "pnl_cny": -16392.10,
                "daily_pnl_cny": -2815.415625,
                "daily_price_pnl_cny": -2815.415625,
                "daily_fx_pnl_cny": 0.0,
            }
        ],
        {
            "a_stock": {
                "market_value_cny": 240962.225,
                "pnl_cny": -9037.775,
                "daily_pnl_cny": -2311.039479,
            }
        },
    )

    assert len(corrections) == 1
    correction = corrections[0]
    assert correction["source"] == "return_reconciliation"
    assert correction["asset_type"] == "其他资产"
    assert correction["theme_exposures"] == []
    assert correction["market_value_cny"] == pytest.approx(33522.85)
    assert correction["pnl_cny"] == pytest.approx(7354.325)
    assert correction["daily_pnl_cny"] == pytest.approx(504.376146)
    assert meta["sections"][0]["section"] == "a_stock"


def test_return_reconciliation_sets_canonical_total_without_visible_unit(monkeypatch) -> None:
    monkeypatch.setattr(return_attribution, "_latest_snapshot_date", lambda cur: "2026-06-19")
    monkeypatch.setattr(
        return_attribution,
        "_section_target_payloads",
        lambda *args, **kwargs: (
            {
                "a_stock": {
                    "market_value_cny": 120.0,
                    "pnl_cny": -20.0,
                    "daily_pnl_cny": 3.0,
                }
            },
            [],
        ),
    )

    payload = build_return_attribution_from_positions(
        [
            {
                "account_code": "tonghuashun",
                "asset_name": "紫金矿业",
                "symbol": "601899",
                "asset_type": "stock_cn",
                "currency": "CNY",
                "market_value_cny": 100.0,
                "holding_pnl_cny": -30.0,
                "daily_pnl_cny": 1.0,
            }
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        account_cfg={"defaults": {"owner_ratio": 1}, "accounts": {"tonghuashun": {"owner_ratio": 1}}},
        reconciliation_cur=object(),
        portfolio_nav_cny=120.0,
    )

    assert payload["total_pnl_cny"] == pytest.approx(-30.0)
    assert payload["canonical_total_pnl_cny"] == pytest.approx(-20.0)
    assert payload["canonical_daily_pnl_cny"] == pytest.approx(3.0)
    assert not any("RETURN_RECONCILIATION" in str(row.get("symbol")) for row in payload["dimensions"]["holding"])
    assert payload["quality"]["return_reconciliation"]["sections"][0]["pnl_delta_cny"] == pytest.approx(10.0)


def test_return_attribution_uses_exposure_dimensions_without_cash_in_industry() -> None:
    payload = build_return_attribution_from_positions(
        [
            {
                "asset_name": "NVIDIA",
                "symbol": "NVDA",
                "asset_type": "stock_us",
                "currency": "USD",
                "market_value_cny": 100.0,
                "holding_pnl_cny": 10.0,
                "daily_pnl_cny": 2.0,
                "daily_fx_pnl_cny": 0.5,
            },
            {
                "asset_name": "紫金矿业",
                "symbol": "601899",
                "asset_type": "stock_cn",
                "currency": "CNY",
                "market_value_cny": 50.0,
                "holding_pnl_cny": -5.0,
                "sw_l1": "有色金属",
            },
            {
                "asset_name": "现金",
                "symbol": "CASH",
                "asset_type": "cash",
                "currency": "CNY",
                "market_value_cny": 20.0,
                "holding_pnl_cny": 1.0,
            },
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        portfolio_nav_cny=140.0,
    )

    assert payload["portfolio_nav_cny"] == pytest.approx(140.0)
    assert payload["gross_exposure_cny"] == pytest.approx(170.0)
    assert payload["total_pnl_cny"] == pytest.approx(6.0)
    assert _row(payload["dimensions"]["industry"], "科技TMT")["pnl_cny"] == pytest.approx(10.0)
    assert _row(payload["dimensions"]["industry"], "周期资源")["pnl_cny"] == pytest.approx(-5.0)
    assert not any(row["name"] == "现金" for row in payload["dimensions"]["industry"])
    assert _row(payload["dimensions"]["asset_type"], "现金")["pnl_cny"] == pytest.approx(1.0)
    assert _row(payload["dimensions"]["theme"], "AI / 算力")["pnl_cny"] == pytest.approx(10.0)
    assert _row(payload["dimensions"]["theme"], "半导体")["pnl_cny"] == pytest.approx(10.0)


def test_fund_pnl_is_allocated_to_top_underlyings_and_residual() -> None:
    config = {
        "asset_mapping": [
            {
                "asset_name": "AI Fund",
                "symbol": "FUND1",
                "asset_class": "fund",
                "risk_bucket": "US_GLOBAL_TECH",
                "region": "United States",
                "currency": "USD",
                "industry": "科技综合",
                "theme": "AI",
                "style": "成长",
                "mapping_confidence": "MEDIUM",
            }
        ],
        "asset_lookthrough": [
            {
                "parent_symbol": "FUND1",
                "parent_asset_name": "AI Fund",
                "lookthrough_type": "TOP10",
                "source_type": "FUND_REPORT",
                "confidence": "MEDIUM",
                "as_of": "2026-06-01",
                "holdings": [
                    {
                        "underlying_name": "NVIDIA",
                        "underlying_symbol": "NVDA",
                        "underlying_weight": 0.6,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "Semiconductors",
                        "underlying_theme": "AI",
                    },
                    {
                        "underlying_name": "Microsoft",
                        "underlying_symbol": "MSFT",
                        "underlying_weight": 0.2,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "Software",
                        "underlying_theme": "Cloud",
                    },
                ],
            }
        ],
    }
    payload = build_return_attribution_from_positions(
        [
            {
                "asset_name": "AI Fund",
                "symbol": "FUND1",
                "asset_type": "fund",
                "currency": "USD",
                "market_value_cny": 1000.0,
                "holding_pnl_cny": 100.0,
                "daily_pnl_cny": 5.0,
            }
        ],
        config,
        as_of="2026-06-02",
    )

    underlying = payload["dimensions"]["underlying"]
    assert _row(underlying, "NVIDIA")["pnl_cny"] == pytest.approx(60.0)
    assert _row(underlying, "Microsoft")["pnl_cny"] == pytest.approx(20.0)
    assert _row(underlying, "未穿透残差")["pnl_cny"] == pytest.approx(20.0)
    assert _row(payload["dimensions"]["industry"], "科技TMT")["pnl_cny"] == pytest.approx(80.0)
    assert _row(payload["dimensions"]["industry"], "未穿透 / 未映射")["pnl_cny"] == pytest.approx(20.0)
    assert _row(payload["dimensions"]["asset_type"], "ETF / 基金")["pnl_cny"] == pytest.approx(100.0)
    assert _row(payload["dimensions"]["theme"], "AI / 算力")["pnl_cny"] == pytest.approx(76.0)
    assert not any(
        detail.get("underlying_name") == "未穿透残差"
        for row in payload["dimensions"]["theme"]
        for detail in row.get("source_details", [])
    )


def test_closed_zero_value_holding_can_still_contribute_to_pnl_attribution() -> None:
    payload = build_return_attribution_from_positions(
        [
            {
                "asset_name": "工业富联",
                "symbol": "601138",
                "asset_type": "stock_cn",
                "currency": "CNY",
                "market_value_cny": 0.0,
                "holding_pnl_cny": 30.0,
                "sw_l1": "电子",
            }
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
    )

    assert payload["total_pnl_cny"] == pytest.approx(30.0)
    assert _row(payload["dimensions"]["holding"], "工业富联")["pnl_cny"] == pytest.approx(30.0)
    assert _row(payload["dimensions"]["industry"], "科技TMT")["pnl_cny"] == pytest.approx(30.0)
    assert _row(payload["dimensions"]["theme"], "AI / 算力")["pnl_cny"] == pytest.approx(30.0)


def test_realized_trade_fact_positions_merge_partial_and_append_closed() -> None:
    merged = _merge_realized_trade_positions(
        [
            {
                "account_code": "tonghuashun",
                "symbol": "600487",
                "asset_name": "亨通光电",
                "market_value_cny": 100.0,
                "holding_pnl_cny": 10.0,
                "daily_pnl_cny": 1.0,
            }
        ],
        [
            {
                "account_code": "tonghuashun",
                "symbol": "600487",
                "asset_name": "亨通光电",
                "market_value_cny": 0.0,
                "holding_pnl_cny": 7.0,
                "daily_pnl_cny": 7.0,
                "daily_price_pnl_cny": 7.0,
                "daily_total_pnl_cny": 7.0,
            },
            {
                "account_code": "tonghuashun",
                "symbol": "600550",
                "asset_name": "保变电气",
                "market_value_cny": 0.0,
                "holding_pnl_cny": -4.0,
                "daily_pnl_cny": -4.0,
            },
        ],
    )

    assert len(merged) == 2
    assert merged[0]["symbol"] == "600487"
    assert merged[0]["holding_pnl_cny"] == pytest.approx(17.0)
    assert merged[0]["daily_pnl_cny"] == pytest.approx(8.0)
    assert merged[1]["symbol"] == "600550"
    assert merged[1]["holding_pnl_cny"] == pytest.approx(-4.0)


def test_realized_trade_merge_keeps_daily_when_daily_view_already_includes_realized() -> None:
    merged = _merge_realized_trade_positions(
        [
            {
                "account_code": "tonghuashun",
                "symbol": "600487",
                "asset_name": "Hengtong",
                "market_value_cny": 100.0,
                "holding_pnl_cny": 10.0,
                "daily_pnl_cny": 8.0,
                "daily_price_pnl_cny": 8.0,
                "daily_total_pnl_cny": 8.0,
                "daily_realized_trade_pnl_cny": 7.0,
                "daily_includes_realized_trade_pnl": True,
            }
        ],
        [
            {
                "account_code": "tonghuashun",
                "symbol": "600487",
                "asset_name": "Hengtong",
                "market_value_cny": 0.0,
                "holding_pnl_cny": 7.0,
                "daily_pnl_cny": 7.0,
                "daily_price_pnl_cny": 7.0,
                "daily_total_pnl_cny": 7.0,
                "realized_pnl_cny": 7.0,
            }
        ],
    )

    assert merged[0]["holding_pnl_cny"] == pytest.approx(17.0)
    assert merged[0]["daily_pnl_cny"] == pytest.approx(8.0)
    assert merged[0]["daily_total_pnl_cny"] == pytest.approx(8.0)
    assert merged[0]["realized_pnl_cny"] == pytest.approx(7.0)


def test_closed_zero_value_index_fund_uses_lookthrough_proxy_for_pnl_attribution() -> None:
    closed_name = "\u5efa\u4fe1\u7eb3\u65af\u8fbe\u514b100\u6307\u6570(QDII)A\u4eba\u6c11\u5e01"
    config = {
        "asset_mapping": [
            {
                "asset_name": "QQQ",
                "symbol": "QQQ",
                "aliases": [closed_name],
                "asset_class": "etf_us",
                "region": "United States",
                "currency": "USD",
                "industry": "\u79d1\u6280\u7efc\u5408",
                "theme": "Nasdaq100",
                "mapping_confidence": "HIGH",
            }
        ],
        "asset_lookthrough": [
            {
                "parent_symbol": "QQQ",
                "parent_asset_name": "QQQ",
                "lookthrough_type": "INDEX_PROXY",
                "confidence": "HIGH",
                "holdings": [
                    {
                        "underlying_name": "NVIDIA",
                        "underlying_symbol": "NVDA",
                        "underlying_weight": 0.7,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "Semiconductors",
                        "underlying_theme": "AI",
                    },
                    {
                        "underlying_name": "Walmart",
                        "underlying_symbol": "WMT",
                        "underlying_weight": 0.3,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "Consumer Staples",
                        "underlying_theme": "Consumer",
                    },
                ],
            }
        ],
    }
    payload = build_return_attribution_from_positions(
        [
            {
                "asset_name": closed_name,
                "symbol": closed_name,
                "asset_type": "fund",
                "currency": "CNY",
                "market_value_cny": 0.0,
                "holding_pnl_cny": -100.0,
            }
        ],
        config,
    )

    industry = {row["name"]: row for row in payload["dimensions"]["industry"]}
    assert industry["\u79d1\u6280TMT"]["pnl_cny"] == pytest.approx(-70.0)
    assert industry["\u5927\u6d88\u8d39"]["pnl_cny"] == pytest.approx(-30.0)
    assert UNMAPPED_INDUSTRY not in industry
    assert _row(payload["dimensions"]["underlying"], "NVIDIA")["pnl_cny"] == pytest.approx(-70.0)
    assert _row(payload["dimensions"]["underlying"], "Walmart")["pnl_cny"] == pytest.approx(-30.0)


def test_duplicate_symbol_positions_match_by_position_value_not_aggregated_symbol() -> None:
    payload = build_return_attribution_from_positions(
        [
            {
                "account_code": "acct",
                "asset_name": "红利基金",
                "symbol": "016440",
                "asset_type": "fund",
                "currency": "CNY",
                "market_value_cny": 100.0,
                "holding_pnl_cny": 10.0,
            },
            {
                "account_code": "acct",
                "asset_name": "红利基金",
                "symbol": "016440",
                "asset_type": "fund",
                "currency": "CNY",
                "market_value_cny": 300.0,
                "holding_pnl_cny": 30.0,
            },
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
    )

    row = _row(payload["dimensions"]["holding"], "红利基金")
    assert payload["total_market_value_cny"] == pytest.approx(400.0)
    assert payload["total_pnl_cny"] == pytest.approx(40.0)
    assert row["market_value_cny"] == pytest.approx(400.0)
    assert row["pnl_cny"] == pytest.approx(40.0)


def test_duplicate_symbol_positions_preserve_source_accounts() -> None:
    payload = build_return_attribution_from_positions(
        [
            {
                "account_code": "tencent_licaitong",
                "asset_name": "红利基金",
                "symbol": "016440",
                "asset_type": "fund",
                "currency": "CNY",
                "market_value_cny": 14480.46,
                "holding_pnl_cny": -519.54,
            },
            {
                "account_code": "ccb_yhs300_fund",
                "asset_name": "红利基金",
                "symbol": "016440",
                "asset_type": "fund",
                "currency": "CNY",
                "market_value_cny": 4970.27,
                "holding_pnl_cny": -29.73,
            },
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
    )

    row = _row(payload["dimensions"]["holding"], "红利基金")
    accounts = {detail["account"] for detail in row["source_details"]}
    assert accounts == {"tencent_licaitong", "ccb_yhs300_fund"}
    assert row["market_value_cny"] == pytest.approx(19450.73)
    assert row["pnl_cny"] == pytest.approx(-549.27)
