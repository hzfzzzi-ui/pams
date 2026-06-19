from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.exposure_engine import (  # noqa: E402
    build_exposure_analysis_payload,
    calculateIndustryExposure,
    calculateMarketExposure,
    calculateThemeExposure,
    normalizeHoldings,
    resolveIndustryBucket,
)


def _theme_names(holding: dict) -> set[str]:
    return {item["themeName"] for item in holding.get("themeExposures", [])}


def _by_symbol(holdings: list[dict]) -> dict[str, dict]:
    return {item["symbol"]: item for item in holdings}


@pytest.mark.parametrize(
    ("leaf", "expected"),
    [
        ("半导体", "科技TMT"),
        ("元器件", "科技TMT"),
        ("火力发电", "稳定公用"),
        ("水力发电", "稳定公用"),
        ("港口", "稳定公用"),
        ("路桥", "稳定公用"),
        ("全国地产", "金融地产"),
        ("中成药", "医药医疗"),
        ("化学制药", "医药医疗"),
        ("农药化肥", "周期资源"),
        ("普钢", "周期资源"),
        ("家居用品", "大消费"),
    ],
)
def test_cn_industry_leaf_labels_map_to_bucket(leaf: str, expected: str) -> None:
    assert resolveIndustryBucket({"asset_type": "stock_cn", "industry": leaf})["bucket"] == expected


def test_explicit_unmapped_bucket_does_not_block_leaf_mapping() -> None:
    result = resolveIndustryBucket(
        {
            "asset_type": "stock_cn",
            "industry_bucket": "未穿透 / 未映射",
            "industry": "半导体",
        }
    )

    assert result["bucket"] == "科技TMT"
    assert result["reason"] == "industry_label"


def test_asset_type_isolated_from_industry_and_theme() -> None:
    holdings = normalizeHoldings(
        [
            {"asset_name": "华为ESOP", "symbol": "HUAWEI_ESOP", "asset_type": "private_equity", "market_value_cny": 100.0, "currency": "CNY"},
            {"asset_name": "SGOV", "symbol": "SGOV", "asset_type": "etf", "market_value_cny": 80.0, "currency": "USD"},
            {"asset_name": "现金", "symbol": "CASH", "asset_type": "cash", "market_value_cny": 50.0, "currency": "CNY"},
            {"asset_name": "沪深300基金", "symbol": "510300", "asset_type": "fund", "market_value_cny": 70.0, "currency": "CNY"},
        ]
    )
    by_symbol = _by_symbol(holdings)

    assert by_symbol["HUAWEI_ESOP"]["assetType"] == "非上市股权"
    assert by_symbol["HUAWEI_ESOP"]["industryBucket"] is None
    assert by_symbol["HUAWEI_ESOP"]["themeExposures"] == []
    assert by_symbol["SGOV"]["assetType"] == "债券 / 货币基金"
    assert by_symbol["SGOV"]["industryBucket"] is None
    assert by_symbol["SGOV"]["themeExposures"] == []
    assert by_symbol["510300"]["assetType"] == "ETF / 基金"
    assert by_symbol["510300"]["industryBucket"] == "未穿透 / 未映射"

    industry_names = {row["name"] for row in calculateIndustryExposure(holdings, [])}
    assert industry_names == {"未穿透 / 未映射"}
    assert {"非上市股权", "债券 / 货币基金", "现金"}.isdisjoint(industry_names)
    assert calculateThemeExposure(holdings, []) == []
    market_names = {row["name"] for row in calculateMarketExposure(holdings, [])}
    assert {"现金", "基金", "货币基金 / 债券"}.isdisjoint(market_names)


