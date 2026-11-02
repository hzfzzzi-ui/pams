"""Atomic source schema bootstrap for a specifically marked disposable database."""
import os
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from scripts import bootstrap_isolated_sources as bootstrap
from scripts.bootstrap_empty_database import BootstrapError


@pytest.fixture
def empty_source_db(monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN required')
    params = parse_dsn(dsn)
    assert params.get('host') in {'127.0.0.1', 'localhost', '::1'}
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    assert admin.server_version // 10000 == 16
    name = 'pams_import_test_' + uuid4().hex
    with admin.cursor() as cur:
        cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone()[0] == 'pams-disposable-schema-test'
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    try:
        yield {**params, 'dbname': name}
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


def test_empty_isolated_database_gets_sources_and_facts_atomically(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for table in ('source_records', 'source_items', 'source_templates', 'cashflow_classification', 'investment_trade_review',
                          'pams_cashflow_fact', 'pams_investment_trade_fact', 'pams_holdings_snapshot'):
                cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
                assert cur.fetchone()[0] is not None
            for legacy in ('source_batches', 'source_files', 'pams_source_row', 'pams_data_job_run', 'pams_data_sync_runs', 'pams_sync_run_log', 'pams_ibkr_source_link_repair_audit',
                           'pams_ibkr_trades', 'pams_ibkr_position_history', 'pams_investment_transactions',
                           'pams_tradingview_transactions',
                           'pams_external_transaction_source', 'pams_investment_trade_snapshot',
                           'pams_account_reconciliation_reviews', 'pams_asset_exposure_relation', 'pams_asset_daily_pnl',
                           'pams_asset_fixed_income_profile', 'pams_asset_real_asset_profile',
                           'pams_account_import_batch', 'pams_account_import_file',
                           'pams_account_import_template', 'pams_account_import_audit'):
                cur.execute('SELECT to_regclass(%s)', ('public.' + legacy,))
                assert cur.fetchone()[0] is None
            for table in ('asset_classifications', 'pams_fund_underlying_position',
                          'broker_processing_state', 'pams_fundamental_snapshot', 'pams_sentiment_state'):
                cur.execute("SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)", ('public.' + table,))
                assert cur.fetchone()[0] == 'r'
                cur.execute('SELECT to_regclass(%s)', ('pams_legacy.' + table,))
                assert cur.fetchone()[0] is None
            cur.execute("""SELECT count(*) FROM information_schema.columns WHERE table_schema='public'
                AND table_name IN ('pams_cashflow_fact','pams_investment_trade_fact','pams_holdings_snapshot')
                AND column_name='source_item_id'""")
            assert cur.fetchone()[0] == 3


def test_full_bootstrap_native_bank_http_reaches_account_ledger_and_sync(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domain.imports import account_import_statement as statement, account_import_store as store
    from domain.investments import portfolio_state
    from domain.brokers.ibkr import ibkr_daily_close_read, ibkr_performance
    from endpoints import account_import, accounts, ledger, sync_center

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_source_row')")
            assert cur.fetchone()[0] is None
            cur.execute("INSERT INTO pams_accounts (account_code,display_name,base_currency) VALUES ('bank_test','Synthetic','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    for module in (store, accounts, ledger, sync_center):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    account_cfg = {'accounts': {'bank_test': {'account_type':'funding','truth_source':'bank_statement',
        'manual_balance':{'amount':'50','currency':'CNY','as_of':(date.today()-timedelta(days=10)).isoformat()}}}}
    monkeypatch.setattr(accounts, '_load_accounts_config', lambda: account_cfg)
    monkeypatch.setattr(accounts, '_load_bank_config', lambda: {})
    monkeypatch.setattr(portfolio_state, '_load_accounts_config', lambda: {})
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    monkeypatch.setattr(ibkr_performance, 'published_report_status', lambda conn: {'status': 'missing', 'label': '缺数据',
        'statement_as_of': None, 'active_row_count': 0, 'expected_statement_as_of': None, 'last_attempt_status': None})
    monkeypatch.setattr(sync_center, 'read_ibkr_reconciliation', lambda: {'status': 'missing', 'label': '缺数据', 'details': ''})
    monkeypatch.setattr(sync_center, 'scheduler_health', lambda path: {'status': 'missing', 'label': '缺数据', 'jobs': {}})
    app = FastAPI()
    for router in (account_import.router, accounts.router, ledger.router, sync_center.router):
        app.include_router(router, prefix='/api')
    day = (date.today() - timedelta(days=2)).isoformat()
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        content = f'transaction_date,amount,currency,balance,summary\n{day},100.05,CNY,200.05,pay\n'.encode()
        response = web.post('/api/account-import/source-records?account_code=bank_test&source_name=synthetic&filename=bank.csv', content=content)
        assert response.status_code == 200, response.text
        path = '/api/account-import/source-records/' + response.json()['source_id']
        batch = web.get(path).json(); revision = batch['revision']
        balance = next(item for item in batch['items'] if item['review_status'] == 'excluded')
        forged_balance = {**balance['parsed_fields'], 'action': 'income'}
        response = web.put(path + '/items/' + balance['source_item_id'], json={
            'revision':revision, 'parsed_fields':forged_balance, 'review_status':'confirmed'})
        assert response.status_code == 200, response.text
        revision = response.json()['revision']
        check = web.post(path+'/cash-preview', json={'revision':revision,'mode':'all'}).json()
        assert check['counts']['invalid'] == 1 and not check['can_publish']
        response = web.put(path + '/items/' + balance['source_item_id'], json={
            'revision':revision, 'parsed_fields':balance['parsed_fields'], 'review_status':'excluded'})
        assert response.status_code == 200, response.text
        revision = response.json()['revision']
        for item in batch['items']:
            if item['review_status'] == 'excluded':
                continue
            response = web.put(path + '/items/' + item['source_item_id'], json={
                'revision':revision, 'parsed_fields':item['parsed_fields'], 'review_status':'confirmed'})
            assert response.status_code == 200, response.text
            revision = response.json()['revision']
        response = web.post(path+'/cash-preview', json={'revision':revision,'mode':'all'})
        assert response.status_code == 200, response.text
        check = response.json()
        assert check['can_publish'], check
        response = web.post(path+'/cash-publish', json={'revision':revision, 'mode':'all', 'preview_token':check['preview_token']})
        assert response.status_code == 200, response.text
        # Once facts exist, read-only consumers do not require the isolated
        # write switch merely to read a new database without the old table.
        monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
        for url in ('/api/accounts/overview', '/api/ledger/workbench', '/api/sync/center'):
            response = web.get(url)
            assert response.status_code == 200, (url, response.text)
            if url.endswith('/accounts/overview'):
                account = next(item for item in response.json()['accounts'] if item['account_code'] == 'bank_test')
                assert sum(item['row_count'] for item in account['statement_profiles']) == 1
                assert account['statement_balance'] is None
                assert account['primary_balance']['amount'] == '50'
            if url.endswith('/ledger/workbench'):
                assert any(item['account_code'] == 'bank_test' and float(item['amount']) == 100.05
                           for item in response.json()['recent_rows'])
            if url.endswith('/sync/center'):
                row = next(item for item in response.json()['rows'] if item['id'] == 'account-reconcile')
                assert row['rows'] == 1 and row['latest_date'] == day
                ibkr = next(item for item in response.json()['rows'] if item['id'] == 'ibkr-flex')
                assert ibkr['latest_time'] is None
        account_cfg['accounts']['bank_test'].pop('manual_balance')
        no_anchor = web.get('/api/accounts/overview').json()
        assert next(a for a in no_anchor['accounts'] if a['account_code']=='bank_test')['primary_balance'] is None
        # A changed fact is retained for diagnosis, but source counts and
        # summaries must not present it as verified or a complete zero.
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_cashflow_fact SET amount=amount+1 WHERE account_code='bank_test'")
        response = web.get('/api/accounts/overview')
        assert response.status_code == 200, response.text
        account = next(item for item in response.json()['accounts'] if item['account_code'] == 'bank_test')
        assert account['statement_profiles'][0]['source_evidence_status'] == 'unknown'
        assert account['statement_profiles'][0]['row_count'] is None
        assert account['primary_balance'] is None
        response = web.get('/api/sync/center')
        assert response.status_code == 200, response.text
        row = next(item for item in response.json()['rows'] if item['id'] == 'account-reconcile')
        assert row['status'] == 'needs_review' and row['rows'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_source_row'),count(*) FROM pams_cashflow_fact")
            assert cur.fetchone() == (None, 1)


def test_new_database_saves_and_reuses_source_mapping_without_legacy_tables(empty_source_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domain.imports import account_import_store as store, document_stage_v2
    from endpoints import account_import

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts (account_code,display_name,base_currency) VALUES ('tonghuashun','Synthetic','CNY') ON CONFLICT DO NOTHING")
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(document_stage_v2.document_kind, 'detect', lambda *args: {'kind':'trade','status':'detected'})
    monkeypatch.setattr(document_stage_v2, 'assess_template', lambda parsed, kind: 'matched')
    monkeypatch.setattr(store, 'parse', lambda content, filename, account, kind, mapping=None: {
        'origin': 'file', 'rows': [{'fields': {'note': 'mapped' if mapping else 'initial'},
                                   'source': {'row': 1}, 'issues': []}]})
    app = FastAPI()
    app.include_router(account_import.router, prefix='/api')
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        assert web.get('/api/account-import/capabilities').json()['source_record_schema_ready']
        path = '/api/account-import/source-records'
        first = web.post(path + '?account_code=tonghuashun&source_name=synthetic&filename=one.csv', content=b'first').json()
        source_path = path + '/' + first['source_id']
        assert web.get(source_path).json()['items'][0]['parsed_fields']['note'] == 'initial'
        response = web.put(source_path + '/mapping', json={'revision': first['revision'], 'mapping': {'symbol': 'ticker'}})
        assert response.status_code == 200, response.text
        assert web.get(source_path).json()['items'][0]['parsed_fields']['note'] == 'mapped'
        response = web.post(path + '?account_code=tonghuashun&source_name=synthetic&filename=two.csv', content=b'second')
        assert response.status_code == 200, response.text
        assert web.get(path + '/' + response.json()['source_id']).json()['items'][0]['parsed_fields']['note'] == 'mapped'
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT mapping,to_regclass('public.pams_account_import_template') FROM source_templates")
        assert cur.fetchone() == ({'symbol': 'ticker'}, None)


def test_fact_account_constraints_are_validated_and_reject_unknown_account(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT conrelid::regclass::text,convalidated FROM pg_constraint
                WHERE contype='f' AND confrelid='public.pams_accounts'::regclass
                  AND conrelid IN ('public.source_records'::regclass, 'public.pams_cashflow_fact'::regclass,
                                   'public.pams_investment_trade_fact'::regclass)""")
            assert dict(cur.fetchall()) == {'source_records': True, 'pams_cashflow_fact': True,
                                           'pams_investment_trade_fact': True}
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute("""INSERT INTO source_records(source_id,account_code,source_name,acquisition_method,document_type)
                    VALUES ('00000000-0000-0000-0000-000000000001','nonexistent','synthetic','manual','cash')""")
        conn.rollback()


def test_asset_api_without_ibkr_legacy_tables_keeps_missing_return_unknown(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from decimal import Decimal
    from fastapi.testclient import TestClient
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read

    today = date.today()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for table in ('pams_ibkr_trades', 'pams_ibkr_position_history'):
                cur.execute('SELECT to_regclass(%s), to_regclass(%s)', ('public.' + table, 'pams_legacy.' + table))
                assert cur.fetchone() == (None, None)
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of)
                VALUES ('synthetic-stock','SYN','ibkr','资产','USD','美股','美股基金',100,80,10,10,%s)""", (today,))
            cur.execute("""INSERT INTO pams_fx_rates_daily (as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic')""", (today,))
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: today)
    monkeypatch.setattr(main, '_view_as_of', lambda: today)
    monkeypatch.setattr(main, '_daily_pnl_view_as_of', lambda: today)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratios', lambda: {})
    monkeypatch.setattr(main, '_default_owner_ratio', lambda: Decimal('1'))
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    with TestClient(main.app) as web:
        core = web.get('/api/holdings/latest')
        assert core.status_code == 200, core.text
        assert float(next(item for item in core.json()['rows'] if item['symbol'] == 'SYN')['market_value']) == 100
        history = web.get('/holdings/history/美股')
        assert history.status_code == 200, history.text
        assert history.json()['history_evidence'] == {
            'status': 'snapshot_based', 'coverage': 'unknown',
            'reason': 'ibkr_position_history_unavailable',
        }
        for url in ('/holdings/latest', '/holdings/by-category/美股'):
            response = web.get(url)
            assert response.status_code == 200, (url, response.text)
            row = next(item for item in response.json()['rows'] if item['symbol'] == 'SYN')
            assert row.get('market_value_cny') is not None, (url, row)
            assert float(row['market_value_cny']) == 700
            assert row['daily_pnl'] is None and row['daily_pnl_cny'] is None
            assert row['daily_return_status'] == 'pending'
            assert row['daily_return_status_reason'] == 'missing_previous_price_evidence'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of)
                VALUES ('synthetic-stock','SYN','ibkr','资产','USD','美股','美股基金',90,80,10,9,%s)""", (today - timedelta(days=1),))
        for url in ('/holdings/latest', '/holdings/by-category/美股'):
            response = web.get(url)
            assert response.status_code == 200, (url, response.text)
            row = next(item for item in response.json()['rows'] if item['symbol'] == 'SYN')
            assert float(row['previous_price']) == 9
            assert float(row['daily_pnl']) == 10 and float(row['daily_pnl_cny']) == 70


def test_weekly_nav_does_not_treat_missing_ibkr_history_as_zero(empty_source_db, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments import valuation_nav as nav
    from domain.brokers.ibkr import ibkr_daily_close_read

    day = date(2026, 10, 30)
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            def nav_row():
                values = nav._ibkr_nav_series(cur, dates=[day], base_currency='CNY',
                    owner_ratio_case_sql=lambda alias: ('%s', [Decimal('1')]))
                return nav.normalize_nav_row(nav.build_weekly_nav_rows(
                    dates=[day], bank_series={day: {'assets': Decimal('50'),
                    'liabilities': Decimal('0'), 'net_worth': Decimal('50')}},
                    snapshot_series={}, a_share_series={}, ibkr_values=values)[0])
            absent = nav_row()
            assert absent['assets'] is None and absent['liabilities'] is None and absent['netWorth'] is None
            assert absent['ibkr_history_evidence'] == 'unavailable'

            # A terminal total alone does not prove the missing IBKR component.
            common = dict(start_date=day, terminal_date=day, base_currency='CNY',
                overview_payload={'accounts': []}, owner_ratio_case_sql=lambda alias: ('%s', [Decimal('1')]),
                account_owner_ratio=lambda account: Decimal('1'), interval='daily',
                terminal_nav_bucket={'assets': Decimal('150'), 'liabilities': Decimal('0'),
                                     'net_worth': Decimal('150')})
            missing = nav.build_nav_weekly_payload(cur, **common)['rows'][0]
            assert missing['netWorth'] is None and missing['ibkr_history_evidence'] == 'unavailable'
            proven = nav.build_nav_weekly_payload(cur, terminal_account_values_cny={'ibkr': Decimal('100')},
                                                 **common)['rows'][0]
            assert Decimal(proven['netWorth']) == 150
            assert proven['ibkr_history_evidence'] == 'current_portfolio_snapshot'


def test_unknown_cost_keeps_market_value_and_not_fake_profit(empty_source_db, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from fastapi.testclient import TestClient
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read

    today = date.today()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_tradingview_transactions')")
            assert cur.fetchone()[0] is None
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of)
                VALUES ('unknown-cost','600001','tonghuashun','资产','CNY','A股','A股',100,NULL,10,10,%s),
                       ('known-cost','600002','tonghuashun','资产','CNY','A股','A股',100,80,10,10,%s)""", (today,today))
            cur.execute("SELECT name,holding_pnl FROM pams_holdings_snapshot WHERE name IN ('unknown-cost','known-cost')")
            assert dict(cur.fetchall()) == {'unknown-cost': None, 'known-cost': Decimal('20')}
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','CNY',1)", (today,))
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: today)
    monkeypatch.setattr(main, '_daily_pnl_view_as_of', lambda: today)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratios', lambda: {})
    monkeypatch.setattr(main, '_default_owner_ratio', lambda: Decimal('1'))
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    with TestClient(main.app) as web:
        for url in ('/api/holdings/latest', '/holdings/latest', '/holdings/by-category/A股'):
            response = web.get(url)
            assert response.status_code == 200, response.text
            rows = {row['name']: row for row in response.json()['rows']}
            assert float(rows['unknown-cost']['market_value']) == 100
            assert rows['unknown-cost']['cost'] is None and rows['unknown-cost']['holding_pnl'] is None
            assert rows['unknown-cost']['cost_evidence'] == 'unavailable'
            assert float(rows['known-cost']['holding_pnl']) == 20
            if url != '/api/holdings/latest':
                assert rows['unknown-cost']['daily_pnl'] is None
                assert rows['unknown-cost']['daily_return_status_reason'] == 'missing_previous_price_or_confirmed_trade'
        summary = web.get('/api/holdings/summary')
        assert summary.status_code == 200, summary.text
        assert summary.json()['total']['value'] is None and float(summary.json()['known_value']) == 200
        assert summary.json()['total']['cost'] is None and summary.json()['total']['pnl'] is None
        accounts = web.get('/api/holdings/by-account')
        assert accounts.status_code == 200, accounts.text
        account = next(row for row in accounts.json()['by_account'] if row['account_code']=='tonghuashun')
        assert account['total_value_base'] is None and float(account['known_value_base']) == 200


