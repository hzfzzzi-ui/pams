import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload_from_inputs


def test_risk_diagnosis_computes_all_and_ex_esop_scopes():
    lookthrough = {
        "as_of": "2026-11-25",
        "total_market_value_cny": 1000000.0,
        "exposures": {
            "asset_type": [
                {"name": "非上市股权", "value_cny": 500000.0, "weight": 0.5},
                {"name": "美股个股", "value_cny": 300000.0, "weight": 0.3},
                {"name": "A股个股", "value_cny": 150000.0, "weight": 0.15},
                {"name": "现金", "value_cny": 50000.0, "weight": 0.05},
            ],
            "currency": [
                {"name": "CNY", "value_cny": 700000.0, "weight": 0.7},
                {"name": "USD", "value_cny": 300000.0, "weight": 0.3},
            ],
            "region": [],
            "industry": [],
            "theme": [],
        },
        "normalized_holdings": [
            {"symbol": "HUAWEI_ESOP", "name": "内部股份分红", "assetType": "非上市股权", "currency": "CNY", "marketValue": 500000.0},
            {"symbol": "NVDA", "name": "NVIDIA CORP", "assetType": "美股个股", "currency": "USD", "marketValue": 300000.0},
            {"symbol": "601899", "name": "紫金矿业", "assetType": "A股个股", "currency": "CNY", "marketValue": 150000.0},
            {"symbol": "CASH_CNY", "name": "Cash CNY", "assetType": "现金", "currency": "CNY", "marketValue": 50000.0},
        ],
    }
    account_cards = {
        "investment_overview": {"net_value": {"value": "700000.0"}},
        "ibkr": {"cash": {"value": "-150000.0"}, "leverage": {"value": "2.0"}},
    }
    payload = build_portfolio_risk_disclosure_payload_from_inputs(
        {"as_of": "2026-11-25", "summary": {"total_net_worth_cny": 1000000.0}},
        lookthrough,
        account_cards=account_cards,
    )
    diag = payload["diagnosis"]
    assert diag["has_esop"] is True
    assert diag["esop_value_cny"] == 500000.0
    assert diag["all"]["leverage_ratio"] == round(1000000.0 / 700000.0, 2)
    assert diag["all"]["ibkr_debt_cny"] == 150000.0
    assert diag["all"]["liquidity"]["locked_pct"] == 0.5
    assert diag["ex_esop"]["gross_exposure_cny"] == 500000.0
    assert diag["ex_esop"]["liquidity"]["locked_pct"] == 0.0
    assert diag["ex_esop"]["top1_name"] == "NVIDIA CORP"
    assert diag["ex_esop"]["stress"]["equity_drop_20"]["impact_cny"] == -90000.0
    assert diag["ex_esop"]["stress"]["fx_drop_5"]["impact_cny"] == -15000.0
