from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.daily_return_state import (  # noqa: E402
    RETURN_STATUS_CONFIRMED,
    RETURN_STATUS_ESTIMATED,
    RETURN_STATUS_MIXED,
    RETURN_STATUS_PENDING,
    combine_return_status,
    daily_pnl_row_from_holding,
    normalize_return_status,
    quote_session_from_source,
    quote_snapshot_from_market_daily,
    return_status_label,
)


def test_daily_return_state_schema_contract() -> None:
    sql = Path("database/postgres/schema/069_daily_return_state.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.pams_market_quote_snapshot" in sql
    assert "CREATE TABLE IF NOT EXISTS public.pams_asset_daily_pnl" in sql
    assert "CREATE TABLE IF NOT EXISTS public.pams_sync_run_log" in sql
    assert "return_status IN ('pending', 'estimated', 'confirmed')" in sql
    assert "quote_session IN ('open', 'close', 'premarket', 'intraday', 'afterhours', 'overnight', 'nav', 'unknown')" in sql
    assert "trade_date, account_code, asset_key, base_currency" in sql


def test_normalize_return_status_keeps_estimate_and_confirmed_separate() -> None:
    assert normalize_return_status(source="databento_estimate", market="US_OVERNIGHT") == RETURN_STATUS_ESTIMATED
    assert normalize_return_status(source="tiantian_fundgz_estimate") == RETURN_STATUS_ESTIMATED
    assert normalize_return_status(source="tushare_fund_nav") == RETURN_STATUS_CONFIRMED
    assert normalize_return_status(source="ibkr_flex") == RETURN_STATUS_CONFIRMED
    assert normalize_return_status() == RETURN_STATUS_PENDING


def test_status_label_and_combined_status() -> None:
    assert return_status_label(RETURN_STATUS_ESTIMATED) == "\u9884\u4f30\u6536\u76ca"
    assert return_status_label(RETURN_STATUS_CONFIRMED) == "\u4eca\u65e5\u6536\u76ca\u5df2\u786e\u8ba4"
    assert combine_return_status([]) == RETURN_STATUS_PENDING
    assert combine_return_status([RETURN_STATUS_CONFIRMED, RETURN_STATUS_CONFIRMED]) == RETURN_STATUS_CONFIRMED
    assert combine_return_status([RETURN_STATUS_ESTIMATED, RETURN_STATUS_CONFIRMED]) == RETURN_STATUS_MIXED


def test_quote_snapshot_from_market_daily_marks_databento_estimate() -> None:
    quote = quote_snapshot_from_market_daily(
        {
            "as_of": date(2026, 6, 25),
            "asset_id": "US_MU",
            "symbol": "MU",
            "name": "Micron Technology",
            "market": "US_OVERNIGHT",
            "currency": "USD",
            "source": "databento_estimate",
            "source_key": "MU",
            "close_price": Decimal("1166"),
            "raw_json": {"dataset": "XNAS.ITCH"},
        }
    )

    assert quote["quote_date"] == date(2026, 6, 25)
    assert quote["return_status"] == RETURN_STATUS_ESTIMATED
    assert quote["quote_session"] == "overnight"
    assert quote["price"] == Decimal("1166")
    assert quote["source_hash"]


def test_quote_snapshot_from_market_daily_marks_confirmed_fund_nav() -> None:
    quote = quote_snapshot_from_market_daily(
        {
            "as_of": date(2026, 6, 25),
            "asset_id": "FUND_001412",
            "symbol": "001412",
            "market": "\u57fa\u91d1",
            "currency": "CNY",
            "source": "tushare_fund_nav",
            "source_key": "001412.OF",
            "unit_nav": Decimal("1.4889"),
        }
    )

    assert quote["return_status"] == RETURN_STATUS_CONFIRMED
    assert quote["quote_session"] == "nav"
    assert quote["price"] == Decimal("1.4889")


def test_daily_pnl_row_from_holding_preserves_backend_status_and_values() -> None:
    row = daily_pnl_row_from_holding(
        {
            "as_of": date(2026, 6, 25),
            "account_code": "tencent_licaitong",
            "asset_id": "FUND_001412",
            "symbol": "001412",
            "name": "\u534e\u590f\u79d1\u521b50ETF\u8054\u63a5C",
            "major_category": "A\u80a1",
            "minor_category": "A\u80a1\u57fa\u91d1",
            "currency": "CNY",
            "quantity": Decimal("32665.91"),
            "current_price": Decimal("1.4889"),
            "market_value_cny": Decimal("48637.29"),
            "daily_total_pnl_cny": Decimal("172.35"),
            "holding_pnl_cny": Decimal("10637.29"),
            "daily_pnl_status": "confirmed",
            "daily_pnl_status_label": "\u4eca\u65e5\u6536\u76ca\u5df2\u786e\u8ba4",
        }
    )

    assert row["trade_date"] == date(2026, 6, 25)
    assert row["asset_key"] == "FUND_001412"
    assert row["daily_pnl_cny"] == Decimal("172.35")
    assert row["total_pnl_cny"] == Decimal("10637.29")
    assert row["return_status"] == RETURN_STATUS_CONFIRMED
    assert row["source_hash"]


def test_quote_session_defaults_to_intraday_for_raw_estimate() -> None:
    assert quote_session_from_source(raw_status="estimated", default="close") == "intraday"