def test_market_exposure_excludes_asset_type_only_buckets() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {"asset_name": "现金", "symbol": "CASH_CNY", "asset_type": "cash", "market_value_cny": 50.0, "currency": "CNY"},
            {"asset_name": "沪深300基金", "symbol": "510300", "asset_type": "fund", "market_value_cny": 70.0, "currency": "CNY"},
            {"asset_name": "SGOV", "symbol": "SGOV", "asset_type": "short_duration_usd", "market_value_cny": 40.0, "currency": "USD"},
            {"asset_name": "贵州茅台", "symbol": "600519", "asset_type": "stock_cn", "market_value_cny": 100.0, "currency": "CNY"},
            {"asset_name": "建行黄金积存金", "symbol": "CCB_GOLD_ACCUM", "asset_type": "gold", "market_value_cny": 80.0, "currency": "CNY"},
        ],
        units=[],
        quality={},
        total_value=340.0,
    )

    asset_types = {row["name"] for row in payload["exposures"]["asset_type"]}
    assert {"现金", "ETF / 基金", "债券 / 货币基金"}.issubset(asset_types)
    market = {row["name"]: row for row in payload["exposures"]["market"]}
    assert {"现金", "基金", "货币基金 / 债券"}.isdisjoint(market)
    assert market["A股"]["value_cny"] == pytest.approx(100.0)
    assert market["黄金 / 商品"]["value_cny"] == pytest.approx(80.0)


def test_direct_non_standard_assets_have_explicit_market_buckets() -> None:
    holdings = normalizeHoldings(
        [
            {"asset_name": "Samsung Electronics", "symbol": "005930.KS", "asset_type": "stock_kr", "market_value_cny": 100.0, "currency": "KRW"},
            {"asset_name": "Gold", "symbol": "GOLD", "asset_type": "gold", "market_value_cny": 80.0, "currency": "CNY"},
            {"asset_name": "Huawei ESOP", "symbol": "HUAWEI_ESOP", "asset_type": "private_equity", "market_value_cny": 60.0, "currency": "CNY"},
            {"asset_name": "SGOV", "symbol": "SGOV", "asset_type": "short_duration_usd", "market_value_cny": 40.0, "currency": "USD"},
            {"asset_name": "NETFLIX, INC.", "symbol": "NFLX", "asset_type": "stock_us", "market_value_cny": 30.0, "currency": "USD"},
        ]
    )
    by_symbol = _by_symbol(holdings)

    assert by_symbol["005930.KS"]["market"] == "\u97e9\u80a1"
    assert by_symbol["GOLD"]["market"] == "\u9ec4\u91d1 / \u5546\u54c1"
    assert by_symbol["HUAWEI_ESOP"]["market"] == "\u975e\u4e0a\u5e02 / \u5185\u90e8"
    assert by_symbol["SGOV"]["market"] == "\u8d27\u5e01\u57fa\u91d1 / \u503a\u5238"
    assert by_symbol["NFLX"]["assetType"] == "\u7f8e\u80a1\u4e2a\u80a1"
    assert by_symbol["NFLX"]["market"] == "\u7f8e\u80a1"


@pytest.mark.parametrize(
    ("symbol", "asset_name", "asset_type", "sw_l1", "industry", "expected_bucket", "expected_themes"),
    [
        ("601899", "紫金矿业", "stock_cn", "有色金属", "", "周期资源", {"贵金属与铜", "黄金避险"}),
        ("000630", "铜陵有色", "stock_cn", "有色金属", "", "周期资源", {"贵金属与铜"}),
        ("NVDA", "NVIDIA", "stock_us", "", "Semiconductors", "科技TMT", {"AI / 算力", "半导体"}),
        ("MSFT", "MICROSOFT", "stock_us", "", "Software", "科技TMT", {"AI / 算力", "云计算 / 软件"}),
        ("GOOGL", "ALPHABET", "stock_us", "", "Communication Services", "科技TMT", {"AI / 算力", "互联网平台", "云计算 / 软件"}),
        ("AMZN", "AMAZON", "stock_us", "", "Consumer Discretionary", "大消费", {"AI / 算力", "互联网平台", "云计算 / 软件"}),
        ("MU", "MICRON", "stock_us", "", "Semiconductors", "科技TMT", {"AI / 算力", "半导体"}),
        ("MRVL", "MARVELL", "stock_us", "", "Semiconductors", "科技TMT", {"AI / 算力", "半导体"}),
        ("002594", "比亚迪", "stock_cn", "汽车", "", "高端制造", {"新能源车", "出海链"}),
        ("600036", "招商银行", "stock_cn", "银行", "", "金融地产", {"金融顺周期", "高股息 / 红利"}),
        ("600941", "中国移动", "stock_cn", "通信", "", "科技TMT", {"高股息 / 红利", "央国企 / 中特估", "通信基础设施"}),
        ("603816", "顾家家居", "stock_cn", "轻工制造", "", "大消费", set()),
        ("600489", "中金黄金", "stock_cn", "", "", "周期资源", {"黄金避险"}),
        ("FTNT", "FORTINET, INC.", "stock_us", "", "Nasdaq-100 constituent", "科技TMT", set()),
    ],
)
def test_core_industry_and_theme_contracts(
    symbol: str,
    asset_name: str,
    asset_type: str,
    sw_l1: str,
    industry: str,
    expected_bucket: str,
    expected_themes: set[str],
) -> None:
    holding = normalizeHoldings(
        [
            {
                "asset_name": asset_name,
                "symbol": symbol,
                "asset_type": asset_type,
                "sw_l1": sw_l1,
                "industry": industry,
                "market_value_cny": 100.0,
                "currency": "USD" if asset_type == "stock_us" else "CNY",
            }
        ]
    )[0]

    assert holding["industryBucket"] == expected_bucket
    assert expected_themes.issubset(_theme_names(holding))


