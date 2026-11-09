"""IBKR return readers against reviewed report trades in the empty schema."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts import bootstrap_isolated_sources as bootstrap


def publish_reviewed_trade_report(cur):
    from collectors.ibkr_source_records import stage_trade_report
    from domain.imports.source_records_v2 import publish_trade_source, review_item

    raw = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"').encode()
    staged = stage_trade_report(cur, report_bytes=raw, expected_source_account='U_TEST')
    source_id = UUID(staged['source_id'])
    cur.execute('SELECT source_item_id, parsed_fields FROM source_items WHERE source_id=%s', (str(source_id),))
    item = cur.fetchone()
    revision = review_item(cur, source_id=source_id, item_id=item['source_item_id'],
                           expected_revision=staged['revision'], parsed_fields=item['parsed_fields'],
                           review_status='confirmed')
    assert publish_trade_source(cur, source_id=source_id, expected_revision=revision)['published'] == 1


def test_ibkr_history_uses_only_verified_trade_facts_and_marks_coverage_unknown(empty_source_db,monkeypatch):
    from domain.investments import investment_return_ibkr as service
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            publish_reviewed_trade_report(cur)
        conn.commit()
    monkeypatch.setattr(service,'_fetch_yfinance_closes',lambda *a,**k:({'TEST':{day+timedelta(days=1):Decimal('200')}},[]))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
    sample=day+timedelta(days=1)
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            values,costs,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
            assert values[sample]==Decimal('400') and costs[sample]==Decimal('341.25')
            assert 'ibkr_history_coverage_unverified' in warnings
            absent,absent_cost,_=service._ibkr_full_trade_return_series(
                cur,dates=[sample],base_currency='CNY',fx_timelines={'USD':[(day,Decimal('7'))]})
            assert absent[sample] is None and absent_cost[sample] is None
            converted,converted_cost,_=service._ibkr_full_trade_return_series(
                cur,dates=[sample],base_currency='CNY',fx_timelines={'USD':[(sample,Decimal('7'))]})
            assert converted[sample]==Decimal('2800') and converted_cost[sample]==Decimal('2388.75')
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
        cur.execute("UPDATE pams_investment_trade_fact SET quantity=3 WHERE broker='ibkr'")
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _,_,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
            assert 'ibkr_history_missing:'+sample.isoformat() in warnings


def test_development_ibkr_history_uses_exact_local_market_prices_without_network(empty_source_db, monkeypatch):
    import main
    from domain.investments import investment_return_ibkr as service
    from endpoints import prices
    day = date(2026, 10, 22)
    first = day + timedelta(days=1)
    missing = day + timedelta(days=2)
    future = day + timedelta(days=3)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_reviewed_trade_report(cur)
            cur.execute("""INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close)
                VALUES ('TEST','YF',%s,200), ('TEST','CN_A',%s,999), ('TEST','YF',%s,300)""",
                (first, missing, future))
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
    monkeypatch.setattr(service, '_fetch_yfinance_closes', lambda *a, **k: pytest.fail('unexpected external fetch'))
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            values, costs, warnings = service._ibkr_full_trade_return_series(
                cur, dates=[first, missing], base_currency='USD', fx_timelines={})
            assert values[first] == Decimal('400') and costs[first] == Decimal('341.25')
            assert values[missing] is None and costs[missing] == Decimal('341.25')
            assert 'ibkr_history_missing:' + missing.isoformat() in warnings
            assert not any(warning.startswith('ibkr_fx_missing:') for warning in warnings)
            fx_values, fx_costs, fx_warnings = service._ibkr_full_trade_return_series(
                cur, dates=[missing], base_currency='CNY', fx_timelines={})
            assert fx_values[missing] is None and fx_costs[missing] is None
            assert 'ibkr_fx_missing:' + missing.isoformat() in fx_warnings
            converted_values, converted_costs, _ = service._ibkr_full_trade_return_series(
                cur, dates=[missing], base_currency='CNY',
                fx_timelines={'USD': [(missing, Decimal('7'))]})
            assert converted_values[missing] is None and converted_costs[missing] == Decimal('2388.75')
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**empty_source_db))
    with TestClient(main.app) as web:
        response = web.get('/api/prices/series', params={'symbols': 'TEST', 'market': 'US',
            'start': first.isoformat(), 'end': missing.isoformat(), 'normalize': 'false'})
        assert response.status_code == 200, response.text
        curve = response.json()['series'][0]
        assert curve['market'] == 'YF' and curve['coverage'] == 'ok' and curve['points'] == 1
        assert curve['rows'][0]['date'] == first.isoformat()
        empty = web.get('/api/prices/series', params={'symbols': 'TEST', 'market': 'US',
            'start': missing.isoformat(), 'end': missing.isoformat()})
        assert empty.status_code == 200, empty.text
        assert empty.json()['series'][0]['coverage'] == 'missing'


def test_return_http_reads_verified_ibkr_facts_when_isolated_writes_are_disabled(empty_source_db,monkeypatch):
    import main
    from domain.investments import investment_return_ibkr as service
    day=date(2026,10,22)
    sample=day+timedelta(days=1)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            publish_reviewed_trade_report(cur)
        conn.commit()
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','false')
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main.accounts_endpoint,'_overview_payload',lambda cur=None, **kwargs:{'accounts':[]})
    monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:sample)
    monkeypatch.setattr(main,'_investment_today_label_date',lambda:sample)
    monkeypatch.setattr(main,'_build_section_return_series_payload_from_snapshot',lambda *a,**k:None)
    monkeypatch.setattr(main,'_build_investment_return_series_payload_from_snapshot',lambda *a,**k:None)
    monkeypatch.setattr(main,'_apply_section_holdings_pnl_overlay',lambda cur,section,payload:payload)
    monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda *a,**k:None)
    monkeypatch.setattr(main,'_build_summary2_latest_payload_for_request',lambda cur:{})
    monkeypatch.setattr(main,'_apply_published_valuation_state_to_return_series_payload',lambda payload,state:payload)
    monkeypatch.setattr(main,'_apply_summary_pnl_to_return_series_payload',lambda payload,summary:payload)
    monkeypatch.setattr(service,'_fetch_yfinance_closes',lambda *a,**k:({'TEST':{sample:Decimal('200')}},[]))
    def build(cur,**kwargs):
        values,costs,warnings=service._ibkr_full_trade_return_series(cur,dates=[sample],base_currency='USD',fx_timelines={})
        cur.execute("SELECT current_setting('transaction_read_only') AS readonly,current_setting('transaction_isolation') AS isolation")
        mode=cur.fetchone()
        return {'value':str(values[sample]),'cost':str(costs[sample]),'warnings':warnings,'mode':mode}
    monkeypatch.setattr(main,'_build_section_return_series_payload',build)
    monkeypatch.setattr(main,'_build_investment_return_series_payload',build)
    with TestClient(main.app) as web:
        for route in ('/investment/section-return-series?section=us_stock&start=2026-10-23',
                      '/investment/return-series?start=2026-10-23'):
            response=web.get(route)
            assert response.status_code==200,response.text
            result=response.json()
            assert Decimal(result['value'])==Decimal('400') and Decimal(result['cost'])==Decimal('341.25')
            assert result['mode']=={'readonly':'on','isolation':'repeatable read'}
            assert 'ibkr_history_coverage_unverified' in result['warnings']
