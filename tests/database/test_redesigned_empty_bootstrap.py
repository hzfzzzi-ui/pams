"""Initialize a genuinely empty isolated database and use the new HTTP path."""
import psycopg2
from decimal import Decimal
from datetime import date
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.imports.test_account_import_ths import event, export
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_empty_database_initializes_new_sources_and_accepts_reviewed_cash(empty_source_db, monkeypatch):
    from endpoints import account_import, ledger, accounts, sync_center
    from domain.imports import account_import_statement as statement, account_import_store
    from domain.investments import portfolio_state
    from domain.brokers.ibkr import ibkr_daily_close_read, ibkr_performance
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for table in ('source_records', 'source_items', 'pams_cashflow_fact', 'pams_investment_trade_fact',
                          'cashflow_classification', 'investment_trade_review'):
                cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
                assert cur.fetchone()[0] is not None
            for table in ('source_batches', 'source_files'):
                cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
                assert cur.fetchone()[0] is None
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('ccb_test_1234','Synthetic','CNY'),('tonghuashun','Synthetic broker','CNY') ON CONFLICT DO NOTHING")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(account_import_store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ledger, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(accounts, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(sync_center, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'profiles', lambda *args: {'ccb_test_1234':[{'statement_template':'ccb_credit_pdf'}]})
    monkeypatch.setattr(accounts, '_load_accounts_config', lambda: {'accounts': {'ccb_test_1234': {'account_type': 'funding', 'truth_source': 'bank_statement'}}})
    monkeypatch.setattr(accounts, '_load_bank_config', lambda: {})
    monkeypatch.setattr(portfolio_state, '_load_accounts_config', lambda: {})
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    monkeypatch.setattr(ibkr_performance, 'published_report_status', lambda conn: {'status':'missing', 'label':'缺数据',
        'statement_as_of':None, 'active_row_count':0, 'expected_statement_as_of':None, 'last_attempt_status':None})
    monkeypatch.setattr(sync_center, 'read_ibkr_reconciliation', lambda: {'status':'missing','label':'缺数据','details':''})
    monkeypatch.setattr(sync_center, 'scheduler_health', lambda path: {'status':'missing','label':'缺数据','jobs':{}})
    app = FastAPI()
    app.include_router(account_import.router, prefix='/api')
    app.include_router(ledger.router, prefix='/api')
    app.include_router(accounts.router, prefix='/api')
    app.include_router(sync_center.router, prefix='/api')
    data = ('中国建设银行信用卡交易明细\n交易日,入账日,信用卡卡号,类型,入账币种,入账金额,交易描述\n'
            '20260101,20260102,****1234,消费,人民币,20.00,示例商户\n').encode('gb18030')
    with TestClient(app) as client:
        client.headers['Origin'] = 'http://testserver'
        capabilities = client.get('/api/account-import/capabilities').json()
        assert capabilities['source_record_schema_ready'] and capabilities['source_record_publish_enabled']
        assert not capabilities['schema_ready']
        response = client.post('/api/account-import/source-records?account_code=ccb_test_1234&source_name=synthetic&filename=test.csv', content=data)
        assert response.status_code == 200, response.text
        source_id = response.json()['source_id']
        response = client.get('/api/account-import/source-records/' + source_id)
        assert response.status_code == 200, response.text
        source = response.json(); item = source['items'][0]
        response = client.put('/api/account-import/source-records/' + source_id + '/items/' + item['source_item_id'],
            json={'revision': source['revision'], 'parsed_fields': item['parsed_fields'], 'review_status': 'confirmed'})
        assert response.status_code == 200, response.text
        revision = response.json()['revision']
        response = client.post('/api/account-import/source-records/' + source_id + '/cash-preview', json={'revision': revision, 'mode': 'all'})
        assert response.status_code == 200, response.text
        response = client.post('/api/account-import/source-records/' + source_id + '/cash-publish',
            json={'revision': revision, 'mode': 'all', 'preview_token': response.json()['preview_token']})
        assert response.status_code == 200 and response.json()['published'] == 1
        broker_data = export(event(**{'成交日期':'20260103', '本次金额':'-503.10'}), event('银行转证券', **{
            '成交日期':'20260102', '发生金额':'500.00', '本次金额':'500.00', '定位串':'20260102!cash'}))
        response = client.post('/api/account-import/source-records?account_code=tonghuashun&source_name=synthetic&filename=table.xls', content=broker_data)
        assert response.status_code == 200, response.text
        source_id = response.json()['source_id']
        source = client.get('/api/account-import/source-records/' + source_id).json()
        revision = source['revision']
        for item in source['items']:
            response = client.put('/api/account-import/source-records/' + source_id + '/items/' + item['source_item_id'],
                json={'revision': revision, 'parsed_fields': item['parsed_fields'], 'review_status': 'confirmed'})
            assert response.status_code == 200, response.text
            revision = response.json()['revision']
        response = client.post('/api/account-import/source-records/' + source_id + '/trade-preview', json={'revision': revision})
        assert response.status_code == 200, response.text
        response = client.post('/api/account-import/source-records/' + source_id + '/trade-publish',
            json={'revision': revision, 'preview_token': response.json()['preview_token']})
        assert response.status_code == 200 and response.json()['published'] == 2
        response = client.get('/api/ledger/workbench?month=2026-01')
        assert response.status_code == 200, response.text
        ledger_data = response.json()
        assert ledger_data['periods']['month']['expense'] is None
        assert ledger_data['review_rows'][0]['flow_type'] == 'unclassified'
        funding = next(row for row in ledger_data['recent_rows_month'] if row['flow_type'] == 'investment_inflow')
        assert funding['amount'] == 500 and funding['needs_review'] is True
        response = client.get('/api/accounts/overview')
        assert response.status_code == 200, response.text
        account = next(item for item in response.json()['accounts'] if item['account_code'] == 'ccb_test_1234')
        assert sum(item['row_count'] for item in account['statement_profiles']) == 1
        assert account['statement_balance'] is None
        response = client.get('/api/sync/center')
        assert response.status_code == 200, response.text
        assert next(row for row in response.json()['rows'] if row['id'] == 'account-reconcile')['rows'] == 1
        cash_id = ledger_data['review_rows'][0]['id']
        response = client.post(f'/api/ledger/rows/{cash_id}/update', json={
            'cashflow_type': 'operating_expense', 'category': '购物消费', 'subcategory': '测试'})
        assert response.status_code == 200, response.text
        updated = client.get('/api/ledger/workbench?month=2026-01').json()
        assert updated['periods']['month']['expense'] == 20
        response = client.post(f'/api/ledger/rows/{cash_id}/update', json={
            'cashflow_type': 'internal_transfer', 'counter_account_code': 'tonghuashun'})
        assert response.status_code == 409
        published_id = source_id
        response = client.post('/api/account-import/source-records?account_code=ccb_test_1234&source_name=synthetic&filename=other.csv',
                               content=data.replace('示例商户'.encode('gb18030'), '测试商户'.encode('gb18030')))
        assert response.status_code == 200, response.text
        draft = response.json()
        listing = client.get('/api/account-import/source-records').json()['sources']
        assert [row['status'] for row in listing].count('draft') == 1
        assert [row['status'] for row in listing].count('published') == 2
        assert client.post('/api/account-import/source-records/' + published_id + '/delete-draft', json={'revision': revision + 1}).status_code == 400
        assert client.post('/api/account-import/source-records/' + draft['source_id'] + '/delete-draft', json={'revision': draft['revision'] + 1}).status_code == 400
        response = client.post('/api/account-import/source-records/' + draft['source_id'] + '/delete-draft', json={'revision': draft['revision']})
        assert response.status_code == 200 and response.json()['deleted']
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=-21 WHERE cashflow_id=%s', (cash_id,))
        response = client.get('/api/accounts/overview')
        assert response.status_code == 200, response.text
        account = next(item for item in response.json()['accounts'] if item['account_code'] == 'ccb_test_1234')
        assert account['statement_profiles'][0]['source_evidence_status'] == 'unknown'
        assert account['statement_profiles'][0]['row_count'] is None
        response = client.get('/api/sync/center')
        assert next(row for row in response.json()['rows'] if row['id'] == 'account-reconcile')['rows'] is None
        response = client.get('/api/ledger/workbench?month=2026-01')
        row = next(item for item in response.json()['recent_rows_month'] if item['account_code'] == 'ccb_test_1234')
        assert row['source_evidence_status'] == 'unknown' and row['amount_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=-20 WHERE cashflow_id=%s', (cash_id,))
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT f.amount,c.status FROM pams_cashflow_fact f JOIN cashflow_classification c USING(cashflow_id)')
        assert cur.fetchone() == (-20, 'confirmed')
        cur.execute("SELECT symbol,gross_amount,net_cash FROM pams_investment_trade_fact WHERE trade_type='buy'")
        assert cur.fetchone() == ('000001', Decimal('1000'), Decimal('-1003.1'))
    from domain.ledger.ledger_view import _load_cash_rows, classify_ledger_row
    from domain.investments.trade_read_v2 import load_trade_rows
    from domain.investments.holdings_view import _adjust_a_stock_daily_pnl_for_same_day_buys
    from domain.investments.valuation_nav import _tonghuashun_series_from_trade_fact
    from endpoints.prices import _same_day_a_stock_trades, _a_stock_trade_net_quantities_before
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        rows = _load_cash_rows(cur, start=date(2026, 1, 1), end=date(2026, 1, 31), base_currency='CNY')
        assert len(rows) == 1 and rows[0]['source_evidence_status'] == 'verified'
        assert classify_ledger_row(rows[0], set(), set())['flow_type'] == 'expense'
        rows = load_trade_rows(cur, account_code='tonghuashun', start=date(2026, 1, 1), end=date(2026, 12, 31))
        assert len(rows) == 2 and all(row['source_evidence_status'] == 'verified' for row in rows)
        cur.execute("""INSERT INTO market_observations(observation_type,as_of,symbol,market,source,source_key,close_price)
            VALUES ('daily','2026-01-03','sz000001','CN_A','tencent_cn_unadjusted_day_v1','synthetic',11)""")
        day = date(2026, 1, 3)
        projected = _tonghuashun_series_from_trade_fact(cur, dates=[day], account_owner_ratio=lambda _: Decimal('1'))
        assert projected.preview_by_date[day] == Decimal('596.9')
        assert day in projected.unknown_dates and projected.cash_scope_unverified
        key = ('tonghuashun', '000001')
        assert len(_same_day_a_stock_trades(cur, {key}, day)[key]) == 1
        assert not _a_stock_trade_net_quantities_before(cur, {key}, day)
        holding = {'account_code': 'tonghuashun', 'symbol': '000001', 'major_category': 'A股',
                   'as_of': date(2026, 1, 3), 'quantity': 100, 'current_price': 11, 'previous_price': 9,
                   'market_value': 1100, 'market_value_cny': 1100, 'currency': 'CNY', 'daily_pnl': 200}
        adjusted = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, [dict(holding)])[0]
        assert adjusted['daily_pnl'] == 100
        cur.execute("UPDATE pams_investment_trade_fact SET gross_amount=1 WHERE trade_type='buy'")
        assert key in _same_day_a_stock_trades(cur, {key}, day).unverified_keys
        adjusted = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, [dict(holding)])[0]
        assert adjusted['_daily_basis_unavailable'] is True