def test_index_membership_stays_out_of_industry_and_theme() -> None:
    holding = normalizeHoldings(
        [
            {
                "asset_name": "NVIDIA",
                "symbol": "NVDA",
                "asset_type": "stock_us",
                "industry": "Semiconductors",
                "theme": "Nasdaq-100 constituent",
                "market_value_cny": 100.0,
                "currency": "USD",
            }
        ]
    )[0]

    assert holding["industryBucket"] == "科技TMT"
    assert "Nasdaq-100 constituent" in holding["indexMembershipTags"]
    assert "Nasdaq-100 constituent" not in _theme_names(holding)


def test_overseas_region_and_index_proxy_underlying_mapping_contracts() -> None:
    holdings = normalizeHoldings(
        [
            {
                "asset_name": "005930.KS - SAMSUNG ELECTRONICS CO LTD",
                "symbol": "005930.KS",
                "asset_type": "stock_kr",
                "market_value_cny": 100.0,
                "currency": "KRW",
                "region": "Korea",
                "industry": "Semiconductors",
            },
            {
                "asset_name": "3690 - MEITUAN-CLASS B",
                "symbol": "3690",
                "asset_type": "stock_hk",
                "market_value_cny": 100.0,
                "currency": "HKD",
                "region": "Hong Kong",
                "industry": "互联网平台",
            },
            {
                "asset_name": "QQQ Component",
                "symbol": "WMT",
                "asset_type": "stock_us",
                "market_value_cny": 100.0,
                "currency": "USD",
                "industry": "Nasdaq-100 constituent",
            },
        ]
    )
    by_symbol = _by_symbol(holdings)

    assert by_symbol["005930.KS"]["region"] == "韩国"
    assert by_symbol["005930.KS"]["currency"] == "KRW"
    assert by_symbol["005930.KS"]["industryBucket"] == "科技TMT"
    assert by_symbol["3690"]["market"] == "港股"
    assert by_symbol["3690"]["region"] == "中国大陆"
    assert by_symbol["3690"]["industryBucket"] == "大消费"
    assert by_symbol["WMT"]["industryBucket"] == "大消费"
    assert "Nasdaq-100 constituent" in by_symbol["WMT"]["indexMembershipTags"]


