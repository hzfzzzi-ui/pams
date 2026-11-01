"""Full new-schema bank import and confirmed transfer evidence."""
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap


def test_partial_broker_import_waits_for_independent_bank_source(empty_source_db, monkeypatch):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from endpoints import account_import
    from tests.imports.test_account_import_ths import event, export

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_a','Synthetic bank','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'a': {'account_code':'bank_a',
        'statement_template':'manual_statement_csv', 'currency':'CNY'}}, 'rules':[], 'amount_rules':[]})
    app = FastAPI()
    app.include_router(account_import.router, prefix='/api')
    day = (date.today() - timedelta(days=2)).isoformat()
    native_day = day.replace('-', '')
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        parent = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'upload','kind':'trade'}).json()
        path = '/api/account-import/batches/' + parent['batch_id']
        data = export(event('利息归本', **{'成交日期':native_day,'发生金额':'10.00','定位串':'synthetic-interest'}),
                      event('银行转证券', **{'成交日期':native_day,'发生金额':'500.00','定位串':'synthetic-deposit'}))
        response = web.post(path + '/files?filename=synthetic.xls', content=data)
        assert response.status_code == 200, response.text
        parent = response.json()
        parent = web.put(path, json={'revision':parent['revision'], 'rows':parent['rows']}).json()
        check = web.post(path + '/preview', json={'revision':parent['revision']}).json()
        assert check['can_publish_ready'] and check['counts']['pending'] == 1
        result = web.post(path + '/publish-ready', json={'revision':parent['revision'],'preview_token':check['preview_token']})
        assert result.status_code == 200, result.text
        child_id = result.json()['deferred_batch_id']
        child_path = '/api/account-import/batches/' + child_id
        child = web.get(child_path).json()
        child = web.put(child_path, json={'revision':child['revision'], 'rows':child['rows']}).json()
        pending = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
        assert not pending['can_publish'] and pending['rows'][0]['bank_candidates'] == []
        from domain.imports import source_fact_evidence
        with monkeypatch.context() as patch:
            patch.setattr(source_fact_evidence, '_reviewed_snapshot', lambda *args, **kwargs: False)
            invalid = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
            assert invalid['rows'][0]['status'] == 'blocked'
            assert any(i['code'] == 'deferred_source_unverified' for i in invalid['rows'][0]['issues'])
        child['rows'][0]['fields']['counter_account_code'] = 'bank_a'
        child['rows'][0]['acknowledged'] = True
        child = web.put(child_path, json={'revision':child['revision'], 'rows':child['rows']}).json()
        pending = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
        assert not pending['can_publish']
        assert any(i['code'] == 'bank_confirmation_required' for i in pending['rows'][0]['issues'])
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_cashflow_fact')
            assert cur.fetchone()[0] == 0

        bank = web.post('/api/account-import/batches', json={'account_code':'bank_a','mode':'upload','kind':'cash'}).json()
        bank_path = '/api/account-import/batches/' + bank['batch_id']
        content = f'transaction_date,amount,currency,balance,summary\n{day},-500,CNY,200,investment\n'.encode()
        bank = web.post(bank_path + '/files?filename=synthetic.csv', content=content).json()
        bank['rows'][0]['fields'].update(action='transfer', counter_account_code='tonghuashun')
        bank = web.put(bank_path, json={'revision':bank['revision'], 'rows':bank['rows']}).json()
        bank_check = web.post(bank_path + '/preview', json={'revision':bank['revision']}).json()
        assert bank_check['can_publish'], bank_check
        assert web.post(bank_path + '/publish', json={'revision':bank['revision'],'preview_token':bank_check['preview_token']}).status_code == 200

        pending = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
        candidates = pending['rows'][0]['bank_candidates']
        assert len(candidates) == 1 and candidates[0]['account_code'] == 'bank_a'
        child['rows'][0]['fields']['bank_cashflow_id'] = '999999'
        child = web.put(child_path, json={'revision':child['revision'], 'rows':child['rows']}).json()
        invalid = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
        assert not invalid['can_publish'] and any(i['code'] == 'bank_confirmation_required' for i in invalid['rows'][0]['issues'])
        child['rows'][0]['fields']['bank_cashflow_id'] = candidates[0]['cashflow_id']
        child = web.put(child_path, json={'revision':child['revision'], 'rows':child['rows']}).json()
        checked = web.post(child_path + '/preview', json={'revision':child['revision']}).json()
        assert checked['can_publish'], checked
        response = web.post(child_path + '/publish', json={'revision':child['revision'],'preview_token':checked['preview_token']})
        assert response.status_code == 200, response.text
        replay = web.post(child_path + '/publish', json={'revision':child['revision'],'preview_token':checked['preview_token']})
        assert replay.status_code == 200 and replay.json()['replayed']
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cashflow_id,net_cash FROM pams_investment_trade_fact WHERE side='deposit'")
            deposit = cur.fetchone()
            assert deposit['net_cash'] == 500 and str(deposit['cashflow_id']) == candidates[0]['cashflow_id']
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 1
            cur.execute("SELECT count(*) AS n FROM evidence_confirmation WHERE confirmation_type='transfer' AND revoked_at IS NULL")
            assert cur.fetchone()['n'] == 1