def test_unknown_cost_invalidates_summary_and_published_total_pnl(empty_source_db):
    from datetime import date
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments.portfolio_summary import build_holdings_summary_payload
    from domain.investments.portfolio_valuation_state import build_portfolio_valuation_state_from_parts

    today = date.today()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,account_code,asset_liability,currency,major_category,minor_category,market_value,cost,as_of)
                VALUES ('unknown','tonghuashun','资产','CNY','A股','A股',100,NULL,%s),
                       ('known','tonghuashun','资产','CNY','A股','A股',100,80,%s)""", (today,today))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate) VALUES (%s,'CNY','CNY',1)", (today,))
            payload = build_holdings_summary_payload(cur, base_currency='CNY', view_as_of=today,
                                                    owner_ratio_sql='1', owner_ratio_params=())
            assert payload['total']['value'] is None and Decimal(payload['known_value']) == 200
            assert payload['total']['cost'] is None and payload['total']['pnl'] is None
            state = build_portfolio_valuation_state_from_parts(account_balances=[], investment_positions=[{
                'account_code':'tonghuashun','asset_id':'unknown','asset_type':'stock',
                'market_value_cny':Decimal('100'),'holding_pnl_cny':None,'as_of':today}])
            assert state['net_liquidation_value'] == Decimal('100')
            assert state['total_pnl_cny'] is None and state['data_completeness'] == 'partial'
            cur.execute("""INSERT INTO valuation_results
                (result_type,valuation_date,cycle,base_currency,status,data_completeness,total_pnl_cny,
                 net_liquidation_value,assets_cny,liabilities_cny,gross_exposure_cny,investment_value_cny,cash_cny)
                VALUES ('portfolio_published',%s,'synthetic','CNY','published','partial',NULL,0,0,0,0,0,0)
                RETURNING total_pnl_cny""", (today,))
            assert cur.fetchone()['total_pnl_cny'] is None


def test_tonghuashun_nav_unknown_tax_is_not_zero_and_known_net_cash_is_usable(empty_source_db):
    from datetime import date, timedelta
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments import valuation_nav as nav
    from domain.imports import source_records_v2 as source

    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            def publish(fields):
                sid, iid = uuid4(), uuid4()
                source.create_source(cur,source_id=sid,account_code='tonghuashun',source_name='synthetic',acquisition_method='manual',document_type='trade')
                source.add_item(cur,source_id=sid,item_id=iid,parse_method='manual',raw_fields=fields,parsed_fields=fields)
                source.review_item(cur,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                source.publish_trade_source(cur,source_id=sid,expected_revision=3)
            fields={'business_date':day.isoformat(),'symbol':'600001','currency':'CNY','trade_type':'buy',
                    'quantity':'10','price':'8','gross_amount':'80','fee_amount':'1','tax_amount':None,'net_cash':None}
            publish(fields)
            days = [day - timedelta(days=1), day]
            values = nav._tonghuashun_series_from_trade_fact(cur, dates=days, account_owner_ratio=lambda _: Decimal(1))
            assert values.unknown_dates == set(days) and values.cash_scope_unverified
            rows = nav.build_weekly_nav_rows(dates=days, bank_series={}, snapshot_series={},
                                             a_share_series=values, ibkr_values={})
            assert rows[1]['net_worth'] is None and rows[1]['unavailable_accounts'] == ['tonghuashun']
            scaled = nav.scale_series_to_terminal_anchor(values, terminal_date=day, target_value=Decimal(100))
            assert scaled.unknown_dates == set(days)
            publish({**fields,'symbol':'600002','net_cash':'-81'})
            known = nav._tonghuashun_series_from_trade_fact(cur, dates=days, account_owner_ratio=lambda _: Decimal(1))
            assert day in known.unknown_dates  # Cash is known; the held stock has no proven price.
            assert known[day] == Decimal(-81)


def test_tonghuashun_nav_never_treats_foreign_cash_as_cny_without_daily_fx(empty_source_db):
    from datetime import date, timedelta
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments import valuation_nav as nav
    from domain.imports import source_records_v2 as source
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sid, iid = uuid4(), uuid4()
            fields={'business_date':day.isoformat(),'currency':'USD','trade_type':'deposit','gross_amount':'100','net_cash':'100'}
            source.create_source(cur,source_id=sid,account_code='tonghuashun',source_name='synthetic',acquisition_method='manual',document_type='trade')
            source.add_item(cur,source_id=sid,item_id=iid,parse_method='manual',raw_fields=fields,parsed_fields=fields)
            source.review_item(cur,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=fields,review_status='confirmed')
            source.publish_trade_source(cur,source_id=sid,expected_revision=3)
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','USD',8,'synthetic')""", (day-timedelta(days=1),day+timedelta(days=1)))
            values=nav._tonghuashun_series_from_trade_fact(cur,dates=[day],account_owner_ratio=lambda _: Decimal(1))
            assert day in values.unknown_dates and values[day] == 0
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            values=nav._tonghuashun_series_from_trade_fact(cur,dates=[day],account_owner_ratio=lambda _: Decimal(1))
            assert day in values.unknown_dates and values[day]==Decimal(700)  # Scope remains unverified.


