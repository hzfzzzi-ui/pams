from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.investment_returns import _today_window_payload  # noqa: E402


def test_today_window_keeps_active_close_bucket_after_manual_sync():
    rows = [
        {"date": date(2026, 5, 31), "market_value_cny": Decimal("1000"), "total_pnl_cny": Decimal("100")},
        {"date": date(2026, 6, 1), "market_value_cny": Decimal("1050"), "total_pnl_cny": Decimal("150")},
        {"date": date(2026, 6, 2), "market_value_cny": Decimal("1080"), "total_pnl_cny": Decimal("180")},
    ]

    payload = _today_window_payload(
        rows,
        label_date=date(2026, 6, 1),
        return_amount=lambda row: Decimal("0") if row is None else Decimal(row["total_pnl_cny"]),
    )

    assert payload["label_date"] == "2026-06-01"
    assert payload["baseline_date"] == "2026-05-31"
    assert payload["latest_date"] == "2026-06-02"
    assert payload["total_return"] == "80"


def test_today_window_does_not_reset_when_latest_row_duplicates_label_row():
    rows = [
        {"date": date(2026, 5, 31), "market_value_cny": Decimal("1000"), "total_pnl_cny": Decimal("100")},
        {"date": date(2026, 6, 1), "market_value_cny": Decimal("1050"), "total_pnl_cny": Decimal("150")},
        {"date": date(2026, 6, 2), "market_value_cny": Decimal("1050"), "total_pnl_cny": Decimal("150")},
    ]

    payload = _today_window_payload(
        rows,
        label_date=date(2026, 6, 1),
        return_amount=lambda row: Decimal("0") if row is None else Decimal(row["total_pnl_cny"]),
    )

    assert payload["total_return"] == "50"


def test_today_window_clears_after_daily_pnl_rollover_when_data_is_stale():
    rows = [
        {"date": date(2026, 6, 14), "market_value_cny": Decimal("1000"), "total_pnl_cny": Decimal("100")},
        {"date": date(2026, 6, 15), "market_value_cny": Decimal("1050"), "total_pnl_cny": Decimal("150")},
    ]

    payload = _today_window_payload(
        rows,
        label_date=date(2026, 6, 16),
        return_amount=lambda row: Decimal("0") if row is None else Decimal(row["total_pnl_cny"]),
    )

    assert payload["label_date"] == "2026-06-16"
    assert payload["baseline_date"] == "2026-06-15"
    assert payload["latest_date"] == "2026-06-15"
    assert payload["total_return"] == "0"
