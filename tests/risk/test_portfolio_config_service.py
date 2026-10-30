import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.risk.portfolio_config import _config_market, build_portfolio_config_payload_from_inputs  # noqa: E402
from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload_from_inputs  # noqa: E402


def test_config_market_prefers_korean_region_currency_over_legacy_us_asset_type() -> None:
    assert _config_market(
        {
            "assetType": "美股个股",
            "symbol": "005930.KS",
            "region": "韩国",
            "currency": "KRW",
        }
    ) == "韩股"


def test_portfolio_config_rules_are_derived_from_risk_and_lookthrough() -> None:
    risk = {
        "as_of": "2026-06-15",
        "summary": {
            "total_net_worth_cny": 1_000_000,
            "negative_cash_ratio": 0.14,
            "negative_cash_cny": 50_000,
        },
        "positions": [
            {
                "asset_id": "HUAWEI_ESOP",
                "asset_name": "华为 ESOP",
                "asset_type": "esop",
                "position_value_cny": 300_000,
                "position_weight": 0.30,
            },
            {
                "asset_id": "601899",
                "asset_name": "紫金矿业",
                "asset_type": "a_share_stock",
                "position_value_cny": 120_000,
                "position_weight": 0.12,
            },
            {
                "asset_id": "QQQ",
                "asset_name": "QQQ - INVESCO QQQ TRUST SERIES 1",
                "asset_type": "us_stock",
                "position_value_cny": 90_000,
                "position_weight": 0.09,
            },
        ],
        "holdings": [
            {
                "account_code": "ccb_7332",
                "account_name": "建行7332国内储蓄卡",
                "asset_id": "CASH_CCB",
                "asset_name": "建行7332国内储蓄卡",
                "asset_type": "cash",
                "currency": "CNY",
                "market_value_cny": 70_000,
            },
            {
                "account_code": "ibkr",
                "account_name": "IBKR",
                "asset_id": "CASH_USD",
                "symbol": "CASH_USD",
                "asset_name": "Cash USD",
                "asset_type": "cash",
                "currency": "USD",
                "market_value_cny": -5_000,
            },
        ],
    }
    lookthrough = {
        "as_of": "2026-06-15",
        "total_market_value_cny": 1_000_000,
        "exposures": {
            "asset_type": [
                {"name": "现金", "weight": 0.02, "value_cny": 20_000, "main_sources": [{"name": "现金账户", "value_cny": 20_000}]},
                {
                    "name": "美股个股",
                    "weight": 0.22,
                    "value_cny": 220_000,
                    "main_sources": [
                        {"name": "MU - MICRON TECHNOLOGY INC", "value_cny": 140_000},
                        {"name": "NVDA - NVIDIA CORP", "value_cny": 80_000},
                    ],
                },
                {"name": "非上市股权", "weight": 0.30, "value_cny": 300_000},
                {"name": "A股个股", "weight": 0.18, "value_cny": 180_000},
                {"name": "ETF / 基金", "weight": 0.09, "value_cny": 90_000},
                {"name": "黄金 / 商品", "weight": 0.02, "value_cny": 20_000},
            ],
            "theme": [
                {"name": "半导体", "weight": 0.28, "value_cny": 280_000},
                {"name": "AI / 算力", "weight": 0.24, "value_cny": 240_000},
                {"name": "贵金属与铜", "weight": 0.16, "value_cny": 160_000},
            ],
            "market": [
                {
                    "name": "A股",
                    "weight": 0.18,
                    "value_cny": 180_000,
                    "main_sources": [{"name": "紫金矿业", "value_cny": 120_000}],
                },
                {"name": "港股", "weight": 0.03, "value_cny": 30_000},
                {"name": "美股", "weight": 0.09, "value_cny": 90_000},
                {"name": "韩股", "weight": 0.04, "value_cny": 40_000},
            ],
        },
        "normalized_holdings": [
            {
                "asset_id": "QQQ",
                "asset_name": "QQQ - INVESCO QQQ TRUST SERIES 1",
                "market_value_cny": 90_000,
            },
            {
                "id": "SPCX",
                "symbol": "SPCX",
                "name": "SPCX - SPACE EXPLORATION TECHN-CL A",
                "assetType": "美股个股",
                "market": "美股",
                "currency": "USD",
                "marketValue": 10_000,
            },
        ],
        "underlying_holdings": [
            {
                "parentHoldingId": "HUAWEI_ESOP",
                "parentName": "华为 ESOP",
                "underlyingSymbol": "HUAWEI_ESOP",
                "underlyingName": "华为 ESOP",
                "effectiveMarketValue": 300_000,
                "assetType": "非上市股权",
                "market": "非上市 / 内部",
                "region": "中国大陆",
                "currency": "CNY",
                "lookthroughType": "DIRECT",
            },
            {
                "parentHoldingId": "MU",
                "parentName": "MU - MICRON TECHNOLOGY INC",
                "underlyingSymbol": "MU",
                "underlyingName": "Micron Technology",
                "effectiveMarketValue": 140_000,
                "assetType": "美股个股",
                "market": "美股",
                "currency": "USD",
                "lookthroughType": "DIRECT",
            },
            {
                "parentHoldingId": "NVDA",
                "parentName": "NVDA - NVIDIA CORP",
                "underlyingSymbol": "NVDA",
                "underlyingName": "NVIDIA",
                "effectiveMarketValue": 80_000,
                "assetType": "美股个股",
                "market": "美股",
                "currency": "USD",
                "lookthroughType": "DIRECT",
            },
            {
                "parentHoldingId": "QQQ",
                "parentName": "QQQ - INVESCO QQQ TRUST SERIES 1",
                "underlyingSymbol": "QQQ",
                "underlyingName": "QQQ",
                "effectiveMarketValue": 90_000,
                "assetType": "美股个股",
                "market": "美股",
                "currency": "USD",
                "lookthroughType": "INDEX_PROXY",
            },
            {
                "parentHoldingId": "601899",
                "parentName": "紫金矿业",
                "underlyingSymbol": "601899",
                "underlyingName": "紫金矿业",
                "effectiveMarketValue": 120_000,
                "assetType": "A股个股",
                "market": "A股",
                "currency": "CNY",
                "lookthroughType": "DIRECT",
            },
            {
                "parentHoldingId": "3690",
                "parentName": "3690 - MEITUAN-CLASS B",
                "underlyingSymbol": "3690",
                "underlyingName": "Meituan",
                "effectiveMarketValue": 30_000,
                "assetType": "港股个股",
                "market": "港股",
                "currency": "HKD",
                "lookthroughType": "DIRECT",
            },
            {
                "parentHoldingId": "110020",
                "parentName": "易沪深300",
                "underlyingSymbol": "",
                "underlyingName": "未穿透残差",
                "effectiveMarketValue": 90_000,
                "assetType": "ETF / 基金",
                "market": "基金",
                "region": "中国大陆",
                "currency": "CNY",
                "lookthroughType": "PRODUCT_TAG",
            },
        ],
    }

    payload = build_portfolio_config_payload_from_inputs(
        risk,
        lookthrough,
        asset_label_lookup={
            "3690": {"symbol": "03690.HK", "display_name": "美团-W"},
            "3690 - meituan-class b": {"symbol": "03690.HK", "display_name": "美团-W"},
            "meituan": {"symbol": "03690.HK", "display_name": "美团-W"},
            "mu": {"symbol": "MU", "display_name": "MU - MICRON TECHNOLOGY INC"},
            "micron technology": {"symbol": "MU", "display_name": "MU - MICRON TECHNOLOGY INC"},
            "spcx": {"symbol": "SPCX", "display_name": "SPCX - SPACE EXPLORATION TECHN-CL A"},
        },
    )
    rules = {row["id"]: row for row in payload["rules"]}
    actions = {row["id"]: row for row in payload["rebalance"]}

    assert payload["as_of"] == "2026-06-15"
    assert payload["plan"]["esop_debt_cny"] == 320_000
    assert [item["id"] for item in payload["inputs"]] == ["holdings", "risk_disclosure"]
    assert payload["inputs"][1]["href"] == "#portfolio-risk-disclosure"
    assert [group["id"] for group in payload["groups"]] == ["primary_allocation"]
    assert len(payload["rules"]) == 6
    assert set(rules) == {
        "esop",
        "stock",
        "bond",
        "cash",
        "non_allocation_assets",
        "debt",
    }
    drilldown = {row["id"]: row for row in payload["drilldown_rules"]}
    assert rules["stock"]["group_id"] == "primary_allocation"
    assert rules["bond"]["group_id"] == "primary_allocation"
    assert drilldown["us_equity"]["group_id"] == "primary_allocation"
    assert drilldown["us_stock"]["group_id"] == "primary_allocation"
    assert drilldown["other_equity"]["group_id"] == "primary_allocation"
    assert rules["esop"]["current"] == 0.30
    assert rules["esop"]["current_amount_cny"] == 300_000
    assert rules["esop"]["target_low_amount_cny"] == 600_000
    assert rules["esop"]["target_high_amount_cny"] == 670_000
    assert rules["esop"]["hard_high_amount_cny"] == 700_000
    assert "current_amount_label" in rules["esop"]
    assert "target_amount_label" in rules["esop"]
    assert "limit_amount_label" in rules["esop"]
    assert rules["esop"]["status"] == "待增配"
    assert rules["cash"]["tone"] == "info"
    assert rules["cash"]["status"] == "需增配"
    assert rules["cash"]["current_amount_cny"] == 70_000
    assert "notes" not in rules["cash"]
    assert rules["cash"]["children"][0]["name"] == "建行7332国内储蓄卡"
    assert rules["cash"]["children"][0]["amount_label"] == "¥70,000"
    assert not any(child["amount_cny"] < 0 for child in rules["cash"]["children"])
    assert rules["debt"]["current_amount_cny"] == -50_000
    assert rules["debt"]["current_amount_label"] == "-¥50,000"
    assert [child["rule_id"] for child in rules["debt"]["children"]] == ["debt_current", "debt_cost"]
    assert rules["debt"]["children"][0]["name"] == "当前融资"
    assert rules["debt"]["children"][1]["name"] == "ESOP后付息压力"
    assert drilldown["debt_current"]["current_amount_cny"] == -50_000
    assert drilldown["debt_current"]["current_amount_label"] == "-¥50,000"
    assert any(child["name"] == "IBKR · Cash USD" and child["amount_label"] == "-¥5,000" for child in drilldown["debt_current"]["children"])
    assert drilldown["debt_cost"]["current_amount_cny"] == 11_100
    assert drilldown["debt_cost"]["current_amount_label"] == "¥11,100"
    assert [child["name"] for child in drilldown["debt_cost"]["children"]] == ["当前年息", "新增融资年息"]
    assert sum(child["amount_cny"] for child in drilldown["debt_cost"]["children"]) == 11_100
    assert rules["bond"]["status"] == "需增配"
    assert rules["non_allocation_assets"]["status"] == "披露"
    assert rules["non_allocation_assets"]["target_label"] == "仅披露"
    assert rules["non_allocation_assets"]["limit_label"] == "不参与再平衡"
    assert rules["non_allocation_assets"]["current_amount_cny"] == 20_000
    assert rules["non_allocation_assets"]["children"][0]["name"] == "黄金 / 商品"
    assert rules["stock"]["children"][0]["name"] == "美国权益"
    assert rules["stock"]["children"][0]["rule_id"] == "us_equity"
    assert [child["name"] for child in rules["stock"]["children"]] == [
        "美国权益",
        "中国/港股权益",
        "韩国/欧洲/其他权益",
    ]
    assert sum(child["amount_cny"] for child in rules["stock"]["children"]) == rules["stock"]["current_amount_cny"]
    assert rules["stock"]["current_amount_cny"] == 560_000
    assert drilldown["us_equity"]["current_amount_cny"] == 320_000
    assert drilldown["cn_hk_equity"]["current_amount_cny"] == 240_000
    assert drilldown["other_equity"]["current_amount_cny"] == 0
    assert any(child["name"] == "易沪深300 未穿透残差" for child in drilldown["cn_hk_equity"]["children"])
    assert any(child["name"] == "美团-W" and child["symbol"] == "03690.HK" for child in drilldown["cn_hk_equity"]["children"])
    assert any(child["name"] == "SPACE EXPLORATION TECHN-CL A" and child["symbol"] == "SPCX" for child in drilldown["us_equity"]["children"])
    assert any(child["name"] == "MICRON TECHNOLOGY INC" and child["symbol"] == "MU" for child in drilldown["us_equity"]["children"])
    assert sum(child["amount_cny"] for child in drilldown["cn_hk_equity"]["children"]) == drilldown["cn_hk_equity"]["current_amount_cny"]
    assert drilldown["us_broad"]["current"] == 0.09
    assert drilldown["us_stock"]["children"][0]["name"] == "MICRON TECHNOLOGY INC"
    assert drilldown["us_stock"]["children"][0]["symbol"] == "MU"
    assert drilldown["us_stock"]["children"][0]["amount_cny"] == 140_000

    assert actions["do_esop"]["title"] == "执行 ESOP 增配"
    assert actions["buy_us_broad"]["title"] == "增配美股宽基"
    assert actions["reserve_interest"]["title"] == "预留融资成本"
    assert "¥11,100/年" in actions["reserve_interest"]["text"]
    assert actions["cut_single"]["title"] == "单票降至上限内"
    assert actions["cut_resource"]["title"] == "资源股暴露只减不加"


