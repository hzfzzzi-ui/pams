"""Use only the explicitly marked disposable PostgreSQL server and synthetic data."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))

from domain.investments.holding_return_display import attach_recent_nav_returns
from domain.investments.holdings_view import build_holdings_latest_payload
from domain.investments.portfolio_valuation_state import build_portfolio_valuation_state_from_parts
from domain.market.fund_nav_estimates import upsert_asset_market_daily_nav
from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_known_investment_subtotal_remains_separate_from_incomplete_nav(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('synthetic','Synthetic')")
            for name, kind, value in [('position', '资产', Decimal('200')),
                                      ('loan', '负债', Decimal('70'))]:
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (account_code,name,symbol,asset_liability,currency,major_category,minor_category,
                     as_of,market_value,cost,quantity,current_price)
                    VALUES ('synthetic',%s,%s,%s,'CNY','ESOP','ESOP',%s,%s,%s,1,%s)""",
                    (name, name, kind, date(2026, 1, 1), value, value, value))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT account_code, asset_liability, market_value, as_of FROM pams_holdings_snapshot")
            positions = [{'account_code': row['account_code'],
                          'asset_type': 'liability' if row['asset_liability'] == '负债' else 'esop',
                          'market_value_cny': -row['market_value'] if row['asset_liability'] == '负债' else row['market_value'],
                          'as_of': row['as_of'], 'snapshot_scope_status': 'partial'} for row in cur.fetchall()]
            result = build_portfolio_valuation_state_from_parts(account_balances=[], investment_positions=positions)
            assert result['net_liquidation_value'] is None
            assert result['known_investment_value_cny'] == Decimal('130')


def test_confirmed_nav_return_survives_rollover_without_fact_changes(empty_source_db):
    previous, close, active = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for account, symbol, source in [('fund_a', '000001', 'tushare_fund_nav'),
                                             ('fund_b', '000002', 'eastmoney_f10')]:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,%s)", (account, account))
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (account_code,name,symbol,asset_liability,currency,major_category,minor_category,
                     as_of,market_value,cost,quantity,current_price)
                    VALUES (%s,%s,%s,'资产','CNY','A股','A股基金',%s,100,100,10,10)""",
                    (account, symbol, symbol, previous))
                holding = dict(account_code=account, symbol=symbol, name=symbol)
                for day, price in [(previous, Decimal(10)), (close, Decimal(11))]:
                    upsert_asset_market_daily_nav(cur, holding=holding, as_of=day,
                                                 unit_nav=price, source=source, raw={})
                    cur.execute("""INSERT INTO market_observations
                        (observation_type,as_of,account_code,symbol,name,confirmed_nav_date,
                         confirmed_unit_nav,status) VALUES ('fund',%s,%s,%s,%s,%s,%s,'confirmed')""",
                        (day, account, symbol, symbol, day, price))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            def read(day):
                return build_holdings_latest_payload(cur, limit=500, include_closed=False,
                    view_as_of=day, daily_pnl_start_as_of=day, excluded_accounts=[],
                    owner_ratios={}, default_owner_ratio=Decimal(1))
            current = read(active)
            assert len(current['rows']) == 2
            assert all(row['daily_pnl_cny'] is None for row in current['rows'])
            result = attach_recent_nav_returns(current, active_date=active, load_date=read)
            for row in result['rows']:
                recent = row['recent_confirmed_return']
                assert recent['as_of'] == str(close)
                assert Decimal(recent['fields']['daily_pnl_cny']) == 10
                assert row['daily_pnl_cny'] is None
                assert Decimal(row['market_value_cny']) == 110
            cur.execute('SHOW transaction_read_only')
            assert cur.fetchone()['transaction_read_only'] == 'on'
            cur.execute('SELECT as_of,quantity,cost,current_price FROM pams_holdings_snapshot')
            assert all(row['as_of'] == previous and row['quantity'] == 10 and row['cost'] == 100
                       and row['current_price'] == 10 for row in cur.fetchall())