def test_new_bank_sources_and_two_sided_confirmation(empty_source_db, monkeypatch):
    from domain.imports import account_import_statement as statement, account_import_store as store
    from domain.investments.portfolio_state import load_cash_transactions, load_transfer_matches
    from endpoints import account_import, bank_reconcile, ledger
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            for table in ('pams_bank_statement_imports','pams_bank_statement_rows','pams_bank_transfer_matches'):
                cur.execute('SELECT to_regclass(%s)', (table,))
                assert cur.fetchone()[0] is None
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_a','A','CNY'),('bank_b','B','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    for module in (store, bank_reconcile, ledger):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {
        'a': {'account_code':'bank_a', 'statement_template':'manual_statement_csv', 'currency':'CNY'},
        'b': {'account_code':'bank_b', 'statement_template':'manual_statement_csv', 'currency':'CNY'}},
        'rules':[], 'amount_rules':[]})
    app = FastAPI()
    for router in (account_import.router, bank_reconcile.router, ledger.router):
        app.include_router(router, prefix='/api')
    day = (date.today() - timedelta(days=2)).isoformat()
    with TestClient(app) as web:
        web.headers['Origin'] = 'http://testserver'
        for account, counter, amount in [('bank_a','bank_b','-100.05'),('bank_b','bank_a','100.05')]:
            response = web.post('/api/account-import/batches', json={'account_code':account,'mode':'upload','kind':'cash'})
            assert response.status_code == 200, response.text
            path = '/api/account-import/batches/' + response.json()['batch_id']
            content = f'transaction_date,amount,currency,balance,summary\n{day},{amount},CNY,200.05,transfer\n'.encode()
            response = web.post(path + '/files?filename=bank.csv', content=content)
            assert response.status_code == 200, response.text
            batch = response.json()
            batch['rows'][0]['fields']['action'] = 'transfer'
            batch['rows'][0]['fields']['counter_account_code'] = counter
            response = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
            assert response.status_code == 200, response.text
            batch = response.json()
            response = web.post(path + '/preview', json={'revision':batch['revision']})
            assert response.status_code == 200, response.text
            check = response.json()
            assert check['can_publish'], check
            response = web.post(path + '/publish', json={'revision':batch['revision'], 'preview_token':check['preview_token']})
            assert response.status_code == 200, response.text
            from domain.ledger.ledger_view import _load_pending_transfer_cases
            with psycopg2.connect(**empty_source_db) as check_conn:
                check_conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
                with check_conn.cursor(cursor_factory=RealDictCursor) as check_cur:
                    pending = _load_pending_transfer_cases(check_cur, start=date.fromisoformat(day), end=date.fromisoformat(day))
                    if account == 'bank_a':
                        assert len(pending) == 1 and pending[0]['status'] == 'unmatched'
                    else:
                        assert len(pending) == 2 and all(item['status'] == 'unique_candidate' for item in pending)
        workbench = web.get('/api/ledger/workbench', params={'month':day[:7]})
        assert workbench.status_code == 200, workbench.text
        assert workbench.json()['pending_transfer_count'] == 2
        assert workbench.json()['pending_transfer_count_status'] == 'known_subset'
        assert {item['status'] for item in workbench.json()['pending_transfer_cases']} == {'unique_candidate'}
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cashflow_id,account_code,amount,is_internal_transfer,affects_spending FROM pams_cashflow_fact ORDER BY account_code")
            facts = cur.fetchall()
            assert len(facts) == 2 and all(row['is_internal_transfer'] and not row['affects_spending'] for row in facts)
            first, second = (facts[0]['cashflow_id'], facts[1]['cashflow_id'])
        # A conflicting edit while confirmation waits on its first account
        # lock must not expand the lock set or begin source verification.
        import queue
        import time
        from domain.ledger import confirmed_transfers as transfer_service
        worker_pid = queue.Queue()
        def waiting_confirmation():
            with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT pg_backend_pid() AS pid')
                worker_pid.put(cur.fetchone()['pid'])
                try:
                    transfer_service.confirm(cur,from_kind='cashflow',from_id=first,to_kind='cashflow',to_id=second,
                                             note='并发范围检查',confirmed=True)
                except ValueError as exc:
                    return str(exc)
                return 'unexpected-success'
        with psycopg2.connect(**empty_source_db) as locker, locker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:bank_a',0))")
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(waiting_confirmation)
                pid = worker_pid.get(timeout=5)
                blocked = False
                with psycopg2.connect(**empty_source_db) as observer, observer.cursor() as check:
                    for _ in range(100):
                        check.execute('SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)', (pid,))
                        if check.fetchone()[0]:
                            blocked = True
                            break
                        time.sleep(0.02)
                assert blocked
                cur.execute("UPDATE pams_cashflow_fact SET counter_account_code='tonghuashun' WHERE cashflow_id=%s", (first,))
                locker.commit()
                assert task.result(timeout=10) == 'transfer account set changed while acquiring locks'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE pams_cashflow_fact SET counter_account_code='bank_b' WHERE cashflow_id=%s", (first,))
            cur.execute("SELECT raw_fields->'initial_fields'->>'action' AS original, parsed_fields->'fields'->>'action' AS reviewed FROM source_items WHERE review_status='confirmed' ORDER BY original")
            evidence = cur.fetchall()
            assert {row['original'] for row in evidence} == {'income','expense'}
            assert all(row['reviewed'] == 'transfer' for row in evidence)
        # The shared physical table still enforces each event type's shape.
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO evidence_confirmation
                    (confirmation_id,confirmation_type,from_cashflow_id,to_cashflow_id,from_account_code,
                     to_account_code,amount,currency,from_evidence,to_evidence,note)
                    VALUES ('00000000-0000-0000-0000-000000000001','transfer',%s,%s,
                            'bank_a','bank_b',NULL,'CNY','{}','{}','invalid')""", (first,second))
            conn.rollback()
        imports = web.get('/api/bank-reconcile/imports')
        assert imports.status_code == 200, imports.text
        assert len(imports.json()['imports']) == 2
        assert all(row['coverage_status'] == 'unknown' and Decimal(row['latest_balance']) == Decimal('200.05') and row['balance_source'] == 'statement_row' for row in imports.json()['summary'])
        pending = web.get('/api/bank-reconcile/unmatched-rows?since=2000-01-01')
        assert pending.status_code == 200, pending.text
        assert {r['row_id'] for r in pending.json()['rows']} == {first,second}
        run = web.post('/api/bank-reconcile/transfer-matches/run')
        assert run.status_code == 200 and run.json()['status'] == 'review_required'
        payload = {'from_kind':'cashflow','from_id':first,'to_kind':'cashflow','to_id':second,'note':'两侧原始流水已核对','confirmed':True}
        assert web.post('/api/bank-reconcile/transfers/confirm', json={**payload,'confirmed':False}).status_code == 409
        assert web.post('/api/bank-reconcile/transfers/confirm', json={**payload,'to_id':first}).status_code == 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: web.post('/api/bank-reconcile/transfers/confirm', json=payload), range(2)))
        assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
        assert sorted(response.json()['replayed'] for response in responses) == [False, True]
        transfer_id = responses[0].json()['transfer_id']
        assert responses[1].json()['transfer_id'] == transfer_id
        replay = web.post('/api/bank-reconcile/transfers/confirm', json=payload)
        assert replay.status_code == 200 and replay.json()['replayed']
        matches = web.get('/api/bank-reconcile/transfer-matches')
        assert matches.status_code == 200 and matches.json()['matches'][0]['evidence_status'] == 'confirmed'
        workbench = web.get('/api/ledger/workbench', params={'month':day[:7]})
        assert workbench.status_code == 200 and workbench.json()['pending_transfer_count'] == 0
        from domain.ledger.ledger_view import _load_transfer_matches, _load_pending_transfer_cases
        from endpoints.accounts import _fetch_transfer_rollups
        day_value = date.fromisoformat(day)
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                confirmed = _load_transfer_matches(cur, start=day_value, end=day_value, base_currency='CNY')
                assert len(confirmed) == 1 and confirmed[0]['amount_cny'] == Decimal('100.05')
                assert confirmed[0]['source_row_id'] == first and confirmed[0]['target_row_id'] == second
                assert _load_pending_transfer_cases(cur, start=day_value, end=day_value) == []
                rollups = _fetch_transfer_rollups(cur)
                assert rollups['bank_a']['out'][0]['total_amount'] == Decimal('100.05')
        # A published flag alone is not proof: retained confirmation becomes
        # invalid when either linked original file no longer matches its hash.
        from domain.ledger import confirmed_transfers
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT i.file_id FROM source_items i JOIN pams_cashflow_fact f ON f.source_item_id=i.source_item_id WHERE f.cashflow_id=%s", (first,))
            file_id = cur.fetchone()[0]
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE file_id=%s", (file_id,))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert confirmed_transfers.read_one(cur,transfer_id)['evidence_status'] == 'invalidated'
        assert web.post('/api/bank-reconcile/transfers/confirm', json=payload).status_code == 409
        broken = web.get('/api/bank-reconcile/imports')
        assert broken.status_code == 200, broken.text
        bank_summary = next(r for r in broken.json()['summary'] if r['account_code']=='bank_a')
        assert bank_summary['source_evidence_status']=='unknown' and bank_summary['net_flow'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute('UPDATE source_files SET content=substring(content FROM 1 FOR octet_length(content)-1) WHERE file_id=%s', (file_id,))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        assert web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]['evidence_status'] == 'confirmed'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            ledger_rows = load_cash_transactions(cur)
            assert {row['id'] for row in ledger_rows} == {first, second}
            assert all(row['coverage_status'] == 'unknown' for row in ledger_rows)
            portfolio_matches = load_transfer_matches(cur)
            assert len(portfolio_matches) == 1 and portfolio_matches[0]['status'] == 'confirmed'
        assert web.get('/api/bank-reconcile/unmatched-rows?since=2000-01-01').json()['rows'] == []
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=amount-1 WHERE cashflow_id=%s', (first,))
        changed = web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]
        assert changed['evidence_status'] == 'invalidated'
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert _load_transfer_matches(cur, start=day_value, end=day_value, base_currency='CNY') == []
                pending_cases = _load_pending_transfer_cases(cur, start=day_value, end=day_value)
                assert any(item['status'] == 'invalidated' and item['amount'] is None for item in pending_cases)
                assert not _fetch_transfer_rollups(cur)
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_transfer_matches(cur)[0]['status'] == 'invalidated'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('UPDATE pams_cashflow_fact SET amount=amount+1 WHERE cashflow_id=%s', (first,))
        revoke = web.post(f'/api/bank-reconcile/transfers/{transfer_id}/revoke', json={'note':'synthetic correction','confirmed':True})
        assert revoke.status_code == 200, revoke.text
        workbench = web.get('/api/ledger/workbench', params={'month':day[:7]})
        assert workbench.status_code == 200 and workbench.json()['pending_transfer_count'] == 2
        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert _load_transfer_matches(cur, start=day_value, end=day_value, base_currency='CNY') == []
                assert not _fetch_transfer_rollups(cur)
                pending = _load_pending_transfer_cases(cur, start=day_value, end=day_value)
                assert len(pending) == 2 and all(item['status'] == 'unique_candidate' for item in pending)
        assert web.get('/api/bank-reconcile/transfer-matches').json()['matches'][0]['evidence_status'] == 'revoked'
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_transfer_matches(cur)[0]['status'] == 'revoked'
        replacement = web.post('/api/bank-reconcile/transfers/confirm', json=payload)
        assert replacement.status_code == 200 and replacement.json()['transfer_id'] != transfer_id

        # A broker report may create a derived bank leg from the same source.
        # It cannot serve as independent evidence of the real bank statement.
        response = web.post('/api/account-import/batches', json={'account_code':'bank_a','mode':'upload','kind':'cash'})
        path = '/api/account-import/batches/' + response.json()['batch_id']
        content = f'transaction_date,amount,currency,balance,summary\n{day},-50,CNY,200,investment\n'.encode()
        batch = web.post(path + '/files?filename=second.csv', content=content).json()
        batch['rows'][0]['fields'].update(action='transfer', counter_account_code='tonghuashun')
        changed = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']})
        assert changed.status_code == 200, changed.text
        batch = changed.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert checked['can_publish'], checked
        assert web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']}).status_code == 200
        batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'manual','kind':'cash'}).json()
        path = '/api/account-import/batches/' + batch['batch_id']
        fields = {'kind':'cash','business_date':day,'currency':'CNY','action':'deposit','net_cash':'50','counter_account_code':'bank_a'}
        response = web.put(path, json={'revision':batch['revision'],'rows':[{'fields':fields}]})
        assert response.status_code == 200, response.text
        batch = response.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert not checked['can_publish']
        assert any(issue['code']=='bank_confirmation_required' for row in checked['rows'] for issue in row['issues'])
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-50")
            bank_id = cur.fetchone()['cashflow_id']
        batch['rows'][0]['fields']['bank_cashflow_id'] = str(bank_id)
        changed = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']})
        assert changed.status_code == 200, changed.text
        batch = changed.json()
        not_confirmed = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert not not_confirmed['can_publish']
        batch['rows'][0]['acknowledged'] = True
        changed = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']})
        assert changed.status_code == 200, changed.text
        batch = changed.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert checked['can_publish'], checked
        from domain.ledger import confirmed_transfers
        with monkeypatch.context() as patch:
            def fail_match(*args, **kwargs):
                raise ValueError('synthetic match failure')
            patch.setattr(confirmed_transfers, 'confirm', fail_match)
            failed = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
            assert failed.status_code >= 400
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE account_code='tonghuashun' AND net_cash=50")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-50")
            assert cur.fetchone()[0] == 1
        response = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert response.status_code == 200, response.text
        replay = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert replay.status_code == 200 and replay.json()['replayed']
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT trade_id,cashflow_id FROM pams_investment_trade_fact WHERE account_code='tonghuashun' AND net_cash=50")
            investment = cur.fetchone()
            assert investment['cashflow_id'] == bank_id
            cur.execute("SELECT count(*) AS n FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-50")
            assert cur.fetchone()['n'] == 1
        matches = web.get('/api/bank-reconcile/transfer-matches').json()['matches']
        assert any(m['from_evidence']['id']==bank_id and m['to_evidence']['id']==investment['trade_id']
                   and m['evidence_status']=='confirmed' for m in matches)

        # With no real bank fact, retain an expressly labelled derived leg.
        batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'manual','kind':'cash'}).json()
        path = '/api/account-import/batches/' + batch['batch_id']
        fields = {'kind':'cash','business_date':day,'currency':'CNY','action':'deposit','net_cash':'70','counter_account_code':'bank_a'}
        response = web.put(path, json={'revision':batch['revision'],'rows':[{'fields':fields}]})
        batch = response.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert checked['can_publish'], checked
        derived_plan = next(entry for entry in checked['write_plan'] if entry.get('role') == 'bank_leg')
        assert derived_plan['after']['raw_json']['evidence_role'] == 'derived_bank_leg'
        assert derived_plan['after']['raw_json']['independent_bank_evidence'] is False
        response = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert response.status_code == 200, response.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT trade_id,cashflow_id,source_item_id FROM pams_investment_trade_fact WHERE net_cash=70 AND account_code='tonghuashun'")
            derived_trade = cur.fetchone()
            cur.execute("SELECT raw_json,source_item_id FROM pams_cashflow_fact WHERE cashflow_id=%s", (derived_trade['cashflow_id'],))
            derived = cur.fetchone()
            assert derived['source_item_id'] == derived_trade['source_item_id']
            assert derived['raw_json']['evidence_role'] == 'derived_bank_leg'
            assert derived['raw_json']['independent_bank_evidence'] is False
        fake = web.post('/api/bank-reconcile/transfers/confirm', json={
            'from_kind':'cashflow','from_id':derived_trade['cashflow_id'], 'to_kind':'trade',
            'to_id':derived_trade['trade_id'], 'note':'不允许同一来源冒充两侧', 'confirmed':True})
        assert fake.status_code == 409
        # A later bank statement cannot be made a second expense/transfer by
        # choosing "distinct" when it may correspond to that derived leg.
        batch = web.post('/api/account-import/batches', json={'account_code':'bank_a','mode':'upload','kind':'cash'}).json()
        path = '/api/account-import/batches/' + batch['batch_id']
        content = f'transaction_date,amount,currency,balance,summary\n{day},-70,CNY,200,real-bank\n'.encode()
        batch = web.post(path + '/files?filename=bank70.csv', content=content).json()
        batch['rows'][0]['fields'].update(action='transfer',counter_account_code='tonghuashun',duplicate_decision='distinct')
        batch['rows'][0]['acknowledged'] = True
        batch = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']}).json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert not checked['can_publish']
        assert any(issue['code']=='derived_leg_requires_reconciliation' for row in checked['rows'] for issue in row['issues'])
        batch['rows'][0]['fields']['derived_cashflow_id'] = str(derived_trade['cashflow_id'])
        response = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']})
        assert response.status_code == 200, response.text
        batch = response.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert checked['can_publish'], checked
        from domain.ledger import bank_source_confirmation
        with monkeypatch.context() as patch:
            def fail_confirmation(*args, **kwargs):
                raise ValueError('synthetic confirmation failure')
            patch.setattr(bank_source_confirmation, 'confirm', fail_confirmation)
            failed = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
            assert failed.status_code >= 400
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM source_batches WHERE batch_id=%s", (batch['batch_id'],))
            assert cur.fetchone()[0] != 'published'
            cur.execute("SELECT count(*) FROM evidence_confirmation WHERE cashflow_id=%s", (derived_trade['cashflow_id'],))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-70")
            assert cur.fetchone()[0] == 1
        response = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert response.status_code == 200, response.text
        again = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
        assert again.status_code == 200 and again.json()['replayed'], again.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-70")
            assert cur.fetchone()['n'] == 1
            cur.execute("SELECT confirmation_id FROM evidence_confirmation WHERE cashflow_id=%s", (derived_trade['cashflow_id'],))
            confirmation_id = cur.fetchone()['confirmation_id']
            from domain.imports.source_row_read import source_row_relation
            relation = source_row_relation(cur)
            cur.execute(f"SELECT count(*) AS n FROM {relation} AS s WHERE account_code='bank_a' AND source_projection_origin='confirmed_bank_source'")
            assert cur.fetchone()['n'] == 1
        bank_imports = web.get('/api/bank-reconcile/imports')
        assert bank_imports.status_code == 200, bank_imports.text
        assert any(row['source_file'] == 'bank70.csv' for row in bank_imports.json()['imports'])
        # The original broker source and the newly reviewed bank statement are
        # now independent; the very same fact is eligible for confirmation.
        paired = web.post('/api/bank-reconcile/transfers/confirm', json={
            'from_kind':'cashflow','from_id':derived_trade['cashflow_id'], 'to_kind':'trade',
            'to_id':derived_trade['trade_id'], 'note':'真实银行资料与券商资料已核对', 'confirmed':True})
        assert paired.status_code == 200, paired.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            try:
                cur.execute("UPDATE evidence_confirmation SET note='tampered' WHERE confirmation_id=%s", (str(confirmation_id),))
                raise AssertionError('confirmation must be immutable')
            except psycopg2.errors.RaiseException:
                conn.rollback()
        revoked = web.post(f'/api/bank-reconcile/bank-sources/{confirmation_id}/revoke',
                           json={'note':'银行来源核准撤销','confirmed':True})
        assert revoked.status_code == 200, revoked.text
        matches = web.get('/api/bank-reconcile/transfer-matches').json()['matches']
        assert any(m['transfer_id']==paired.json()['transfer_id'] and m['evidence_status']=='invalidated' for m in matches)
        assert not any(row['source_file']=='bank70.csv' for row in web.get('/api/bank-reconcile/imports').json()['imports'])

        # Two matching real bank facts are ambiguous even when one ID is supplied.
        for index in range(2):
            batch = web.post('/api/account-import/batches', json={'account_code':'bank_a','mode':'upload','kind':'cash'}).json()
            path = '/api/account-import/batches/' + batch['batch_id']
            content = f'transaction_date,amount,currency,balance,summary\n{day},-80,CNY,200,bank-{index}\n'.encode()
            batch = web.post(path + '/files?filename=ambiguous.csv', content=content).json()
            batch['rows'][0]['fields'].update(action='transfer', counter_account_code='tonghuashun', duplicate_decision='distinct')
            batch['rows'][0]['acknowledged'] = True
            response = web.put(path, json={'revision':batch['revision'],'rows':batch['rows']})
            assert response.status_code == 200, response.text
            batch = response.json()
            checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
            assert checked['can_publish'], checked
            response = web.post(path+'/publish', json={'revision':batch['revision'],'preview_token':checked['preview_token']})
            assert response.status_code == 200, response.text
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code='bank_a' AND amount=-80 ORDER BY cashflow_id")
            ambiguous = [row['cashflow_id'] for row in cur.fetchall()]
            assert len(ambiguous) == 2
        batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'manual','kind':'cash'}).json()
        path = '/api/account-import/batches/' + batch['batch_id']
        fields = {'kind':'cash','business_date':day,'currency':'CNY','action':'deposit','net_cash':'80',
                  'counter_account_code':'bank_a','bank_cashflow_id':str(ambiguous[0])}
        response = web.put(path, json={'revision':batch['revision'],'rows':[{'fields':fields}]})
        assert response.status_code == 200, response.text
        batch = response.json()
        checked = web.post(path+'/preview', json={'revision':batch['revision']}).json()
        assert not checked['can_publish']
        assert any(issue['code']=='bank_confirmation_required' for row in checked['rows'] for issue in row['issues'])
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE account_code='tonghuashun' AND net_cash=80")
            assert cur.fetchone()[0] == 0
