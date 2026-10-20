from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.fx_rates import build_latest_fx_rates_payload  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_build_latest_fx_rates_payload_handles_empty_result():
    payload = build_latest_fx_rates_payload(FakeCursor([]), base_currency="CNY")

    assert payload == {"as_of": None, "rates": []}


def test_build_latest_fx_rates_payload_preserves_shape_and_date_normalization():
    cur = FakeCursor(
        [
            {
                "as_of": date(2026, 5, 31),
                "base_currency": "CNY",
                "quote_currency": "USD",
                "rate": Decimal("7.20"),
                "source": "manual",
            }
        ]
    )

    payload = build_latest_fx_rates_payload(cur, base_currency="CNY")

    assert set(payload) == {"as_of", "base_currency", "rates"}
    assert payload["as_of"] == "2026-05-31"
    assert payload["base_currency"] == "CNY"
    assert payload["rates"][0]["as_of"] == "2026-05-31"
    assert payload["rates"][0]["rate"] == "7.20"
    assert "DISTINCT ON (quote_currency)" in cur.executed[0][0]
    assert cur.executed[0][1] == ("CNY",)


def test_build_latest_fx_rates_payload_uses_max_rate_date_as_top_level_date():
    cur = FakeCursor(
        [
            {
                "as_of": date(2026, 5, 31),
                "base_currency": "CNY",
                "quote_currency": "HKD",
                "rate": Decimal("0.91"),
                "source": "manual",
            },
            {
                "as_of": date(2026, 6, 22),
                "base_currency": "CNY",
                "quote_currency": "USD",
                "rate": Decimal("7.18"),
                "source": "manual",
            },
        ]
    )

    payload = build_latest_fx_rates_payload(cur, base_currency="CNY")

    assert payload["as_of"] == "2026-06-22"
    assert payload["rates"][0]["as_of"] == "2026-05-31"
    assert payload["rates"][1]["as_of"] == "2026-06-22"
