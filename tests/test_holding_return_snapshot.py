from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.holding_return_snapshot import (  # noqa: E402
    build_section_return_series_payload_from_snapshot,
    build_investment_return_series_payload_from_snapshot,
    holding_return_snapshot_table_exists,
    upsert_investment_return_series_snapshot,
)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_holding_return_snapshot_schema_contract() -> None:
    sql = Path("database/postgres/schema/025_holding_return_snapshot.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS pams_holding_return_snapshot" in sql
    assert "PRIMARY KEY (snapshot_date, section, base_currency)" in sql
    assert "idx_holding_return_snapshot_series" in sql
    assert "time_weighted_return_rate" in sql


def test_build_return_series_from_snapshot_preserves_legacy_shape() -> None:
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
        },
        {
            "snapshot_date": date(2026, 1, 2),
            "market_value": Decimal("112"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("12"),
            "realized_income": Decimal("3"),
            "total_pnl": Decimal("15"),
            "return_rate": Decimal("0.15"),
            "daily_return_rate": Decimal("0.12"),
            "time_weighted_return_rate": Decimal("0.12"),
        },
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows, [], [], [], []])

    payload = build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        today_label_date=date(2026, 1, 3),
    )

    assert payload is not None
    assert payload["method"] == "holding_return_snapshot"
    assert payload["start"] == "2026-01-01"
    assert payload["end"] == "2026-01-02"
    assert payload["rows"][-1]["market_value"] == "112"
    assert payload["rows"][-1]["total_pnl"] == "15"
    assert payload["today_window"]["total_return"] == "0"


def test_snapshot_payload_prefers_stored_today_window() -> None:
    stored_window = {
        "label_date": "2026-01-03",
        "baseline_date": "2026-01-02",
        "latest_date": "2026-01-03",
        "total_return": "7",
        "return_rate": "0.07",
    }
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "raw_json": {},
        },
        {
            "snapshot_date": date(2026, 1, 3),
            "market_value": Decimal("112"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("12"),
            "realized_income": Decimal("3"),
            "total_pnl": Decimal("15"),
            "return_rate": Decimal("0.15"),
            "daily_return_rate": Decimal("0.12"),
            "time_weighted_return_rate": Decimal("0.12"),
            "raw_json": {"today_window": stored_window},
        },
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows, [], [], [], []])

    payload = build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 3),
        today_label_date=date(2026, 1, 3),
    )

    assert payload is not None
    assert payload["today_window"] == stored_window


def test_total_snapshot_today_window_prefers_live_section_windows() -> None:
    stale_total_window = {
        "label_date": "2026-01-03",
        "baseline_date": "2026-01-02",
        "latest_date": "2026-01-03",
        "total_return": "999",
        "return_rate": "9.99",
    }
    total_rows = [
        {
            "snapshot_date": date(2026, 1, 3),
            "market_value": Decimal("1000"),
            "cost": Decimal("1000"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "raw_json": {"today_window": stale_total_window},
        }
    ]
    section_row = lambda amount, value: [
        {
            "snapshot_date": date(2026, 1, 3),
            "market_value": Decimal(str(value)),
            "cost": Decimal("0"),
            "total_pnl": Decimal("0"),
            "cumulative_capital": Decimal("0"),
            "raw_json": {
                "today_window": {
                    "label_date": "2026-01-03",
                    "baseline_date": "2026-01-02",
                    "latest_date": "2026-01-03",
                    "total_return": str(amount),
                    "return_rate": "0",
                }
            },
        }
    ]
    cur = FakeCursor(
        [
            {"table_name": "pams_holding_return_snapshot"},
            total_rows,
            section_row("1.5", "100"),
            section_row("2.5", "100"),
            [],
            section_row("-1", "100"),
        ]
    )

    payload = build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 3),
        today_label_date=date(2026, 1, 3),
    )

    assert payload is not None
    assert payload["today_window"]["total_return"] == "3.0"