def test_european_sek_holding_maps_to_sweden_region_and_currency() -> None:
    holdings = normalizeHoldings(
        [
            {
                "asset_name": "SIVE - SIVERS SEMICONDUCTORS AB",
                "symbol": "SIVE",
                "asset_type": "stock_eu",
                "market_value_cny": 100.0,
                "currency": "SEK",
                "region": "Sweden",
                "industry": "Semiconductors",
            },
            {
                "asset_name": "Cash SEK",
                "symbol": "CASH_SEK",
                "asset_type": "cash",
                "market_value_cny": 10.0,
                "currency": "SEK",
            },
        ]
    )
    by_symbol = _by_symbol(holdings)

    assert by_symbol["SIVE"]["assetType"] == "欧股个股"
    assert by_symbol["SIVE"]["market"] == "欧股"
    assert by_symbol["SIVE"]["region"] == "瑞典"
    assert by_symbol["SIVE"]["currency"] == "SEK"
    assert by_symbol["SIVE"]["industryBucket"] == "科技TMT"
    assert by_symbol["CASH_SEK"]["assetType"] == "现金"
    assert by_symbol["CASH_SEK"]["region"] == "瑞典"
    assert by_symbol["CASH_SEK"]["currency"] == "SEK"

    market = {row["name"]: row for row in calculateMarketExposure(holdings, [])}
    assert market["欧股"]["value_cny"] == pytest.approx(100.0)


def test_fund_underlying_prefers_underlying_region_currency_and_symbol_market() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {
                "asset_name": "国内科技基金",
                "symbol": "FUND_CN",
                "asset_type": "fund",
                "market_value_cny": 1000.0,
                "currency": "CNY",
            }
        ],
        units=[
            {
                "holding_name": "国内科技基金",
                "holding_symbol": "FUND_CN",
                "holding_asset_class": "fund",
                "value_cny": 600.0,
                "underlying_weight": 0.6,
                "underlying_name": "寒武纪",
                "underlying_symbol": "688256",
                "underlying_region": "China",
                "underlying_currency": "CNY",
                "underlying_industry": "电子",
                "lookthrough_type": "INDEX_PROXY",
                "confidence": "MEDIUM",
            },
            {
                "holding_name": "国内科技基金",
                "holding_symbol": "FUND_CN",
                "holding_asset_class": "fund",
                "value_cny": 400.0,
                "underlying_weight": 0.4,
                "underlying_name": "Walmart",
                "underlying_symbol": "WMT",
                "underlying_region": "United States",
                "underlying_currency": "USD",
                "underlying_industry": "Nasdaq-100 constituent",
                "lookthrough_type": "INDEX_PROXY",
                "confidence": "MEDIUM",
            },
        ],
        quality={},
        total_value=1000.0,
    )
    underlyings = {row["symbol"]: row for row in payload["exposures"]["underlying_exposure"]}

    assert underlyings["688256"]["asset_type"] == "A股个股"
    assert underlyings["688256"]["market"] == "A股"
    assert underlyings["688256"]["region"] == "中国大陆"
    assert underlyings["688256"]["currency"] == "CNY"
    assert underlyings["WMT"]["industry_bucket"] == "大消费"
    market = {row["name"]: row for row in payload["exposures"]["market"]}
    assert market["A股"]["value_cny"] == pytest.approx(600.0)
    assert market["美股"]["value_cny"] == pytest.approx(400.0)
    assert "基金" not in market


def test_fund_without_lookthrough_is_unmapped_not_name_guessed() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {"asset_name": "沪深300基金", "symbol": "510300", "asset_type": "fund", "market_value_cny": 100.0, "currency": "CNY"},
            {"asset_name": "纳指100基金", "symbol": "QQQ_PROXY", "asset_type": "fund", "market_value_cny": 100.0, "currency": "CNY", "theme": "Nasdaq-100 constituent"},
            {"asset_name": "QQQ - INVESCO QQQ TRUST SERIES 1", "symbol": "QQQ", "asset_type": "index_proxy", "market_value_cny": 100.0, "currency": "USD"},
        ],
        units=[],
        quality={},
        total_value=300.0,
    )

    normalized = {item["symbol"]: item for item in payload["normalized_holdings"]}
    assert normalized["510300"]["assetType"] == "ETF / 基金"
    assert normalized["QQQ_PROXY"]["assetType"] == "ETF / 基金"
    assert normalized["QQQ"]["assetType"] == "ETF / 基金"
    assert {row["name"] for row in payload["exposures"]["industry"]} == {"未穿透 / 未映射"}
    assert payload["exposures"]["theme"] == []
    assert "基金" not in {row["name"] for row in payload["exposures"]["market"]}
    assert "Nasdaq-100 constituent" in normalized["QQQ_PROXY"]["indexMembershipTags"]
    assert "Nasdaq-100 constituent" in normalized["QQQ"]["indexMembershipTags"]


