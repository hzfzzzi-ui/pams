"""The unified market table preserves the independent observation grains."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/api"))

from domain.investments.daily_return_state import upsert_quote_snapshot
from domain.market.fund_nav_estimates import (
    existing_estimate,
    upsert_asset_market_daily_nav,
    upsert_estimate_row,
)
from scripts.bootstrap_development_database import initialize_development_database
from tests.database.test_development_bootstrap import fresh_development_database  # noqa: F401


def test_observation_types_preserve_idempotency_and_fund_anchor(fresh_development_database):
    params, _ = fresh_development_database
    day = date(2026, 1, 2)
    with psycopg2.connect(**params) as conn:
        initialize_development_database(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for retired in ("pams_asset_market_daily", "pams_market_quote_snapshot", "pams_fund_nav_estimates"):
                cur.execute("SELECT to_regclass(%s) AS name", ("public." + retired,))
                assert cur.fetchone()["name"] is None
            quote = {
                "quote_date": day, "asset_id": None, "symbol": "011613", "name": "synthetic",
                "market": "基金", "currency": "CNY", "provider": "synthetic",
                "source": "synthetic", "source_key": "synthetic:011613", "quote_session": "nav",
                "return_status": "estimated", "price": Decimal("1.25"), "price_time": None,
                "base_currency": "CNY", "fx_rate": None, "source_hash": None, "raw_json": {},
            }
            first = upsert_quote_snapshot(cur, quote)
            second = upsert_quote_snapshot(cur, {**quote, "price": Decimal("1.27")})
            assert first["quote_id"] == second["quote_id"]
            assert second["quote_date"] == day and second["price"] == Decimal("1.27")
            upsert_asset_market_daily_nav(
                cur, holding={"symbol": "011613", "name": "synthetic"},
                as_of=day, unit_nav=Decimal("1.20"), estimated_unit_nav=Decimal("1.25"),
                source="synthetic", raw={"test": True},
            )
            fund = {
                "as_of": day, "account_code": "synthetic-account", "symbol": "011613",
                "name": "synthetic", "confirmed_nav_date": date(2026, 1, 1),
                "confirmed_unit_nav": Decimal("1.20"), "confirmed_growth_pct": None,
                "estimate_time": None, "estimated_unit_nav": Decimal("1.25"),
                "estimated_growth_pct": None, "anchor_as_of": date(2026, 1, 1),
                "anchor_market_value": Decimal("1200"), "anchor_unit_nav": Decimal("1.20"),
                "estimated_market_value": Decimal("1250"), "estimated_delta": Decimal("50"),
                "estimated_delta_pct": Decimal("4.166667"), "status": "estimated",
                "note": None, "raw_json": Json({"test": True}),
            }
            upsert_estimate_row(cur, fund)
            upsert_estimate_row(cur, {**fund, "estimated_market_value": Decimal("1260")})
            estimate = existing_estimate(cur, as_of=day, account_code="synthetic-account",
                                         symbol="011613", name="synthetic")
            assert estimate["anchor_market_value"] == Decimal("1200")
            assert estimate["estimated_market_value"] == Decimal("1260")
            assert estimate["confirmed_unit_nav"] == Decimal("1.20")
            cur.execute("SELECT observation_type, COUNT(*) AS count FROM market_observations GROUP BY observation_type")
            counts = {row["observation_type"]: row["count"] for row in cur.fetchall()}
            assert counts == {"daily": 1, "quote": 2, "fund": 1}
            cur.execute("SAVEPOINT invalid_quote")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO market_observations
                    (observation_type, as_of, provider, source, source_key, return_status)
                    VALUES ('quote', %s, 'test', 'test', 'bad', 'not-a-status')""", (day,))
            cur.execute("ROLLBACK TO SAVEPOINT invalid_quote")