def test_portfolio_risk_disclosure_builds_seven_surfaces() -> None:
    risk = {
        "as_of": "2026-06-15",
        "summary": {
            "total_net_worth_cny": 1_000_000,
        },
        "positions": [
            {"asset_id": "HUAWEI_ESOP", "asset_name": "华为 ESOP", "asset_type": "esop", "position_value_cny": 300_000, "position_weight": 0.30},
            {"asset_id": "601899", "asset_name": "紫金矿业", "asset_type": "a_share_stock", "position_value_cny": 120_000, "position_weight": 0.12},
        ],
        "user_metrics": [
            {
                "key": "negative_cash_ratio",
                "title": "负现金 / 融资占比",
                "current_value": 0.12,
                "level": "red",
                "note": "融资偏高",
            },
            {
                "key": "single_position",
                "title": "单票 / 单资产暴露",
                "current_value": 0.30,
                "level": "yellow",
                "sources": [{"asset_name": "华为 ESOP", "position_value_cny": 300_000, "position_weight": 0.30}],
            },
            {
                "key": "equity_exposure",
                "title": "股票 / 权益暴露",
                "current_value": 0.80,
                "level": "yellow",
            },
        ],
        "investment_style_exposure": [
            {"label": "科技成长", "exposure_ratio": 0.28, "exposure_cny": 280_000, "level": "yellow"}
        ],
    }
    lookthrough = {
        "as_of": "2026-06-15",
        "total_market_value_cny": 1_000_000,
        "exposures": {
            "asset_type": [
                {"name": "非上市股权", "weight": 0.30, "value_cny": 300_000, "main_sources": [{"name": "华为 ESOP", "value_cny": 300_000}]},
                {"name": "A股个股", "weight": 0.20, "value_cny": 200_000},
                {"name": "现金", "weight": 0.05, "value_cny": 50_000},
            ],
            "region": [
                {"name": "中国", "weight": 0.50, "value_cny": 500_000},
                {"name": "美国", "weight": 0.25, "value_cny": 250_000},
            ],
            "currency": [
                {"name": "CNY", "weight": 0.65, "value_cny": 650_000},
                {"name": "USD", "weight": 0.25, "value_cny": 250_000},
            ],
            "industry": [
                {"name": "周期资源", "weight": 0.16, "value_cny": 160_000, "main_sources": [{"name": "紫金矿业", "value_cny": 120_000}]},
            ],
            "theme": [
                {"name": "贵金属与铜", "weight": 0.18, "value_cny": 180_000, "main_sources": [{"name": "紫金矿业", "value_cny": 120_000}]},
                {"name": "AI / 算力", "weight": 0.22, "value_cny": 220_000},
            ],
        },
    }

    payload = build_portfolio_risk_disclosure_payload_from_inputs(risk, lookthrough)
    categories = {row["id"]: row for row in payload["categories"]}

    assert [row["id"] for row in payload["inputs"]] == ["holdings", "risk_disclosure"]
    assert len(payload["categories"]) == 7
    assert payload["dashboard"]["checklist_total"] == 7
    assert 0 <= payload["dashboard"]["score"] <= 100
    assert payload["dashboard"]["status_tone"] in {"ok", "info", "warn", "risk"}
    assert len(payload["dashboard"]["category_scores"]) == 7
    assert set(categories) == {
        "market_direction",
        "region_currency",
        "industry_theme",
        "concentration",
        "liquidity_valuation",
        "leverage_financing",
        "factor_style",
    }
    assert categories["market_direction"]["checklist"]["total"] >= 1
    assert 0 <= categories["market_direction"]["score"] <= 100
    interest_item = next(item for item in categories["market_direction"]["items"] if item["id"] == "interest_credit")
    assert interest_item["tone"] == "ok"
    assert interest_item["rule"]["pass_condition"] == "当前值 < 20.0%"
    market_rule = categories["market_direction"]["items"][0]["rule"]
    assert set(market_rule) == {
        "name",
        "formula",
        "numerator",
        "denominator",
        "pass_condition",
        "warn_condition",
        "fail_condition",
        "linked_config_rule",
        "adjustment_direction",
    }
    assert "组合净资产" in market_rule["denominator"]
    assert market_rule["pass_condition"]
    assert market_rule["linked_config_rule"]
    assert categories["leverage_financing"]["items"][0]["name"] == "负现金/融资"
    assert categories["leverage_financing"]["items"][0]["tone"] == "risk"
    assert categories["leverage_financing"]["checklist"]["failed"] >= 1
    assert categories["leverage_financing"]["items"][0]["rule"]["formula"]
    assert any(item["name"] == "贵金属与铜" for item in categories["industry_theme"]["items"])
    assert categories["liquidity_valuation"]["items"][0]["name"] == "非上市权益估值"
    assert payload["method"]["target_config"].startswith("目标配置")
