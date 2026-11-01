from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.daily_return_state import (  # noqa: E402
    RETURN_STATUS_CONFIRMED,
    RETURN_STATUS_ESTIMATED,
    RETURN_STATUS_MIXED,
    RETURN_STATUS_PENDING,
    combine_return_status,
    normalize_return_status,
    quote_session_from_source,
    quote_snapshot_from_market_daily,
    return_status_label,
)


def test_daily_return_state_schema_contract() -> None:
    sql = Path("database/postgres/schema/025a_asset_snapshots.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.market_observations" in sql
    assert "return_status IN ('pending','estimated','confirmed')" in sql
    assert "quote_session IN ('open','close','premarket','intraday','afterhours','overnight','nav','unknown')" in sql
    assert "as_of, provider, source_key, quote_session" in sql


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


def test_quote_snapshot_from_market_daily_prefers_estimated_fund_nav() -> None:
    quote = quote_snapshot_from_market_daily(
        {
            "as_of": date(2026, 6, 25),
            "asset_id": "FUND_001412",
            "symbol": "001412",
            "market": "\u57fa\u91d1",
            "currency": "CNY",
            "source": "tiantian_fundgz_estimate",
            "source_key": "001412",
            "unit_nav": Decimal("1.4889"),
            "estimated_unit_nav": Decimal("1.5012"),
        }
    )

    assert quote["return_status"] == RETURN_STATUS_ESTIMATED
    assert quote["quote_session"] == "nav"
    assert quote["price"] == Decimal("1.5012")


def test_quote_session_defaults_to_intraday_for_raw_estimate() -> None:
    assert quote_session_from_source(raw_status="estimated", default="close") == "intraday"