def test_snapshot_payload_ignores_stored_today_window_for_previous_label() -> None:
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "raw_json": {},
        },
        {
            "snapshot_date": date(2026, 1, 2),
            "market_value": Decimal("112"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("12"),
            "realized_income": Decimal("3"),
            "total_pnl": Decimal("15"),
            "return_rate": Decimal("0.15"),
            "daily_return_rate": Decimal("0.12"),
            "time_weighted_return_rate": Decimal("0.12"),
            "raw_json": {
                "today_window": {
                    "label_date": "2026-01-02",
                    "baseline_date": "2026-01-01",
                    "latest_date": "2026-01-02",
                    "total_return": "15",
                    "return_rate": "0.15",
                }
            },
        },
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows, [], [], [], []])

    payload = build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
        today_label_date=date(2026, 1, 3),
    )

    assert payload is not None
    assert payload["today_window"] == {
        "label_date": "2026-01-03",
        "baseline_date": "2026-01-02",
        "latest_date": "2026-01-02",
        "total_return": "0",
        "return_rate": "0",
    }


def test_snapshot_payload_rejects_incomplete_cache_when_required() -> None:
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "data_completeness": None,
            "raw_json": {},
        }
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows])

    assert build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 1),
        require_data_completeness=True,
    ) is None


def test_snapshot_payload_accepts_complete_cache_when_required() -> None:
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("100"),
            "cost": Decimal("100"),
            "position_pnl": Decimal("0"),
            "realized_income": Decimal("0"),
            "total_pnl": Decimal("0"),
            "return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "data_completeness": "complete",
            "raw_json": {},
        }
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows])

    payload = build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 1),
        require_data_completeness=True,
    )

    assert payload is not None
    assert payload["rows"][0]["market_value"] == "100"


def test_snapshot_table_missing_returns_false_and_no_payload() -> None:
    cur = FakeCursor([{"table_name": None}])

    assert holding_return_snapshot_table_exists(cur) is False

    cur = FakeCursor([{"table_name": None}])
    assert build_investment_return_series_payload_from_snapshot(
        cur,
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 1, 2),
    ) is None


def test_a_stock_snapshot_with_zero_value_history_is_rejected() -> None:
    rows = [
        {
            "snapshot_date": date(2026, 1, 1),
            "market_value": Decimal("0"),
            "net_flow": Decimal("25000"),
            "cumulative_capital": Decimal("25000"),
            "capital_return_rate": Decimal("-1"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "display_return_rate": Decimal("-1"),
            "raw_json": {},
        },
        {
            "snapshot_date": date(2026, 6, 5),
            "market_value": Decimal("233392.13"),
            "net_flow": Decimal("0"),
            "cumulative_capital": Decimal("250000"),
            "capital_return_rate": Decimal("-0.06643148"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "display_return_rate": Decimal("-0.06643148"),
            "raw_json": {},
        },
    ]
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}, rows])

    assert build_section_return_series_payload_from_snapshot(
        cur,
        section="a_stock",
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 6, 5),
    ) is None


def test_upsert_return_series_snapshot_writes_rows() -> None:
    cur = FakeCursor([{"table_name": "pams_holding_return_snapshot"}])
    payload = {
        "today_window": {"total_return": "7"},
        "rows": [
            {
                "date": "2026-01-02",
                "market_value": "112",
                "cost": "100",
                "position_pnl": "12",
                "realized_income": "3",
                "total_pnl": "15",
                "return_rate": "0.15",
                "daily_return_rate": "0.12",
                "time_weighted_return_rate": "0.12",
            }
        ]
    }

    written = upsert_investment_return_series_snapshot(cur, payload=payload, base_currency="CNY")

    assert written == 1
    assert any("INSERT INTO pams_holding_return_snapshot" in sql for sql, _ in cur.executed)
    assert any('"today_window": {"total_return": "7"}' in str(value) for value in cur.executed[-1][1])
    assert cur.executed[-1][1][-1] == "complete"
