import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.assets.lookthrough_state import derive_lookthrough_payload


def test_direct_foreign_stocks_auto_classify_as_direct_full():
    positions = [
        {
            "account_code": "ibkr",
            "symbol": "SPCX",
            "asset_name": "SPCX - SPACE EXPLORATION TECHN-CL A",
            "asset_type": "us_stock",
            "currency": "USD",
            "market_value_cny": 8500.0,
            "as_of": "2026-11-25",
        },
        {
            "account_code": "ibkr",
            "symbol": "CBRS",
            "asset_name": "CBRS - CEREBRAS SYSTEMS INC - A",
            "asset_type": "us_stock",
            "currency": "USD",
            "market_value_cny": 6000.0,
            "as_of": "2026-11-25",
        },
        {
            "account_code": "ibkr",
            "symbol": "SIVE",
            "asset_name": "SIVE - SIVERS SEMICONDUCTORS AB",
            "asset_type": "other",
            "currency": "SEK",
            "market_value_cny": 40.0,
            "as_of": "2026-11-25",
        },
        {
            "account_code": "ibkr",
            "symbol": "NVDAX",
            "asset_name": "NVDAX - EXAMPLE SEMICONDUCTOR CORP",
            "asset_type": "us_stock",
            "currency": "USD",
            "market_value_cny": 2000.0,
            "as_of": "2026-11-25",
        },
    ]

    payload = derive_lookthrough_payload(positions, config={"asset_mapping": [], "lookthrough": []})
    quality = payload["quality"]
    by_sym = {item["symbol"]: item for item in quality["items"]}

    for sym in ("SPCX", "CBRS", "SIVE", "NVDAX"):
        assert by_sym[sym]["lookthrough_type"] == "DIRECT"
        assert by_sym[sym]["quality_status"] == "FULL"
        assert by_sym[sym]["coverage_ratio"] == 1.0

    assert quality["coverage_ratio"] == 1.0
    assert quality["low_confidence_assets"] == []

    holdings_by_sym = {h["symbol"]: h for h in payload["normalized_holdings"]}
    assert holdings_by_sym["SPCX"]["industryBucket"] == "高端制造"
    assert holdings_by_sym["CBRS"]["industryBucket"] == "科技TMT"
    assert holdings_by_sym["SIVE"]["industryBucket"] == "科技TMT"
    assert holdings_by_sym["NVDAX"]["industryBucket"] == "科技TMT"


def test_etf_and_funds_are_never_misclassified_as_direct_stock():
    positions = [
        {
            "account_code": "ibkr",
            "symbol": "VOO",
            "asset_name": "VOO - VANGUARD S&P 500 ETF",
            "asset_type": "us_stock",
            "currency": "USD",
            "market_value_cny": 10000.0,
            "as_of": "2026-11-25",
        },
        {
            "account_code": "lct",
            "symbol": "000001",
            "asset_name": "测试成长混合基金A",
            "asset_type": "a_share_fund",
            "currency": "CNY",
            "market_value_cny": 5000.0,
            "as_of": "2026-11-25",
        },
    ]

    payload = derive_lookthrough_payload(positions, config={"asset_mapping": [], "lookthrough": []})
    by_sym = {item["symbol"]: item for item in payload["quality"]["items"]}

    assert by_sym["VOO"]["lookthrough_type"] == "PRODUCT_TAG"
    assert by_sym["VOO"]["quality_status"] == "PRODUCT_TAG_ONLY"
    assert by_sym["VOO"]["coverage_ratio"] == 0.0
    assert by_sym["000001"]["lookthrough_type"] == "PRODUCT_TAG"
    assert by_sym["000001"]["quality_status"] == "PRODUCT_TAG_ONLY"
