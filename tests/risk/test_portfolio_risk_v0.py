from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))

import pandas as pd

from domain.risk.core.risk_engine import (
    account_weight,
    factor_exposure,
    gross_exposure,
    position_weight,
    risk_alerts,
    stress_loss,
    total_nav,
)


def sample_limits():
    return {
        "base_currency": "CNY",
        "data": {"cash_asset_types": ["cash"], "fx_rates_to_cny": {"CNY": 1, "USD": 7.2}},
        "limits": {
            "blocking_level": "red",
            "gross_exposure": {"yellow": 0.8, "red": 1.1},
            "negative_cash_ratio": {"yellow": 0.01, "red": 0.05},
            "account_weight": {"default": {"yellow": 0.6, "red": 0.8}, "by_account": {}},
            "position_weight": {
                "default": {"yellow": 0.2, "red": 0.3},
                "by_asset_type": {"cash": {"yellow": 1.0, "red": 1.01}},
            },
            "factor_exposure": {"US_EQUITY": {"yellow": 0.2, "red": 0.4}},
            "stress_loss": {"yellow": 0.05, "red": 0.15},
        },
        "stress_tests": {"us_down_50": {"label": "US -50%", "shocks": {"US_EQUITY": -0.5}}},
    }


def sample_factor_map():
    return {
        "symbols": {"AAPL": {"US_EQUITY": 1.0, "USD_FX": 1.0}, "CASH_CNY": {"CASH": 1.0}},
        "asset_types": {"us_stock": {"US_EQUITY": 1.0, "USD_FX": 1.0}, "cash": {"CASH": 1.0}},
        "default": {"OTHER": 1.0},
    }


def sample_positions():
    return pd.DataFrame(
        [
            {
                "account_code": "ibkr",
                "account_name": "IBKR",
                "asset_id": "AAPL",
                "symbol": "AAPL",
                "asset_name": "Apple",
                "asset_type": "us_stock",
                "currency": "USD",
                "market_value": 1000,
                "cost": 900,
                "fx_rate_to_cny": 7.2,
                "owner_ratio": 1.0,
            },
            {
                "account_code": "manual",
                "account_name": "Manual",
                "asset_id": "CASH_CNY",
                "symbol": "CASH_CNY",
                "asset_name": "Cash",
                "asset_type": "cash",
                "currency": "CNY",
                "market_value": 2800,
                "cost": 2800,
                "fx_rate_to_cny": 1,
                "owner_ratio": 1.0,
            },
            {
                "account_code": "a_share_stock",
                "account_name": "A Share",
                "asset_id": "600519",
                "symbol": "600519",
                "asset_name": "Sample A",
                "asset_type": "a_share_stock",
                "currency": "CNY",
                "market_value": 1600,
                "cost": 1500,
                "fx_rate_to_cny": 1,
                "owner_ratio": 0.625,
            },
        ]
    )


def test_total_nav_applies_fx_and_owner_ratio():
    assert total_nav(sample_positions(), sample_limits()) == 11000


def test_gross_exposure_excludes_cash():
    result = gross_exposure(sample_positions(), sample_limits())
    assert result["gross_exposure_cny"] == 8200
    assert round(result["gross_exposure_ratio"], 4) == 0.7455


def test_account_and_position_weights():
    accounts = account_weight(sample_positions(), sample_limits())
    assert accounts.iloc[0]["account_code"] == "ibkr"
    assert round(accounts.iloc[0]["account_weight"], 4) == 0.6545

    positions = position_weight(sample_positions(), sample_limits())
    assert positions.iloc[0]["asset_id"] == "AAPL"
    assert round(positions.iloc[0]["position_weight"], 4) == 0.6545


def test_factor_exposure_and_stress_loss():
    factors = factor_exposure(sample_positions(), sample_factor_map(), sample_limits())
    us = factors.loc[factors["factor"] == "US_EQUITY"].iloc[0]
    assert round(us["exposure_ratio"], 4) == 0.6545

    stress = stress_loss(
        sample_positions(),
        sample_factor_map(),
        sample_limits()["stress_tests"],
        sample_limits(),
    )
    assert round(stress.iloc[0]["loss_ratio"], 4) == 0.3273


def test_risk_alerts_include_red_position_and_factor():
    alerts = risk_alerts(sample_positions(), sample_limits(), sample_factor_map())
    red_rules = {a["rule"] for a in alerts if a["level"] == "red"}
    assert "position_weight:AAPL" in red_rules
    assert "factor_exposure:US_EQUITY" in red_rules
