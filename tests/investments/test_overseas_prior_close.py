"""Use the preceding exchange session as the basis for an overseas daily return."""
import os
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from domain.investments.holdings.holdings_view import _mark_missing_prior_price, _previous_price_basis_join


def test_same_session_precision_intraday_flat_and_missing_session():
    dsn = os.getenv("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires explicitly isolated PostgreSQL")
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""CREATE TEMP TABLE pams_ohlcv_daily
            (symbol text, market text, trade_date date, close numeric) ON COMMIT DROP""")
        cur.execute("""CREATE TEMP TABLE pams_holdings_snapshot
            (account_code text, symbol text, name text, currency text, as_of date, current_price numeric,
             quantity numeric, market_value numeric) ON COMMIT DROP""")
        cur.execute("""INSERT INTO pams_ohlcv_daily VALUES
            ('CLOSE','YF','2026-11-25',100), ('CLOSE','YF','2026-11-27',102.69000001),
            ('LIVE','YF','2026-11-24',97), ('LIVE','YF','2026-11-25',100),
            ('FLAT','YF','2026-11-25',100), ('FLAT','YF','2026-11-27',100),
            ('GAP','YF','2026-11-24',97), ('SNAP','YF','2026-11-24',97)""")
        cur.execute("""INSERT INTO pams_holdings_snapshot VALUES
            ('ibkr','SNAP','SNAP','USD','2026-11-25',100,1,100)""")
        cur.execute(f"""WITH h AS (
            SELECT symbol, symbol AS name, price AS current_price, '美股' AS major_category,
                   'USD' AS currency, 'ibkr' AS account_code, 1 AS quantity, price AS market_value,
                   DATE '2026-11-25' AS as_of, DATE '2026-11-27' AS mark_day
            FROM (VALUES ('CLOSE',102.69), ('LIVE',103), ('FLAT',100),
                         ('GAP',102), ('SNAP',101)) marks(symbol,price))
            SELECT h.*, mark_day AS snapshot_as_of, previous_price,
                   previous_as_of AS _previous_price_date, h.current_price - prev_basis.previous_price AS daily_pnl_cny
            FROM h CROSS JOIN (SELECT NULL::numeric AS close) prev_px
            {_previous_price_basis_join('h.mark_day')}""")
        rows = {r["symbol"]: r for r in _mark_missing_prior_price(cur.fetchall())}
    assert rows["CLOSE"]["daily_pnl_cny"] == Decimal("2.69")
    assert rows["LIVE"]["daily_pnl_cny"] == 3
    assert rows["FLAT"]["daily_pnl_cny"] == 0
    assert rows["SNAP"]["daily_pnl_cny"] == 1
    assert rows["GAP"]["previous_price"] is None
    assert rows["GAP"]["daily_pnl_cny"] is None
    assert rows["GAP"]["daily_return_status"] == "pending"