def test_lookthrough_parent_keeps_account_and_residual_without_direct_product_duplication() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {
                "asset_name": "Alpha Mixed Fund",
                "symbol": "001412",
                "asset_type": "stock_cn",
                "market_value_cny": 100.0,
                "currency": "CNY",
                "account_name": "Broker A",
            }
        ],
        units=[
            {
                "holding_name": "Alpha Mixed Fund",
                "holding_symbol": "001412.OF",
                "holding_asset_class": "fund",
                "account": "Broker A",
                "value_cny": 70.0,
                "underlying_weight": 0.7,
                "underlying_name": "Alpha Component",
                "underlying_symbol": "ALPHA",
                "underlying_currency": "USD",
                "underlying_asset_class": "stock_us",
                "underlying_industry": "Software",
                "underlying_theme": "AI",
                "lookthrough_type": "TOP10",
                "confidence": "MEDIUM",
            },
            {
                "holding_name": "Alpha Mixed Fund",
                "holding_symbol": "001412.OF",
                "holding_asset_class": "fund",
                "account": "Broker A",
                "value_cny": 30.0,
                "underlying_weight": 0.3,
                "underlying_name": "Alpha Mixed Fund",
                "underlying_symbol": "001412",
                "underlying_currency": "CNY",
                "underlying_asset_class": "fund",
                "underlying_industry": "Balanced fund",
                "underlying_theme": "Residual",
                "lookthrough_type": "PRODUCT_TAG",
                "confidence": "LOW",
            },
        ],
        quality={},
        total_value=100.0,
    )

    parent = payload["normalized_holdings"][0]
    assert parent["assetType"] == "ETF / 基金"
    assert parent["lookthroughStatus"] == "lookthrough_available"
    assert parent["account"] == "Broker A"

    rows = payload["exposures"]["underlying_exposure"]
    assert not any(row["name"] == "Alpha Mixed Fund" and row["value_cny"] == 100.0 for row in rows)

    component = next(row for row in rows if row["name"] == "Alpha Component")
    residual = next(row for row in rows if row["name"] == "未穿透残差")
    assert component["account"] == "Broker A"
    assert component["source_details"][0]["account"] == "Broker A"
    assert residual["value_cny"] == pytest.approx(30.0)
    assert residual["account"] == "Broker A"
    assert residual["source_details"][0]["lookthrough_type"] == "PRODUCT_TAG"
    assert residual["industry_bucket"] == "未穿透 / 未映射"
    assert residual["theme_names"] == []

    industry_detail_accounts = [
        detail["account"]
        for row in payload["exposures"]["industry"]
        for detail in row["source_details"]
    ]
    assert "Broker A" in industry_detail_accounts
    assert "" not in industry_detail_accounts


def test_direct_holding_uses_mapped_underlying_asset_in_underlying_exposure() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {
                "asset_name": "US67066G1040 - NVIDIA CORP",
                "symbol": "US67066G1040",
                "asset_type": "stock_us",
                "market_value_cny": 100.0,
                "currency": "USD",
                "account_name": "IBKR",
            }
        ],
        units=[
            {
                "holding_name": "US67066G1040 - NVIDIA CORP",
                "holding_symbol": "US67066G1040",
                "holding_asset_class": "stock_us",
                "account": "IBKR",
                "value_cny": 100.0,
                "underlying_weight": 1.0,
                "underlying_name": "NVIDIA",
                "underlying_symbol": "NVDA",
                "underlying_currency": "USD",
                "underlying_asset_class": "stock_us",
                "underlying_industry": "Semiconductors",
                "underlying_theme": "AI",
                "lookthrough_type": "DIRECT",
                "confidence": "HIGH",
            }
        ],
        quality={},
        total_value=100.0,
    )

    rows = payload["exposures"]["underlying_exposure"]
    assert len(rows) == 1
    assert rows[0]["name"] == "NVIDIA"
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["source"] == "直接持仓"
    assert rows[0]["source_asset_name"] == "US67066G1040 - NVIDIA CORP"
    assert rows[0]["account"] == "IBKR"
    assert rows[0]["source_details"][0]["source_symbol"] == "US67066G1040"
    assert rows[0]["source_details"][0]["underlying_symbol"] == "NVDA"

    industry_total = sum(row["value_cny"] for row in payload["exposures"]["industry"])
    assert industry_total == pytest.approx(100.0)


