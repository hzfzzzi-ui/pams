from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.reference_data import build_categories_payload  # noqa: E402


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


def test_build_categories_payload_groups_minor_categories_by_major():
    cur = FakeCursor(
        [
            {"exists": True},
            [
                {"major_category": "A股", "minor_category": "A股个股"},
                {"major_category": "A股", "minor_category": "A股基金"},
                {"major_category": "美股", "minor_category": "美股基金"},
            ],
        ]
    )

    payload = build_categories_payload(cur)

    assert payload == {
        "categories": {
            "A股": ["A股个股", "A股基金"],
            "美股": ["美股基金"],
        }
    }
    assert "ORDER BY major_category, minor_category" in cur.executed[1][0]


def test_build_categories_payload_handles_empty_result():
    assert build_categories_payload(FakeCursor([{"exists": True}, []])) == {"categories": {}}


def test_build_categories_payload_falls_back_to_holding_snapshot():
    cur = FakeCursor(
        [
            {"exists": False},
            [
                {"major_category": "A股", "minor_category": "A股"},
                {"major_category": "美股", "minor_category": "美股基金"},
            ],
        ]
    )

    payload = build_categories_payload(cur)

    assert payload == {"categories": {"A股": ["A股"], "美股": ["美股基金"]}}
    assert "pams_holdings_snapshot" in cur.executed[1][0]