def test_same_day_buy_uses_only_account_matched_published_confirmed_sources(empty_source_db):
    from datetime import date, timedelta
    from psycopg2.extras import RealDictCursor
    from domain.investments.holdings_view import _adjust_a_stock_daily_pnl_for_same_day_buys
    from domain.investments.valuation_nav import _tonghuashun_series_from_trade_fact
    from endpoints.prices import _same_day_a_stock_trades, _a_stock_trade_net_quantities_before
    from domain.imports import source_records_v2 as source

    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.pams_tradingview_transactions') AS legacy")
            assert cur.fetchone()['legacy'] is None
            for account, quantity, trade_day in (('tonghuashun',2,day),('ibkr',100,day),
                                                  ('tonghuashun',100,day-timedelta(days=1))):
                sid, iid = uuid4(), uuid4()
                fields={'business_date':trade_day.isoformat(),'symbol':'600001','currency':'CNY','trade_type':'buy',
                        'market':'SSE','quantity':str(quantity),'price':'8','gross_amount':str(quantity*8),
                        'fee_amount':'0','tax_amount':'0','net_cash':str(-quantity*8)}
                source.create_source(cur,source_id=sid,account_code=account,source_name='synthetic',acquisition_method='manual',document_type='trade')
                source.add_item(cur,source_id=sid,item_id=iid,parse_method='manual',raw_fields=fields,parsed_fields=fields)
                source.review_item(cur,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                source.publish_trade_source(cur,source_id=sid,expected_revision=3)
            row = {'account_code': 'tonghuashun', 'symbol': '600001', 'major_category': 'A股',
                   'as_of': day, 'quantity': 10, 'current_price': 10, 'previous_price': 9,
                   'market_value': 100, 'market_value_cny': 100, 'daily_pnl': 10, 'daily_pnl_cny': 10}
            result = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, [row])[0]
            assert result['daily_pnl'] == 12 and result['daily_pnl_cny'] == 12
            key = ('tonghuashun', '600001')
            trades = _same_day_a_stock_trades(cur, {key}, day)
            assert len(trades[key]) == 1 and trades[key][0]['quantity'] == 2
            earlier = _a_stock_trade_net_quantities_before(cur, {key}, day)
            assert earlier[key] == 100 and key in earlier.unverified_keys
            values = _tonghuashun_series_from_trade_fact(cur, dates=[day], account_owner_ratio=lambda _: 1)
            assert values[day] == -816 and day in values.unknown_dates  # No proven market price.
            cur.execute("UPDATE pams_investment_trade_fact SET gross_amount=1 WHERE account_code='tonghuashun' AND trade_date=%s", (day,))
            assert key in _same_day_a_stock_trades(cur, {key}, day).unverified_keys


