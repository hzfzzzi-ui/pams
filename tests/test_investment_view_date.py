from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.investment_view_date import (  # noqa: E402
    clamp_to_latest_data_date,
    daily_pnl_window_date,
    market_data_candidate_date,
)


def test_market_data_candidate_uses_previous_date_before_1505():
    now = datetime(2026, 6, 2, 15, 4, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert market_data_candidate_date(now=now) == date(2026, 6, 1)


def test_market_data_candidate_uses_current_date_at_1505():
    now = datetime(2026, 6, 2, 15, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert market_data_candidate_date(now=now) == date(2026, 6, 2)


def test_market_data_candidate_converts_timezone_before_cutoff():
    now = datetime(2026, 6, 2, 6, 59, 59, tzinfo=ZoneInfo("UTC"))

    assert market_data_candidate_date(now=now, tz_name="Asia/Shanghai") == date(2026, 6, 1)


def test_clamp_to_latest_data_date_prevents_empty_today_row():
    assert clamp_to_latest_data_date(date(2026, 6, 2), date(2026, 6, 1)) == date(2026, 6, 1)


def test_clamp_to_latest_data_date_keeps_candidate_when_data_is_current():
    assert clamp_to_latest_data_date(date(2026, 6, 2), date(2026, 6, 2)) == date(2026, 6, 2)


def test_daily_pnl_window_keeps_previous_date_before_0700():
    now = datetime(2026, 6, 16, 6, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert daily_pnl_window_date(now=now) == date(2026, 6, 15)


def test_daily_pnl_window_uses_current_date_at_0700():
    now = datetime(2026, 6, 16, 7, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert daily_pnl_window_date(now=now) == date(2026, 6, 16)
