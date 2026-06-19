from pathlib import Path

from src.ingest.csv_loader import read_positions_csv
from src.ingest.store import PortfolioStore, import_positions_csv


def test_csv_loader_normalizes_owner_ratio_and_cny(tmp_path: Path):
    csv_path = tmp_path / "positions.csv"
    csv_path.write_text(
        "as_of,account_code,symbol,asset_name,asset_type,currency,quantity,price,market_value,cost\n"
        "2026-05-31,a_share_stock,600519,Sample,a_share_stock,CNY,100,10,,900\n",
        encoding="utf-8",
    )
    limits = {
        "base_currency": "CNY",
        "data": {
            "account_owner_ratios": {"a_share_stock": 0.625},
            "default_owner_ratio": 1.0,
            "fx_rates_to_cny": {"CNY": 1.0},
            "cash_asset_types": ["cash"],
        },
    }
    df = read_positions_csv(csv_path, "a_share_stock", limits)
    assert df.iloc[0]["market_value"] == 1000
    assert df.iloc[0]["market_value_cny"] == 625
    assert df.iloc[0]["owner_ratio"] == 0.625


def test_import_writes_raw_lineage_and_latest_positions(tmp_path: Path):
    csv_path = tmp_path / "manual.csv"
    csv_path.write_text(
        "as_of,account_code,symbol,asset_name,asset_type,currency,market_value,cost\n"
        "2026-05-31,manual,CASH_CNY,Cash,cash,CNY,1000,1000\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "portfolio.sqlite3"
    raw_dir = tmp_path / "raw"
    limits = {
        "base_currency": "CNY",
        "data": {"fx_rates_to_cny": {"CNY": 1.0}, "cash_asset_types": ["cash"]},
    }
    result = import_positions_csv(csv_path, "manual", db_path=db_path, raw_dir=raw_dir, risk_limits=limits)
    assert result["row_count"] == 1
    assert Path(result["raw_path"]).exists()

    store = PortfolioStore(db_path)
    positions = store.latest_positions()
    assert len(positions) == 1
    assert positions.iloc[0]["asset_id"] == "CASH_CNY"


def test_csv_loader_accepts_fully_populated_template(tmp_path: Path):
    csv_path = tmp_path / "template.csv"
    csv_path.write_text(
        "as_of,account_code,account_name,owner_ratio,symbol,asset_id,asset_name,asset_type,currency,quantity,price,market_value,cost,fx_rate_to_cny\n"
        "2026-05-31,ibkr,IBKR,1.0,AAPL,AAPL,Apple,us_stock,USD,10,190,1900,1800,7.2\n",
        encoding="utf-8",
    )
    limits = {
        "base_currency": "CNY",
        "data": {"fx_rates_to_cny": {"USD": 7.2}, "cash_asset_types": ["cash"]},
    }
    df = read_positions_csv(csv_path, "ibkr", limits)
    assert df.iloc[0]["market_value_cny"] == 13680
    assert df.iloc[0]["owner_ratio"] == 1.0
