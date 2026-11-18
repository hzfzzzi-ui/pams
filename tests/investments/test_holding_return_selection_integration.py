"""Exercise selected-holding return against a disposable PostgreSQL database."""
from datetime import date
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor

from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from domain.investments.position_performance import build_position_return_series


def test_selected_position_return_uses_only_the_selected_asset(empty_source_db):
    first, second = date(2026, 1, 5), date(2026, 1, 6)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_account','Synthetic')")
            for asset, symbol in [('asset_a', '600001'), ('asset_b', '600002')]:
                cur.execute("""INSERT INTO pams_asset_master(asset_id, canonical_name, primary_symbol, asset_type)
                               VALUES (%s,%s,%s,'stock')""", (asset, asset, symbol))
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (account_code,asset_id,name,symbol,asset_liability,currency,major_category,minor_category,
                     as_of,market_value,cost,quantity,current_price)
                    VALUES ('test_account',%s,%s,%s,'资产','CNY','A股','A股个股',%s,1000,1000,100,10)""",
                    (asset, asset, symbol, first))
                for day, price in [(first, 10), (second, 11 if asset == 'asset_a' else 20)]:
                    cur.execute("""INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close)
                                   VALUES (%s,'CN_A',%s,%s)""", ('sh' + symbol, day, price))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = build_position_return_series(
                cur, start=first, end=second, interval='daily', base_currency='CNY',
                owner_ratio=lambda _: Decimal(1), excluded_accounts=(),
                selection=[('test_account', 'asset_a')])
            assert Decimal(result['rows'][-1]['return_rate']) == Decimal('0.1')
            assert Decimal(result['rows'][-1]['market_value']) == Decimal('1100')