def test_ai_compute_theme_exposes_second_level_chain() -> None:
    payload = build_exposure_analysis_payload(
        positions=[
            {"asset_name": "NVIDIA", "symbol": "NVDA", "asset_type": "stock_us", "market_value_cny": 100.0, "currency": "USD"},
            {"asset_name": "MICRON", "symbol": "MU", "asset_type": "stock_us", "market_value_cny": 100.0, "currency": "USD"},
            {"asset_name": "MARVELL", "symbol": "MRVL", "asset_type": "stock_us", "market_value_cny": 100.0, "currency": "USD"},
        ],
        units=[],
        quality={},
        total_value=300.0,
    )
    ai_row = next(row for row in payload["exposures"]["theme"] if row["name"] == "AI / 算力")
    sub_names = {row["name"] for row in ai_row["sub_themes"]}

    assert {"GPU / AI 加速器", "网络 / 交换芯片", "存储 / HBM", "光模块 / CPO"}.issubset(sub_names)
    assert ai_row["weight"] == pytest.approx((100.0 + 100.0 + 80.0) / 300.0)
    assert all(row["status"] == "待计算" for row in ai_row["sub_themes"])


def test_source_details_keep_direct_and_fund_underlyings_separate() -> None:
    real_units = [
        {
            "holding_name": "测试科技基金",
            "holding_symbol": "FUND_TOP10",
            "holding_asset_class": "fund",
            "underlying_name": f"Tech {idx}",
            "underlying_symbol": f"T{idx}",
            "underlying_weight": 0.01,
            "value_cny": 10.0,
            "underlying_asset_class": "stock_us",
            "underlying_industry": "Information Technology",
            "underlying_currency": "USD",
            "lookthrough_type": "TOP10",
            "confidence": "MEDIUM",
        }
        for idx in range(11)
    ]
    payload = build_exposure_analysis_payload(
        positions=[
            {"asset_name": "NVIDIA", "symbol": "NVDA", "asset_type": "stock_us", "market_value_cny": 100.0, "currency": "USD"},
            {"asset_name": "测试科技基金", "symbol": "FUND_TOP10", "asset_type": "fund", "market_value_cny": 1000.0, "currency": "CNY"},
        ],
        units=[
            *real_units,
            {
                "holding_name": "测试科技基金",
                "holding_symbol": "FUND_TOP10",
                "holding_asset_class": "fund",
                "underlying_name": "测试科技基金",
                "underlying_symbol": "FUND_TOP10",
                "underlying_weight": 0.89,
                "value_cny": 890.0,
                "underlying_asset_class": "fund",
                "underlying_industry": "科技主题",
                "underlying_theme": "AI算力",
                "underlying_currency": "CNY",
                "lookthrough_type": "PRODUCT_TAG",
                "confidence": "LOW",
            },
        ],
        quality={},
        total_value=1100.0,
    )

    industry = {row["name"]: row for row in payload["exposures"]["industry"]}
    tech_details = industry["科技TMT"]["source_details"]
    direct_detail = next(item for item in tech_details if item["source_asset_name"] == "NVIDIA")
    fund_detail = next(item for item in tech_details if item["source_asset_name"] == "测试科技基金")

    assert direct_detail["underlying_name"] == ""
    assert len(fund_detail["underlying_top"]) == 10
    assert fund_detail["underlying_names"][0].startswith("Tech")
    assert industry["未穿透 / 未映射"]["value_cny"] == pytest.approx(890.0)
    assert payload["exposures"]["theme"]
    assert all(row["name"] != "科技主题" for row in payload["exposures"]["theme"])