def test_mapping_is_saved_and_reused_without_changing_original_evidence(empty_source_db, monkeypatch):
    from endpoints import account_import
    from domain.imports import account_import_store as store, document_stage_v2
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(document_stage_v2.document_kind, 'detect', lambda *args: {'kind':'trade','status':'detected'})
    monkeypatch.setattr(document_stage_v2, 'assess_template', lambda parsed, kind: 'matched')
    monkeypatch.setattr(store, 'parse', lambda content, filename, account, kind, mapping=None: {
        'rows': [{'fields': {'note': 'mapped' if mapping else 'initial'}, 'source': {'row': 1}, 'issues': []}]})
    app = FastAPI(); app.include_router(account_import.router, prefix='/api')
    with TestClient(app) as client:
        client.headers['Origin'] = 'http://testserver'
        url = '/api/account-import/source-records?account_code=tonghuashun&source_name=synthetic&filename=one.csv'
        first = client.post(url, content=b'synthetic one').json()
        source_id = first['source_id']
        response = client.put('/api/account-import/source-records/' + source_id + '/mapping',
                              json={'revision': first['revision'], 'mapping': {'symbol': 'ticker'}})
        assert response.status_code == 200, response.text
        mapped = client.get('/api/account-import/source-records/' + source_id).json()
        assert mapped['items'][0]['parsed_fields']['note'] == 'mapped'
        second = client.post(url.replace('one.csv','two.csv'), content=b'synthetic two').json()
        source = client.get('/api/account-import/source-records/' + second['source_id']).json()
        assert source['items'][0]['parsed_fields']['note'] == 'mapped'
        item = mapped['items'][0]
        reviewed = client.put('/api/account-import/source-records/' + source_id + '/items/' + item['source_item_id'],
            json={'revision':mapped['revision'], 'parsed_fields':item['parsed_fields'], 'review_status':'confirmed'})
        assert reviewed.status_code == 200
        rejected = client.put('/api/account-import/source-records/' + source_id + '/mapping',
                              json={'revision':reviewed.json()['revision'], 'mapping': {'symbol':'another'}})
        assert rejected.status_code == 400
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT public.source_original_content(account_code,sha256,content) FROM source_records WHERE source_id=%s', (source_id,))
        assert bytes(cur.fetchone()[0]) == b'synthetic one'
        cur.execute("SELECT mapping FROM source_templates WHERE account_code='tonghuashun' AND document_type='trade'")
        assert cur.fetchone()[0] == {'symbol':'ticker'}
