from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.market.fx_rates import build_latest_fx_rates_payload  # noqa: E402
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: E402,F401


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

    payload = build_latest_fx_rates_payload(cur, base_currency="CNY", as_of=date(2026, 6, 1))

    assert set(payload) == {"as_of", "base_currency", "rates"}
    assert payload["as_of"] == "2026-05-31"
    assert payload["base_currency"] == "CNY"
    assert payload["rates"][0]["as_of"] == "2026-05-31"
    assert payload["rates"][0]["rate"] == "7.20"
    assert "DISTINCT ON (quote_currency)" in cur.executed[0][0]
    assert cur.executed[0][1] == ("CNY", date(2026, 6, 1))


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

    payload = build_latest_fx_rates_payload(cur, base_currency="CNY", as_of=date(2026, 6, 22))

    assert payload["as_of"] == "2026-06-22"
    assert payload["rates"][0]["as_of"] == "2026-05-31"
    assert payload["rates"][1]["as_of"] == "2026-06-22"


def test_latest_rates_filter_at_postgresql_boundary(empty_source_db):  # noqa: F811
    """An invalid latest quote must not obscure the newest valid historical quote."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from scripts.bootstrap_empty_database import bootstrap_paths

    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql_file = next(path for path in bootstrap_paths() if path.name == "004_fx_rates.sql")
        cur.execute(sql_file.read_text(encoding="utf-8"))
        cur.execute("""INSERT INTO pams_fx_rates_daily (as_of,base_currency,quote_currency,rate,source) VALUES
            ('2026-06-20','CNY','USD',7.2,'official'),
            ('2026-06-21','CNY','USD',8,NULL),
            ('2026-06-22','CNY','USD',9,'   '),
            ('2026-06-23','CNY','USD',0,'official'),
            ('2026-06-24','CNY','USD','NaN','official'),
            ('2026-06-26','CNY','USD',99,'official'),
            ('2026-06-19','CNY','HKD',0.9,'published'),
            ('2026-06-25','CNY','EUR',8,chr(9)),
            ('2026-06-25','CNY','GBP',-1,'official'),
            ('2026-06-25','USD','GBP',2,'other-base')""")
        payload = build_latest_fx_rates_payload(cur, base_currency="CNY", as_of=date(2026, 6, 25))
        assert payload == {"as_of": "2026-06-20", "base_currency": "CNY", "rates": [
            {"as_of": "2026-06-19", "base_currency": "CNY", "quote_currency": "HKD", "rate": "0.90000000", "source": "published"},
            {"as_of": "2026-06-20", "base_currency": "CNY", "quote_currency": "USD", "rate": "7.20000000", "source": "official"},
        ]}
        assert build_latest_fx_rates_payload(cur, base_currency="EUR", as_of=date(2026, 6, 25)) == {"as_of": None, "rates": []}
