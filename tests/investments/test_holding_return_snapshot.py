from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.holding_return_snapshot import (  # noqa: E402
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
    sql = Path("database/postgres/schema/025_valuation_results.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.valuation_results" in sql
    assert "ON public.valuation_results (snapshot_date,section,base_currency)" in sql
    assert "idx_valuation_results_holding_series" in sql
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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows, [], [], [], []])

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
    assert payload["today_window"]["total_return"] is None
    assert payload["today_window"]["reason"] == "stale_snapshot"


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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows, [], [], [], []])

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
            "net_flow": Decimal("0"),
            "capital_return_rate": Decimal("0"),
            "daily_return_rate": Decimal("0"),
            "time_weighted_return_rate": Decimal("0"),
            "display_return_rate": Decimal("0"),
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
            {"table_name": "valuation_results"},
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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows, [], [], [], []])

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
        "total_return": None,
        "return_rate": None,
        "status": "unavailable",
        "reason": "stale_snapshot",
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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows])

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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows])

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
    cur = FakeCursor([{"table_name": "valuation_results"}, rows])

    assert build_section_return_series_payload_from_snapshot(
        cur,
        section="a_stock",
        start_date=date(2026, 1, 1),
        interval="daily",
        base_currency="CNY",
        view_as_of=date(2026, 6, 5),
    ) is None


def test_upsert_return_series_snapshot_writes_rows() -> None:
    cur = FakeCursor([{"table_name": "valuation_results"}])
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
    assert any("INSERT INTO valuation_results" in sql for sql, _ in cur.executed)
    assert any('"today_window": {"total_return": "7"}' in str(value) for value in cur.executed[-1][1])
    assert cur.executed[-1][1][-1] == "complete"


def test_zero_total_return_is_not_replaced_by_position_gain():
    from domain.investments.holding_return_snapshot import _snapshot_return_amount
    row = {'market_value': Decimal('100'), 'cost': Decimal('80'), 'total_pnl': Decimal('0')}
    assert _snapshot_return_amount(row) == 0


def test_missing_or_explicit_unknown_total_never_becomes_zero_or_position_gain():
    from domain.investments.holding_return_snapshot import _snapshot_return_amount, _snapshot_today_window
    assert _snapshot_return_amount({'market_value': None, 'cost': None, 'total_pnl': None}) is None
    old = {'snapshot_date': date(2026, 1, 1), 'market_value': Decimal('100'),
           'cost': Decimal('80'), 'total_pnl': Decimal('0'), 'data_completeness': 'complete',
           'raw_json': {'row': {'total_pnl': None}}}
    assert _snapshot_return_amount(old) is None
    result = _snapshot_today_window([old], date(2026, 1, 1))
    assert result['total_return'] is None and result['return_rate'] is None
    assert result['status'] == 'unavailable'


def test_zero_denominator_does_not_fabricate_daily_return_rate():
    from domain.investments.holding_return_snapshot import _snapshot_today_window
    row = {'snapshot_date': date(2026, 1, 1), 'market_value': Decimal('0'),
           'cost': Decimal('0'), 'total_pnl': Decimal('0')}
    result = _snapshot_today_window([row], date(2026, 1, 1))
    assert result['total_return'] == '0' and result['return_rate'] is None
    assert result['reason'] == 'zero_return_denominator'


def test_section_old_filled_zero_does_not_override_explicit_unknown_summary():
    row = {'snapshot_date': date(2026, 1, 1), 'market_value': Decimal('100'),
           'net_flow': Decimal('0'), 'cumulative_capital': Decimal('100'),
           'capital_return_rate': Decimal('0'), 'daily_return_rate': Decimal('0'),
           'time_weighted_return_rate': Decimal('0'), 'display_return_rate': Decimal('0'),
           'data_completeness': 'complete',
           'raw_json': {'status': 'unavailable', 'reason': 'fx_rate_unknown',
                        'summary': {'total_return': '0', 'total_return_rate': '0'},
                        'row': {'market_value': '100', 'daily_return_rate': None,
                                'time_weighted_return_rate': None, 'display_return_rate': None}}}
    cur = FakeCursor([{'table_name': 'valuation_results'}, [row]])
    payload = build_section_return_series_payload_from_snapshot(
        cur, section='us_stock', start_date=date(2026,1,1), interval='daily',
        base_currency='CNY', view_as_of=date(2026,1,1), today_label_date=date(2026,1,1),
        require_data_completeness=False)
    assert payload['status'] == 'unavailable' and payload['reason'] == 'fx_rate_unknown'
    assert payload['summary']['total_return'] is None
    assert payload['rows'][0]['daily_return_rate'] is None
    assert payload['today_window']['total_return'] is None
