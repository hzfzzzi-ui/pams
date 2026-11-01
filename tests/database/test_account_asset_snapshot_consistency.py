"""Account anchors and asset reads share a consistent database transaction."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
from threading import Event, Thread
import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
import pytest

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


@pytest.mark.parametrize('method,path,conflict',[
    ('GET', '/api/account-performance/demo', False),
    ('GET', '/investment/section-return-series?section=a_stock&start=2026-01-01', False),
    ('GET', '/investment/return-series?start=2026-01-01', False),
    ('GET', '/nav/weekly?start=2026-01-01&latest=true', False),
    ('POST', '/investment/section-return-series/snapshot?section=a_stock&start=2026-01-01', False),
    ('POST', '/investment/section-return-series/snapshot?section=a_stock&start=2026-01-01', True),
    ('POST', '/investment/return-series/snapshot?start=2026-01-01', False),
    ('POST', '/investment/return-series/snapshot?start=2026-01-01', True),
])
def test_account_overview_and_asset_read_same_snapshot(empty_source_db, monkeypatch, method, path, conflict):
    from endpoints import accounts
    from endpoints.compatibility_routes import create_compatibility_router
    from domain.brokers.ibkr import ibkr_daily_close_read

    day = date(2026, 1, 2)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('CREATE TABLE snapshot_probe (id int primary key, amount numeric not null)')
        cur.execute('CREATE TABLE snapshot_results (anchor numeric, fact numeric)')
        cur.execute('INSERT INTO snapshot_probe VALUES (1,100)')
    read_started, write_finished = Event(), Event()
    def writer():
        assert read_started.wait(5)
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE snapshot_probe SET amount=200 WHERE id=1')
        write_finished.set()
    thread = Thread(target=writer, daemon=True)
    thread.start()

    def account_rows(cur):
        cur.execute('SELECT amount FROM snapshot_probe WHERE id=1')
        assert cur.fetchone()['amount'] == 100
        read_started.set()
        assert write_finished.wait(5)
        return {'demo': {'account_code':'demo','base_currency':'CNY','display_name':'demo','active':True,'is_asset':True}}
    def holdings(cur):
        cur.execute('SELECT amount FROM snapshot_probe WHERE id=1')
        value = cur.fetchone()['amount']
        return {'demo': [{'currency':'CNY','market_value':value,'as_of':day,'asset_liability':'资产'}]}
    monkeypatch.setattr(accounts, '_load_accounts_config', lambda: {'accounts': {'demo':{
        'account_type':'brokerage','truth_source':'platform_snapshot','base_currency':'CNY',
        'owner_ratio':'1','include_in_nav':True}}})
    monkeypatch.setattr(accounts, '_load_bank_config', lambda: {})
    monkeypatch.setattr(accounts, '_fetch_db_accounts', account_rows)
    monkeypatch.setattr(accounts, '_fetch_statement_balances', lambda cur: {})
    monkeypatch.setattr(accounts, '_fetch_holding_balances', holdings)
    monkeypatch.setattr(accounts, '_fetch_daily_fx_rates_by_date', lambda cur, balances: {})
    monkeypatch.setattr(accounts, '_fetch_transfer_rollups', lambda cur: {})
    monkeypatch.setattr(accounts, '_fetch_latest_reviews', lambda cur: {})
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)

    def build(cur, **kwargs):
        cur.execute('SELECT amount FROM snapshot_probe WHERE id=1')
        fact = cur.fetchone()['amount']
        overview = kwargs.get('overview_payload')
        anchor = (overview['accounts'][0]['primary_balance']['amount'] if overview is not None
                  else kwargs.get('net_liquidation'))
        return {'overview':str(anchor), 'current_fact':str(fact), 'rows':[]}
    def save(cur, *, payload, **kwargs):
        cur.execute('INSERT INTO snapshot_results VALUES (%s,%s)', (payload['overview'],payload['current_fact']))
        if conflict:
            cur.execute('UPDATE snapshot_probe SET amount=300 WHERE id=1')
        return 1
    passthrough = lambda payload, *a, **kw: payload
    options = {name:(lambda *a,**kw:None) for name in inspect.signature(create_compatibility_router).parameters}
    options.update({
        'ANCHOR_NAV_DATE':day, 'accounts_endpoint':accounts,
        'get_conn':lambda:psycopg2.connect(**empty_source_db), 'json':lambda payload:payload,
        '_account_initial_capital_cny':lambda *a:None, '_account_owner_ratio':lambda *a:Decimal(1),
        '_daily_pnl_view_as_of':lambda:day, '_investment_today_label_date':lambda:day,
        '_investment_return_view_as_of':lambda cur:day,
        '_build_section_return_series_payload_from_snapshot':lambda *a,**kw:None,
        '_build_investment_return_series_payload_from_snapshot':lambda *a,**kw:None,
        '_build_account_performance_payload':build, '_build_section_return_series_payload':build,
        '_build_investment_return_series_payload':build, '_build_nav_weekly_payload':build,
        '_apply_reported_account_performance':passthrough,
        '_apply_section_holdings_pnl_overlay':lambda cur,section,payload:payload,
        '_apply_published_valuation_state_to_return_series_payload':passthrough,
        '_apply_summary_pnl_to_return_series_payload':passthrough,
        '_load_portfolio_accounts_config':lambda:{'accounts':{}}, '_load_account_balances':lambda *a:[],
        '_load_investment_positions':lambda *a,**kw:[], '_current_nav_bucket_from_parts':lambda **kw:None,
        '_current_nav_account_values_from_parts':lambda **kw:{}, '_excluded_holding_accounts':lambda:set(),
        '_build_summary2_latest_payload_for_request':lambda cur:{}, '_reported_returns_cfg':lambda:{},
        '_upsert_investment_return_series_snapshot':save,
    })
    router,_ = create_compatibility_router(**options)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as web:
        response = web.request(method,path)
        if conflict:
            assert response.status_code == 409,response.text
            with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM snapshot_results')
                assert cur.fetchone()[0] == 0
                cur.execute('SELECT amount FROM snapshot_probe WHERE id=1')
                assert cur.fetchone()[0] == 200
        else:
            assert response.status_code == 200,response.text
            if method == 'GET':
                assert Decimal(response.json()['overview']) == 100
                assert Decimal(response.json()['current_fact']) == 100
            else:
                with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
                    cur.execute('SELECT anchor,fact FROM snapshot_results')
                    assert cur.fetchone() == (Decimal(100),Decimal(100))
    thread.join(5)
    assert not thread.is_alive()
