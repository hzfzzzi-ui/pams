"""Unknown return evidence must survive snapshot storage and HTTP reads."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.investments.holding_return_snapshot import (
    build_investment_return_series_payload_from_snapshot,
    build_section_return_series_payload_from_snapshot,
    upsert_investment_return_series_snapshot,
)


def test_snapshot_refresh_does_not_overwrite_published_portfolio(empty_source_db, monkeypatch):
    from domain.investments import portfolio_valuation_state as portfolio

    day = date(2026, 10, 30)
    state = {'valuation_date': day, 'cycle': 'manual_publish', 'base_currency': 'CNY',
             'status': 'published', 'data_completeness': 'complete',
             'net_liquidation_value': Decimal('900'), 'assets_cny': Decimal('1000'),
             'liabilities_cny': Decimal('100'), 'gross_exposure_cny': Decimal('1000'),
             'investment_value_cny': Decimal('900'), 'cash_cny': Decimal('0'),
             'daily_pnl_cny': None, 'total_pnl_cny': Decimal('20'), 'source_run_id': None,
             'source_hash': 'a' * 64, 'raw_json': {}}
    monkeypatch.setattr(portfolio, '_build_current_state', lambda *a, **kw: dict(state))
    payload = {'rows': [{'date': day.isoformat(), 'market_value': '100', 'cost': '80',
                         'position_pnl': '20', 'realized_income': '0', 'total_pnl': '20',
                         'return_rate': '0.25', 'daily_return_rate': '0',
                         'time_weighted_return_rate': '0.25'}]}
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            published = portfolio.publish_current_portfolio_valuation_state(cur)
            assert published['net_liquidation_value'] == Decimal('900')
            assert 'result_type' not in published
            upsert_investment_return_series_snapshot(cur, payload=payload, base_currency='CNY')
            payload['rows'][0]['market_value'] = '150'
            upsert_investment_return_series_snapshot(cur, payload=payload, base_currency='CNY')
            cur.execute('SELECT result_type,market_value,net_liquidation_value FROM valuation_results ORDER BY result_type')
            assert [(row['result_type'], row['market_value'], row['net_liquidation_value']) for row in cur.fetchall()] == [
                ('holding_return', Decimal('150'), None), ('portfolio_published', None, Decimal('900'))]
            assert portfolio.latest_published_portfolio_valuation_state(cur)['net_liquidation_value'] == Decimal('900')
            cur.execute('SAVEPOINT result_type_guard')
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute("UPDATE valuation_results SET result_type='portfolio_published' WHERE result_type='holding_return'")
            cur.execute('ROLLBACK TO SAVEPOINT result_type_guard')


def test_unknown_refresh_http_keeps_null_then_complete_zero_is_usable(empty_source_db, monkeypatch):
    import main
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main.accounts_endpoint, '_overview_payload', lambda cur=None: {'accounts': []})
    monkeypatch.setattr(main, '_investment_return_view_as_of', lambda _cur: day)
    monkeypatch.setattr(main, '_investment_today_label_date', lambda: day)
    monkeypatch.setattr(main, '_latest_published_portfolio_valuation_state', lambda *_a, **_k: None)
    monkeypatch.setattr(main, '_build_summary2_latest_payload_for_request', lambda _cur: {})
    monkeypatch.setattr(main, '_apply_published_valuation_state_to_return_series_payload', lambda payload, _state: payload)
    monkeypatch.setattr(main, '_apply_summary_pnl_to_return_series_payload', lambda payload, _summary: payload)
    unknown = {'start': day.isoformat(), 'end': day.isoformat(), 'status': 'unavailable',
               'reason': 'fx_rate_unknown', 'coverage_notes': [{'reason': 'fx_rate_unknown'}],
               'rows': [{'date': day.isoformat(), 'market_value': '100', 'cost': None,
                         'position_pnl': None, 'realized_income': None, 'total_pnl': None,
                         'return_rate': None, 'daily_return_rate': None,
                         'time_weighted_return_rate': None, 'reported_return_adjustment': None}]}
    monkeypatch.setattr(main, '_build_investment_return_series_payload', lambda *_a, **_k: unknown)
    with TestClient(main.app) as web:
        response = web.post('/investment/return-series/snapshot?start=2026-10-30')
        assert response.status_code == 200, response.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM valuation_results WHERE result_type='holding_return' AND section='total'")
            row = cur.fetchone()
            assert row['market_value'] == Decimal(100)
            for key in ('cost','position_pnl','realized_income','total_pnl','return_rate','daily_return_rate','time_weighted_return_rate'):
                assert row[key] is None, (key, row[key])
            assert row['data_completeness'] == 'partial' and row['raw_json']['reason'] == 'fx_rate_unknown'
            result = build_investment_return_series_payload_from_snapshot(
                cur, start_date=day, interval='daily', base_currency='CNY', view_as_of=day,
                today_label_date=day, require_data_completeness=False)
            assert result['status'] == 'unavailable' and result['reason'] == 'fx_rate_unknown'
            assert result['rows'][0]['market_value'] == '100.0000000000'
            assert result['rows'][0]['cost'] is None and result['rows'][0]['return_rate'] is None
            assert result['rows'][0]['reported_return_adjustment'] is None
            assert result['today_window']['total_return'] is None
        response = web.get('/investment/return-series?start=2026-10-30')
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'unavailable' and response.json()['rows'][0]['cost'] is None

        # Old writer generated zeros and falsely marked complete; original source
        # evidence still proves those zeros were missing values.
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""UPDATE valuation_results SET cost=0,position_pnl=0,realized_income=0,
                total_pnl=0,return_rate=0,daily_return_rate=0,time_weighted_return_rate=0,
                data_completeness='complete' WHERE result_type='holding_return' AND section='total'""")
            no_cache = build_investment_return_series_payload_from_snapshot(
                cur, start_date=day, interval='daily', base_currency='CNY', view_as_of=day,
                require_data_completeness=True)
            assert no_cache is None
            partial = build_investment_return_series_payload_from_snapshot(
                cur, start_date=day, interval='daily', base_currency='CNY', view_as_of=day,
                today_label_date=day, require_data_completeness=False)
            assert partial['rows'][0]['cost'] is None and partial['rows'][0]['total_pnl'] is None
            assert partial['today_window']['total_return'] is None
        response = web.get('/investment/return-series?start=2026-10-30')
        assert response.json()['status'] == 'unavailable' and response.json()['rows'][0]['cost'] is None

        known = {'start': day.isoformat(), 'end': day.isoformat(), 'rows': [{
            'date': day.isoformat(), 'market_value': '100', 'cost': '100', 'position_pnl': '0',
            'realized_income': '0', 'total_pnl': '0', 'return_rate': '0', 'daily_return_rate': '0',
            'time_weighted_return_rate': '0'}]}
        monkeypatch.setattr(main, '_build_investment_return_series_payload', lambda *_a, **_k: known)
        response = web.post('/investment/return-series/snapshot?start=2026-10-30')
        assert response.status_code == 200, response.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM valuation_results WHERE result_type='holding_return' AND section='total'")
            row = cur.fetchone()
            assert row['data_completeness'] == 'complete' and row['total_pnl'] == Decimal(0)
        response = web.get('/investment/return-series?start=2026-10-30')
        assert response.status_code == 200, response.text
        assert response.json()['method'] == 'holding_return_snapshot'
        assert Decimal(response.json()['rows'][0]['total_pnl']) == 0


