from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.lookthrough_state import _apply_industry_buckets, derive_lookthrough_payload  # noqa: E402


def test_unmapped_asset_master_bucket_can_be_remapped_from_leaf_label() -> None:
    units = [
        {
            "underlying_symbol": "688256",
            "underlying_industry": "半导体",
            "industry_bucket": "未穿透 / 未映射",
            "industry_bucket_source": "asset_master",
            "underlying_risk_bucket": "CN_EQUITY",
        }
    ]

    _apply_industry_buckets(units, industry_l1_by_symbol=None)

    assert units[0]["industry_bucket"] == "科技TMT"
    assert units[0]["industry_bucket_source"] == "industry_label"


def test_derive_lookthrough_payload_splits_index_proxy_and_residual() -> None:
    config = {
        "risk_buckets": {"US_GLOBAL_TECH": "美股全球科技"},
        "asset_mapping": [
            {
                "asset_name": "QQQ",
                "symbol": "QQQ",
                "asset_class": "etf_us",
                "risk_bucket": "US_GLOBAL_TECH",
                "region": "United States",
                "currency": "USD",
                "industry": "科技综合",
                "theme": "Nasdaq100",
                "style": "美股成长",
                "mapping_confidence": "HIGH",
            },
            {
                "asset_name": "NVIDIA",
                "symbol": "NVDA",
                "asset_class": "stock_us",
                "risk_bucket": "US_GLOBAL_TECH",
                "region": "United States",
                "currency": "USD",
                "industry": "半导体",
                "theme": "AI算力",
                "style": "美股成长",
                "mapping_confidence": "HIGH",
            },
        ],
        "asset_lookthrough": [
            {
                "parent_symbol": "QQQ",
                "parent_asset_name": "QQQ",
                "lookthrough_type": "INDEX_PROXY",
                "source_type": "ESTIMATED",
                "confidence": "MEDIUM",
                "as_of": "2026-06-01",
                "holdings": [
                    {
                        "underlying_name": "NVIDIA",
                        "underlying_symbol": "NVDA",
                        "underlying_weight": 0.6,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "半导体",
                        "underlying_theme": "AI算力",
                        "underlying_style": "美股成长",
                        "underlying_risk_bucket": "US_GLOBAL_TECH",
                    },
                    {
                        "underlying_name": "Microsoft",
                        "underlying_symbol": "MSFT",
                        "underlying_weight": 0.2,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "软件",
                        "underlying_theme": "AI软件",
                        "underlying_style": "美股成长",
                        "underlying_risk_bucket": "US_GLOBAL_TECH",
                    },
                ],
            }
        ],
    }
    positions = [
        {
            "asset_name": "QQQ",
            "symbol": "QQQ",
            "asset_type": "etf_us",
            "currency": "USD",
            "market_value_cny": 100.0,
            "as_of": date(2026, 6, 2),
        },
        {
            "asset_name": "NVIDIA",
            "symbol": "NVDA",
            "asset_type": "stock_us",
            "currency": "USD",
            "market_value_cny": 50.0,
            "as_of": date(2026, 6, 2),
        },
    ]

    payload = derive_lookthrough_payload(positions, config, as_of=date(2026, 6, 2))

    assert payload["total_market_value_cny"] == 150.0
    assert payload["gross_exposure_cny"] == 150.0
    assert payload["portfolio_nav_cny"] == 150.0
    assert payload["quality"]["coverage_ratio"] == (100.0 * 0.8 + 50.0) / 150.0
    assert payload["quality"]["top10_total_value_cny"] == 100.0
    assert payload["quality"]["top10_only_value_cny"] == 80.0
    assert payload["quality"]["proxy_residual_value_cny"] == 20.0
    assert payload["quality"]["covered_value_cny"] == 130.0
    assert payload["quality"]["uncovered_value_cny"] == 20.0
    assert sum(segment["value_cny"] for segment in payload["quality"]["segments"]) == 150.0
    qqq_quality = next(item for item in payload["quality"]["items"] if item["symbol"] == "QQQ")
    assert qqq_quality["quality_status"] == "INDEX_PROXY"
    assert qqq_quality["coverage_ratio"] == 0.8
    qqq_issue = next(item for item in payload["quality"]["low_confidence_assets"] if item["symbol"] == "QQQ")
    assert qqq_issue["covered_value_cny"] == 80.0
    assert qqq_issue["uncovered_value_cny"] == 20.0
    assert qqq_issue["uncovered_weight"] == 20.0 / 150.0

    risk_bucket = payload["exposures"]["risk_bucket"][0]
    assert risk_bucket["name"] == "US_GLOBAL_TECH"
    assert risk_bucket["display_name"] == "美股全球科技"
    assert risk_bucket["value_cny"] == 150.0
    assert "INDEX_PROXY" in risk_bucket["source_breakdown"]
    assert "PRODUCT_TAG" in risk_bucket["source_breakdown"]