def test_tradingview_history_reads_only_published_facts_without_legacy_table(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from fastapi.testclient import TestClient
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read
    from domain.imports import source_records_v2 as source
    from psycopg2.extras import RealDictCursor

    today = date.today()
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.pams_tradingview_transactions') AS public_old, to_regclass('pams_legacy.pams_tradingview_transactions') AS legacy_old")
            assert list(cur.fetchone().values()) == [None, None]
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,symbol,account_code,asset_liability,currency,major_category,minor_category,
                 market_value,cost,quantity,current_price,as_of)
                VALUES ('synthetic','600001','tonghuashun','资产','CNY','A股','A股',100,80,10,10,%s)""", (today,))
            cur.execute("""INSERT INTO pams_ohlcv_daily(symbol,market,trade_date,close)
                VALUES ('sh600001','CN_A',%s,NULL),('sh600001','CN_A',%s,9)""",
                (today - timedelta(days=2), today - timedelta(days=1)))
            for status, quantity, suffix in (('published', 10, 'a'), ('draft', 100, 'b')):
                sid, iid = uuid4(), uuid4()
                trade_day = today - timedelta(days=2) if status == 'published' else today
                fields = {'business_date':trade_day.isoformat(),'symbol':'600001','market':'SSE','currency':'CNY',
                          'trade_type':'buy','quantity':str(quantity),'price':'8','gross_amount':str(quantity*8),
                          'net_cash':str(-quantity*8)}
                source.create_source(cur,source_id=sid,account_code='tonghuashun',source_name='tradingview',acquisition_method='manual',document_type='trade')
                source.add_item(cur,source_id=sid,item_id=iid,parse_method='manual',raw_fields=fields,parsed_fields=fields)
                source.review_item(cur,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                if status == 'published':
                    source.publish_trade_source(cur,source_id=sid,expected_revision=3)
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_view_as_of', lambda: today)
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: today)
    monkeypatch.setattr(main, '_daily_pnl_view_as_of', lambda: today)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratio', lambda code: 1)
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    from psycopg2.extras import RealDictCursor
    from domain.investments.holdings_view import _adjust_a_stock_daily_pnl_for_same_day_buys
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        row = {'account_code': 'tonghuashun', 'symbol': '600001', 'major_category': 'A股',
               'as_of': today, 'quantity': 10, 'current_price': 10, 'previous_price': 9,
               'market_value': 100, 'market_value_cny': 100, 'daily_pnl': 10, 'daily_pnl_cny': 10}
        adjusted = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, [row])[0]
        assert adjusted['daily_pnl'] == 10  # The draft buy must not change it.
    with TestClient(main.app) as web:
        response = web.get('/holdings/history/A股')
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['history_evidence']['status'] == 'estimated'
        rows = {item['date']: item['value'] for item in payload['rows']}
        assert rows[(today - timedelta(days=2)).isoformat()] is None
        assert rows[(today - timedelta(days=1)).isoformat()] == 90
        assert rows[today.isoformat()] == 100
        holdings = web.get('/holdings/by-category/A股?includeClosed=true')
        assert holdings.status_code == 200, holdings.text
        assert holdings.json()['closed_positions_evidence']['tradingview'] == 'unavailable_unverified_trade_coverage'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_investment_trade_fact SET gross_amount=1 WHERE account_code='tonghuashun'")
        response = web.get('/holdings/history/A股')
        assert response.status_code == 200
        assert response.json()['history_evidence']['reason'] == 'trade_source_evidence_incomplete'


def test_initialization_failure_rolls_back_every_table(empty_source_db, monkeypatch, tmp_path):
    bad = tmp_path / 'invalid.sql'
    bad.write_text('SELECT nonexistent_function_for_test();', encoding='utf-8')
    paths = bootstrap.isolated_source_paths()
    monkeypatch.setattr(bootstrap, 'isolated_source_paths', lambda: [*paths, bad])
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(psycopg2.Error):
            bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_accounts'),to_regclass('public.source_batches')")
            assert cur.fetchone() == (None, None)


def test_fund_refresh_checks_schema_without_runtime_ddl(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.market.fund_nav_estimates import ensure_fund_nav_estimate_table

    with psycopg2.connect(**empty_source_db) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(RuntimeError, match='schema unavailable'):
                ensure_fund_nav_estimate_table(cur)
            cur.execute("SELECT to_regclass('public.market_observations') AS existing")
            assert cur.fetchone()['existing'] is None
        conn.rollback()
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            ensure_fund_nav_estimate_table(cur)


def test_disabled_or_nonempty_target_is_refused(empty_source_db, monkeypatch):
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(BootstrapError, match='disabled'):
            bootstrap.initialize_isolated_sources(conn)
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE public.not_empty(id integer)')
        conn.commit()
        with pytest.raises(BootstrapError, match='not empty'):
            bootstrap.initialize_isolated_sources(conn)


def test_summary_and_account_preserve_holdings_with_missing_and_stale_fx(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments import portfolio_summary as summary

    today = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,account_code,asset_liability,currency,major_category,minor_category,market_value,cost,as_of)
                VALUES ('domestic','tonghuashun','资产','CNY','A股','A股',100,80,%s),
                       ('foreign','tonghuashun','资产','USD','A股','A股',100,80,%s)""", (today,today))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')", (today + timedelta(days=1),))
            # This test isolates FX behavior; account coverage has independent full-stack tests.
            monkeypatch.setattr(summary, '_incomplete_snapshot_accounts', lambda *_: set())
            def read():
                kw = dict(base_currency='CNY', view_as_of=today, owner_ratio_sql='1', owner_ratio_params=())
                return summary.build_holdings_summary_payload(cur, **kw), summary.build_holdings_by_account_payload(cur, **kw)
            totals, accounts = read()
            category = totals['by_category'][0]
            account = next(row for row in accounts['by_account'] if row['account_code']=='tonghuashun')
            assert category['holdings_count'] == account['holdings_count'] == 2
            assert totals['total']['value'] is None and Decimal(totals['known_value']) == 100
            assert account['total_value_base'] is None and Decimal(account['known_value_base']) == 100
            assert totals['fx_status'] == account['fx_status'] == 'incomplete'
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (today - timedelta(days=1),))
            totals, accounts = read()
            account = next(row for row in accounts['by_account'] if row['account_code']=='tonghuashun')
            assert Decimal(totals['total']['value']) == Decimal(account['total_value_base']) == 800
            assert totals['fx_precision'] == account['fx_precision'] == 'estimated'
            assert account['fx_rate_as_of'] == (today - timedelta(days=1)).isoformat()
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7.2,'synthetic')", (today,))
            totals, accounts = read()
            account = next(row for row in accounts['by_account'] if row['account_code']=='tonghuashun')
            assert Decimal(totals['total']['value']) == Decimal(account['total_value_base']) == 820
            assert account['fx_precision'] == 'daily_exact'