def test_unknown_section_does_not_turn_into_total_daily_gain(empty_source_db):
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            total = {'rows': [{'date': day.isoformat(), 'market_value': '100', 'cost': '100',
                     'position_pnl': '0', 'realized_income': '0', 'total_pnl': '0', 'return_rate': '0',
                     'daily_return_rate': '0', 'time_weighted_return_rate': '0'}]}
            section = {'status': 'unavailable', 'reason': 'fx_rate_unknown', 'rows': [{
                'date': day.isoformat(), 'market_value': '100', 'net_flow': None, 'cumulative_capital': None,
                'capital_return_rate': None, 'daily_return_rate': None,
                'time_weighted_return_rate': None, 'display_return_rate': None}]}
            assert upsert_investment_return_series_snapshot(cur, payload=total, base_currency='CNY') == 1
            assert upsert_investment_return_series_snapshot(cur, payload=section, base_currency='CNY', section='us_stock') == 1
            payload = build_section_return_series_payload_from_snapshot(
                cur, section='us_stock', start_date=day, interval='daily', base_currency='CNY',
                view_as_of=day, today_label_date=day)
            assert payload['status'] == 'unavailable' and payload['rows'][0]['daily_return_rate'] is None
            assert payload['today_window']['total_return'] is None
            payload = build_investment_return_series_payload_from_snapshot(
                cur, start_date=day, interval='daily', base_currency='CNY', view_as_of=day, today_label_date=day)
            assert payload['today_window']['total_return'] is None
            assert payload['today_window']['status'] == 'unavailable'
            # Falsely completed old rows are still rejected when their frozen
            # original section data proves individual inputs were missing.
            cur.execute("""UPDATE valuation_results SET net_flow=0,cumulative_capital=0,
                capital_return_rate=0,daily_return_rate=0,time_weighted_return_rate=0,
                display_return_rate=0,data_completeness='complete' WHERE result_type='holding_return' AND section='us_stock'""")
            payload = build_investment_return_series_payload_from_snapshot(
                cur, start_date=day, interval='daily', base_currency='CNY', view_as_of=day, today_label_date=day)
            assert payload['today_window']['total_return'] is None
            assert payload['today_window']['status'] == 'unavailable'


