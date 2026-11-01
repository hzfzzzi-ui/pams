# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_published_bank_source_proof_and_tamper_fail_closed(empty_source_db, monkeypatch):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from domain.investments.valuation_nav import _statement_rows_by_account, _bank_backcast_series
    from endpoints import account_import
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_a','A','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'a': {
        'account_code':'bank_a','statement_template':'manual_statement_csv','currency':'CNY'}}, 'rules':[], 'amount_rules':[]})
    app = FastAPI()
    app.include_router(account_import.router, prefix='/api')
    day = date.today() - timedelta(days=1)
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        response = web.post('/api/account-import/batches', json={'account_code':'bank_a','mode':'upload','kind':'cash'})
        assert response.status_code == 200, response.text
        path = '/api/account-import/batches/' + response.json()['batch_id']
        content = f'transaction_date,amount,currency,balance,summary\n{day},-10,CNY,90,expense\n{day},-20,CNY,70,expense\n'.encode()
        response = web.post(path+'/files?filename=bank.csv', content=content)
        assert response.status_code == 200, response.text
        batch = response.json()
        for row in batch['rows']:
            row['fields']['action'] = 'expense'
        response = web.put(path, json={'revision': batch['revision'], 'rows': batch['rows']})
        assert response.status_code == 200, response.text
        batch = response.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert checked['can_publish'], checked
        response = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert response.status_code == 200, response.text
    conn = psycopg2.connect(**empty_source_db)
    conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rows = _statement_rows_by_account(cur, ['bank_a'], day, day)
            assert len(rows['bank_a']) == 2 and not rows.evidence_problems
            assert all(row['statement_balance'] is None for row in rows['bank_a'])
            account = {'account_code':'bank_a','account_type':'funding','base_currency':'CNY','include_in_nav':True,
                       'primary_balance': {'amount':'70','currency':'CNY','as_of':day.isoformat()}}
            values = _bank_backcast_series(accounts=[account], rows_by_account=rows,
                dates=[day], terminal_date=day, fx_timelines={'CNY':[(day,Decimal(1))]}, base_currency='CNY')
            assert values[day]['net_worth'] == Decimal(70)
            with psycopg2.connect(**empty_source_db) as writer, writer.cursor() as update:
                update.execute("UPDATE pams_cashflow_fact SET amount=amount+1 WHERE cashflow_id=(SELECT min(cashflow_id) FROM pams_cashflow_fact)")
            # One repeatable-read transaction continues to see the prior consistent state.
            assert not _statement_rows_by_account(cur,['bank_a'],day,day).evidence_problems
    finally:
        conn.rollback()
        conn.close()
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rows = _statement_rows_by_account(cur,['bank_a'],day,day)
            assert rows.evidence_problems['bank_a']
            values = _bank_backcast_series(accounts=[account], rows_by_account=rows,
                dates=[day], terminal_date=day, fx_timelines={'CNY':[(day,Decimal(1))]}, base_currency='CNY')
            assert values[day]['source_evidence_unavailable_accounts'] == ['bank_a']
    import main
    from endpoints import accounts
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_investment_return_view_as_of', lambda cur: day)
    monkeypatch.setattr(main, '_load_portfolio_accounts_config', lambda: {'accounts': {'ibkr': {'include_in_nav': False}}})
    monkeypatch.setattr(main, '_load_account_balances', lambda *args, **kwargs: [])
    monkeypatch.setattr(main, '_load_investment_positions', lambda *args, **kwargs: [])
    monkeypatch.setattr(main, '_latest_published_portfolio_valuation_state', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, '_current_nav_bucket_from_parts', lambda **kwargs: {'assets':Decimal(70), 'liabilities':Decimal(0), 'net_worth':Decimal(70)})
    monkeypatch.setattr(main, '_current_nav_account_values_from_parts', lambda **kwargs: {'bank_a':Decimal(70)})
    monkeypatch.setattr(accounts, '_overview_payload', lambda cur=None: {'accounts': [account]})
    with TestClient(main.app) as web:
        response = web.get('/nav/weekly', params={'start':day.isoformat(), 'latest':'true', 'interval':'daily'})
        assert response.status_code == 200, response.text
        row = response.json()['rows'][-1]
        assert row['netWorth'] is None
        assert row['source_evidence_unavailable_accounts'] == ['bank_a']