def test_summary_converts_non_cny_base_with_explicit_cny_cross_rates(empty_source_db, monkeypatch):
    from datetime import date, timedelta
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments import portfolio_summary as summary
    today = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (name,account_code,asset_liability,currency,major_category,minor_category,market_value,cost,as_of)
                VALUES ('eur','tonghuashun','资产','EUR','A股','A股',100,80,%s)""", (today,))
            monkeypatch.setattr(summary, '_incomplete_snapshot_accounts', lambda *_: set())
            kw = dict(base_currency='USD', view_as_of=today, owner_ratio_sql='1', owner_ratio_params=())
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',8,'synthetic'),(%s,'CNY','USD',7,'synthetic')", (today,today + timedelta(days=1)))
            payload = summary.build_holdings_summary_payload(cur, **kw)
            assert payload['total']['value'] is None and payload['fx_status'] == 'incomplete'
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (today - timedelta(days=1),))
            payload = summary.build_holdings_summary_payload(cur, **kw)
            account = summary.build_holdings_by_account_payload(cur, **kw)['by_account'][0]
            assert Decimal(payload['total']['value']) == Decimal(account['total_value_base']) == Decimal('114.29')
            assert payload['fx_precision'] == account['fx_precision'] == 'estimated'
            assert account['fx_rate_as_of'] == (today - timedelta(days=1)).isoformat()


def test_portfolio_transaction_gross_cny_uses_trade_day_fx(empty_source_db):
    from datetime import date, timedelta
    from decimal import Decimal
    from psycopg2.extras import RealDictCursor
    from domain.investments.portfolio_state import load_investment_transactions
    from domain.imports import source_records_v2 as source
    day = date(2026, 10, 30)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for currency in ('USD','CNY'):
                sid, iid = uuid4(), uuid4()
                fields={'business_date':day.isoformat(),'symbol':currency,'currency':currency,'trade_type':'buy',
                        'quantity':'1','gross_amount':'100','net_cash':'-100'}
                source.create_source(cur,source_id=sid,account_code='tonghuashun',source_name='synthetic',acquisition_method='manual',document_type='trade')
                source.add_item(cur,source_id=sid,item_id=iid,parse_method='manual',raw_fields=fields,parsed_fields=fields)
                source.review_item(cur,source_id=sid,item_id=iid,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                source.publish_trade_source(cur,source_id=sid,expected_revision=3)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic'),(%s,'CNY','USD',6,'synthetic')",
                        (day + timedelta(days=1), day - timedelta(days=1)))
            rows = {row['symbol']: row for row in load_investment_transactions(cur, 'USD')}
            assert rows['USD']['gross_amount_cny'] is None and rows['USD']['fx_status'] == 'unavailable'
            assert rows['CNY']['gross_amount_cny'] == Decimal(100)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
            rows = {row['symbol']: row for row in load_investment_transactions(cur, 'USD')}
            assert rows['USD']['gross_amount_cny'] == Decimal(700)