def test_real_unknown_cost_flows_through_snapshot_http(empty_source_db, monkeypatch):
    import main
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,as_of)
                VALUES ('unknown-cost','SYNTH','tonghuashun','资产','CNY','A股','A股',100,NULL,10,%s)""", (day,))
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main.accounts_endpoint, '_overview_payload', lambda cur=None: {'accounts': []})
    monkeypatch.setattr(main, '_investment_return_view_as_of', lambda _cur: day)
    monkeypatch.setattr(main, '_investment_today_label_date', lambda: day)
    monkeypatch.setattr(main, '_reported_returns_cfg', lambda: {})
    monkeypatch.setattr(main, '_owner_ratio_case_sql', lambda _alias: ('1', []))
    monkeypatch.setattr(main, '_account_owner_ratio', lambda _account: Decimal(1))
    monkeypatch.setattr(main, '_account_initial_capital_cny', lambda _account: None)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_latest_published_portfolio_valuation_state', lambda *_a, **_k: None)
    monkeypatch.setattr(main, '_build_summary2_latest_payload_for_request', lambda _cur: {})
    monkeypatch.setattr(main, '_apply_published_valuation_state_to_return_series_payload', lambda payload, _state: payload)
    monkeypatch.setattr(main, '_apply_summary_pnl_to_return_series_payload', lambda payload, _summary: payload)
    with TestClient(main.app) as web:
        live = web.get('/investment/return-series?start=2026-10-30')
        assert live.status_code == 200, live.text
        assert live.json()['status'] == 'unavailable' and live.json()['rows'][0]['return_rate'] is None
        refresh = web.post('/investment/return-series/snapshot?start=2026-10-30')
        assert refresh.status_code == 200, refresh.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cost,total_pnl,return_rate,data_completeness FROM valuation_results WHERE result_type='holding_return'")
            row=cur.fetchone()
            assert row['cost'] is None and row['total_pnl'] is None and row['return_rate'] is None
            assert row['data_completeness'] == 'partial'
        result = web.get('/investment/return-series?start=2026-10-30')
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'unavailable' and result.json()['rows'][0]['return_rate'] is None