def test_derive_lookthrough_payload_can_report_nav_separately_from_gross_exposure() -> None:
    payload = derive_lookthrough_payload(
        [
            {
                "asset_name": "Levered Stock",
                "symbol": "LEV",
                "asset_type": "stock_us",
                "currency": "USD",
                "market_value_cny": 150.0,
                "as_of": "2026-06-02",
            },
            {
                "asset_name": "Margin Loan",
                "symbol": "CASH_USD",
                "asset_type": "liability",
                "currency": "USD",
                "market_value_cny": -50.0,
                "as_of": "2026-06-02",
            },
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        as_of="2026-06-02",
        portfolio_nav_cny=100.0,
    )

    assert payload["portfolio_nav_cny"] == 100.0
    assert payload["gross_exposure_cny"] == 150.0
    assert payload["total_market_value_cny"] == 150.0


def test_derive_lookthrough_payload_warns_on_unmapped_asset() -> None:
    payload = derive_lookthrough_payload(
        [
            {
                "asset_name": "未知资产",
                "symbol": "UNKNOWN",
                "asset_type": "unknown",
                "currency": "CNY",
                "market_value_cny": 10.0,
                "as_of": "2026-06-02",
            }
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        as_of="2026-06-02",
    )

    assert payload["exposures"]["risk_bucket"][0]["name"] == "OTHER"
    assert payload["quality"]["items"][0]["quality_status"] == "MISSING"
    assert payload["data_warnings"]


def test_a_share_stock_fallback_is_direct_full_coverage() -> None:
    payload = derive_lookthrough_payload(
        [
            {
                "asset_name": "Test A Share",
                "symbol": "603986",
                "asset_type": "a_share_stock",
                "currency": "CNY",
                "market_value_cny": 30500.0,
                "as_of": "2026-06-05",
            }
        ],
        {"asset_mapping": [], "asset_lookthrough": []},
        as_of="2026-06-05",
        industry_l1_by_symbol={"603986": "鐢靛瓙"},
    )

    item = payload["quality"]["items"][0]
    assert item["lookthrough_type"] == "DIRECT"
    assert item["quality_status"] == "FULL"
    assert item["coverage_ratio"] == 1.0
    assert payload["quality"]["covered_value_cny"] == 30500.0
    assert payload["quality"]["product_tag_only_value_cny"] == 0.0
    assert not payload["data_warnings"]

    unit = payload["underlying_holdings"][0]
    assert unit["lookthroughType"] == "DIRECT"
    assert unit["underlyingWeightInFund"] == 1.0
    assert unit["effectiveMarketValue"] == 30500.0


def test_industry_bucket_prefers_sw_l1_and_falls_back_to_foreign_labels() -> None:
    config = {
        "asset_mapping": [
            {
                "asset_name": "平安银行",
                "symbol": "000001",
                "asset_class": "stock_cn",
                "risk_bucket": "CN_EQUITY",
                "region": "China",
                "currency": "CNY",
                "industry": "证券",
                "theme": "金融",
                "style": "A股",
                "mapping_confidence": "HIGH",
            },
            {
                "asset_name": "贵州茅台",
                "symbol": "600519",
                "asset_class": "stock_cn",
                "risk_bucket": "CN_EQUITY",
                "region": "China",
                "currency": "CNY",
                "industry": "软件",
                "theme": "消费",
                "style": "A股",
                "mapping_confidence": "HIGH",
            },
            {
                "asset_name": "Apple",
                "symbol": "AAPL",
                "asset_class": "stock_us",
                "risk_bucket": "US_GLOBAL_TECH",
                "region": "United States",
                "currency": "USD",
                "industry": "Consumer electronics",
                "theme": "Devices",
                "style": "US growth",
                "mapping_confidence": "HIGH",
            },
            {
                "asset_name": "Unknown Offshore",
                "symbol": "ZZZZ",
                "asset_class": "stock_us",
                "risk_bucket": "OTHER",
                "region": "United States",
                "currency": "USD",
                "industry": "Unclassified niche",
                "theme": "Unknown",
                "style": "Unknown",
                "mapping_confidence": "HIGH",
            },
        ],
        "asset_lookthrough": [],
    }
    positions = [
        {"asset_name": "平安银行", "symbol": "000001", "asset_type": "stock_cn", "market_value_cny": 10.0, "as_of": "2026-06-02"},
        {"asset_name": "贵州茅台", "symbol": "600519", "asset_type": "stock_cn", "market_value_cny": 20.0, "as_of": "2026-06-02"},
        {"asset_name": "Apple", "symbol": "AAPL", "asset_type": "stock_us", "market_value_cny": 30.0, "as_of": "2026-06-02"},
        {"asset_name": "Unknown Offshore", "symbol": "ZZZZ", "asset_type": "stock_us", "market_value_cny": 5.0, "as_of": "2026-06-02"},
    ]

    payload = derive_lookthrough_payload(
        positions,
        config,
        as_of="2026-06-02",
        industry_l1_by_symbol={"000001": "银行", "600519": "食品饮料"},
    )

    industry = {row["name"]: row for row in payload["exposures"]["industry"]}
    assert industry["金融地产"]["value_cny"] == 10.0
    assert industry["大消费"]["value_cny"] == 20.0
    assert industry["科技TMT"]["value_cny"] == 30.0
    assert industry["未穿透 / 未映射"]["value_cny"] == 5.0

    detail = industry["大消费"]["source_details"][0]
    assert detail["sw_l1"] == "食品饮料"
    assert detail["industry_bucket_source"] == "sw_l1"


def test_industry_bucket_keeps_unknown_sw_l1_unmapped() -> None:
    config = {
        "asset_mapping": [
            {
                "asset_name": "综合行业股票",
                "symbol": "600000",
                "asset_class": "stock_cn",
                "risk_bucket": "CN_EQUITY",
                "region": "China",
                "currency": "CNY",
                "industry": "银行",
                "theme": "金融",
                "style": "A股",
                "mapping_confidence": "HIGH",
            },
        ],
        "asset_lookthrough": [],
    }

    payload = derive_lookthrough_payload(
        [{"asset_name": "综合行业股票", "symbol": "600000", "asset_type": "stock_cn", "market_value_cny": 10.0, "as_of": "2026-06-02"}],
        config,
        as_of="2026-06-02",
        industry_l1_by_symbol={"600000": "综合"},
    )

    assert payload["exposures"]["industry"][0]["name"] == "未穿透 / 未映射"
    assert payload["exposures"]["industry"][0]["source_details"][0]["sw_l1"] == "综合"


def test_industry_and_theme_exposures_exclude_cash_like_units() -> None:
    config = {
        "asset_mapping": [
            {
                "asset_name": "NVIDIA",
                "symbol": "NVDA",
                "asset_class": "stock_us",
                "risk_bucket": "US_GLOBAL_TECH",
                "region": "United States",
                "currency": "USD",
                "industry": "Semiconductors",
                "theme": "AI算力",
                "style": "US growth",
                "mapping_confidence": "HIGH",
            },
            {
                "asset_name": "现金账户",
                "symbol": "CASH",
                "asset_class": "cash",
                "risk_bucket": "CASH_SHORT_DURATION",
                "region": "China",
                "currency": "CNY",
                "industry": "现金/短久期",
                "theme": "现金管理",
                "style": "现金",
                "mapping_confidence": "HIGH",
            },
        ],
        "asset_lookthrough": [],
    }
    positions = [
        {"asset_name": "NVIDIA", "symbol": "NVDA", "asset_type": "stock_us", "market_value_cny": 100.0, "as_of": "2026-06-02"},
        {"asset_name": "现金账户", "symbol": "CASH", "asset_type": "cash", "market_value_cny": 50.0, "as_of": "2026-06-02"},
    ]

    payload = derive_lookthrough_payload(positions, config, as_of="2026-06-02")

    risk = {row["name"]: row for row in payload["exposures"]["risk_bucket"]}
    assert risk["CASH_SHORT_DURATION"]["value_cny"] == 50.0
    assert risk["US_GLOBAL_TECH"]["value_cny"] == 100.0

    industry = payload["exposures"]["industry"]
    assert [row["name"] for row in industry] == ["科技TMT"]
    assert industry[0]["value_cny"] == 100.0
    assert industry[0]["weight"] == 1.0

    theme = {row["name"]: row for row in payload["exposures"]["theme"]}
    assert set(theme) == {"AI / 算力", "半导体"}
    assert theme["AI / 算力"]["value_cny"] == 100.0
    assert theme["AI / 算力"]["weight"] == pytest.approx(100.0 / 150.0)
    assert theme["半导体"]["value_cny"] == 100.0
    assert theme["半导体"]["weight"] == pytest.approx(100.0 / 150.0)
