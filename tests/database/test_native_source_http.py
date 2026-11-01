"""Source-first HTTP contract on a disposable PostgreSQL database."""
# ruff: noqa: F811
import importlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg2
import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))

from domain.imports import account_import_statement as statement
from domain.imports import account_import_store as store

from tests.imports.test_account_import_publish import (  # noqa: F401
    DAY,
    client,
    draft,
    holding,
    preview,
    publish,
    sql,
    trade,
)


@pytest.fixture
def isolated(client, monkeypatch):
    web, params = client
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    return web, params


def test_no_fallback_when_isolation_is_disabled_or_old_switch_is_set(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setenv('PAMS_SOURCE_LEGACY_BRIDGE','true')
    batch = draft(web,[trade(source_ref='single-source-only')])
    check = preview(web,batch)
    assert check['can_publish'] and publish(web,batch,check).status_code == 200
    assert sql(params,"SELECT to_regclass('public.pams_account_import_batch') AS old_batch")[0]['old_batch'] is None
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    assert web.get('/api/account-import/capabilities').json()['schema_ready'] is False
    response = web.post('/api/account-import/batches',json={'account_code':'tonghuashun','mode':'manual','kind':'trade'})
    assert response.status_code == 403 and response.json()['detail']['code'] == 'isolated_only'
    assert sql(params,'SELECT count(*) AS n FROM source_batches')[0]['n'] == 1


@pytest.mark.parametrize('fields', [trade(source_ref='http-source-first'), holding()])
def test_manual_http_preserves_existing_contract_without_old_source_writes(isolated, fields):
    web, params = isolated
    batch = draft(web, [fields])
    check = preview(web, batch)
    assert check['can_publish']
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    assert not result.json()['replayed']
    assert publish(web, batch, check).json()['replayed']
    assert sql(params, "SELECT to_regclass('public.pams_account_import_batch') AS old_batch, to_regclass('public.pams_account_import_file') AS old_file")[0] == {'old_batch':None, 'old_file':None}
    assert len(sql(params, 'SELECT * FROM source_items')) == 1


def test_csv_bank_source_retains_original_and_writes_cash_fact(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    batch = web.post('/api/account-import/batches', json={'account_code':'bank_test','mode':'upload','kind':'cash'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    content = f'transaction_date,amount,currency,balance,summary\n{DAY},100.05,CNY,200.05,pay\n'.encode()
    response = web.post(path+'/files?filename=bank.csv', content=content)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert len(batch['rows']) == 2 and batch['rows'][1]['excluded']
    original = web.get(path + '/files/' + batch['files'][0]['file_id'])
    assert original.content == content
    assert not preview(web, batch)['can_publish']
    saved = web.put(path, json={'revision': batch['revision'], 'rows': batch['rows']})
    assert saved.status_code == 200, saved.text
    batch = saved.json()
    check = preview(web, batch)
    assert check['can_publish']
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    assert len(sql(params, 'SELECT * FROM pams_cashflow_fact')) == 1
    assert sql(params, "SELECT to_regclass('public.pams_account_import_batch') AS old_batch, to_regclass('public.pams_account_import_file') AS old_file")[0] == {'old_batch':None, 'old_file':None}


def test_ocr_per_image_confirmation_invalidates_only_changed_image(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda content, *args: {'origin':'ocr', 'rows':[
        {'fields': trade(source_ref=content.decode(), quantity='100' if content == b'one' else '200',
                         gross_amount='1000' if content == b'one' else '2000',
                         net_cash='-1005' if content == b'one' else '-2005'),
         'source': {'row': 1}, 'issues': []}]})
    batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'upload','kind':'trade'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    for content in (b'one', b'two'):
        response = web.post(path+'/files?filename=screen.png', content=content)
        assert response.status_code == 200, response.text
        batch = response.json()
    assert not preview(web, batch)['can_publish']
    for file in batch['files']:
        response = web.post(path+'/files/'+file['file_id']+'/review', json={'revision':batch['revision']})
        assert response.status_code == 200, response.text
        batch = response.json()
    assert preview(web, batch)['can_publish']
    edited = deepcopy(batch['rows'])
    edited[0]['fields']['note'] = 'changed after review'
    batch = web.put(path, json={'revision':batch['revision'], 'rows':edited}).json()
    assert not preview(web, batch)['can_publish']
    statuses = sql(params, 'SELECT review_status FROM source_items ORDER BY source_order')
    assert [row['review_status'] for row in statuses] == ['pending', 'confirmed']


def test_native_http_funding_has_both_legs_and_rollback_on_failure(isolated):
    web, params = isolated
    fields = {'kind':'cash', 'business_date':DAY, 'currency':'CNY', 'action':'deposit',
              'net_cash':'500', 'counter_account_code':'bank_test'}
    batch = draft(web, [fields])
    check = preview(web, batch)
    assert check['can_publish']
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    cash = sql(params, 'SELECT * FROM pams_cashflow_fact')[0]
    trade_fact = sql(params, 'SELECT * FROM pams_investment_trade_fact')[0]
    assert cash['cashflow_id'] == trade_fact['cashflow_id'] and cash['amount'] == -500
    assert cash['source_item_id'] == trade_fact['source_item_id']


def test_xlsx_holding_preserves_original_and_requires_review(isolated):
    web, params = isolated
    book = Workbook()
    book.active.append(['business_date', 'asset_type', 'symbol', 'currency', 'quantity', 'price',
                        'market_value', 'cost', 'cost_basis_type'])
    book.active.append([DAY, 'stock', '000001', 'CNY', '100', '10', '1000', '900', 'holding_cost'])
    output = BytesIO()
    book.save(output)
    content = output.getvalue()
    batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'upload','kind':'holding'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    response = web.post(path+'/files?filename=holding.xlsx', content=content)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert not preview(web, batch)['can_publish']
    assert web.get(path+'/files/'+batch['files'][0]['file_id']).content == content
    saved = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
    assert saved.status_code == 200, saved.text
    batch = saved.json()
    check = preview(web, batch)
    assert check['can_publish'], check
    result = publish(web, batch, check)
    assert result.status_code == 200, result.text
    assert len(sql(params, 'SELECT * FROM pams_holdings_snapshot')) == 1
    assert sql(params, "SELECT to_regclass('public.pams_account_import_batch') AS old_batch, to_regclass('public.pams_account_import_file') AS old_file")[0] == {'old_batch':None, 'old_file':None}


def test_native_http_reparse_and_stale_preview_are_safe(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda content, filename, account, kind, mapping=None: {
        'origin':'file', 'rows':[{'fields':trade(note='mapped' if mapping else 'initial'), 'source':{'row':1}, 'issues':[]}]})
    batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun','mode':'upload','kind':'trade'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    batch = web.post(path+'/files?filename=trades.csv', content=b'synthetic').json()
    original_row = batch['rows'][0]['row_id']
    file_id = batch['files'][0]['file_id']
    batch = web.post(path+'/files/'+file_id+'/parse', json={'mapping':{'symbol':'ticker'}}).json()
    assert batch['rows'][0]['row_id'] != original_row and batch['rows'][0]['fields']['note'] == 'mapped'
    assert web.get(path+'/files/'+file_id).content == b'synthetic'
    batch = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']}).json()
    check = preview(web, batch)
    newer = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']}).json()
    response = publish(web, newer, check)
    assert response.status_code == 409, response.text
    assert not sql(params, 'SELECT * FROM pams_investment_trade_fact')


def test_two_http_publications_cannot_duplicate_facts(isolated):
    web, params = isolated
    batch = draft(web, [trade(source_ref='concurrent-http')])
    check = preview(web, batch)
    barrier = Barrier(2)

    def attempt(_):
        barrier.wait(timeout=5)
        response = publish(web, batch, check)
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(attempt, range(2)))
    assert 200 in codes and all(code in (200, 409) for code in codes), codes
    assert len(sql(params, 'SELECT * FROM pams_investment_trade_fact')) == 1


def test_preview_token_survives_worker_reload_and_replay(isolated):
    from domain.imports import native_source_http

    web, params = isolated
    batch = draft(web, [trade(source_ref='worker-restart')])
    check = preview(web, batch)
    importlib.reload(native_source_http)
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    importlib.reload(native_source_http)
    assert publish(web, batch, check).json()['replayed']
    assert len(sql(params, 'SELECT * FROM pams_investment_trade_fact')) == 1


def test_preview_token_matches_in_separate_process(isolated):
    from domain.imports.native_source_http import _token

    code = ('import sys; sys.path.insert(0,sys.argv[1]); '
            'from domain.imports.native_source_http import _token; '
            'print(_token(sys.argv[2],sys.argv[3],int(sys.argv[4]),int(sys.argv[5])))')
    args = [sys.executable, '-c', code, str(ROOT / 'backend/api'), 'a' * 64, 'b' * 64, '3', '12345']
    other = subprocess.run(args, check=True, capture_output=True, text=True, env=os.environ.copy())
    assert other.stdout.strip() == _token('a' * 64, 'b' * 64, 3, 12345)


def test_explicit_isolated_key_required_for_preview(isolated, monkeypatch):
    web, params = isolated
    batch = draft(web, [trade(source_ref='missing-key')])
    monkeypatch.delenv('PAMS_SOURCE_TEST_TOKEN_KEY')
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/preview", json={'revision':batch['revision']})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'source_token_unconfigured'
    assert not sql(params, 'SELECT * FROM pams_investment_trade_fact')


def test_http_failure_rolls_back_first_bank_leg_and_source_state(isolated):
    web, params = isolated
    fields = {'kind':'cash', 'business_date':DAY, 'currency':'CNY', 'action':'deposit',
              'net_cash':'500', 'counter_account_code':'bank_test'}
    batch = draft(web, [fields])
    check = preview(web, batch)
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION reject_native_http_trade() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic failure'; END $$""")
        cur.execute('CREATE TRIGGER reject_native_http_trade BEFORE INSERT ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION reject_native_http_trade()')
    assert publish(web, batch, check).status_code == 503
    assert not sql(params, 'SELECT * FROM pams_cashflow_fact')
    assert not sql(params, 'SELECT * FROM pams_investment_trade_fact')
    assert sql(params, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


def test_other_source_workflows_do_not_leak_into_import_http(isolated):
    web, params = isolated
    batch = draft(web, [trade(source_ref='visible-batch')])
    other = str(uuid4())
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                       VALUES (%s,'tonghuashun','trade','synthetic','api')""", (other,))
    result = web.get('/api/account-import/batches').json()
    assert [row['batch_id'] for row in result['rows']] == [batch['batch_id']]
    assert web.get('/api/account-import/batches/'+other).status_code == 404


@pytest.mark.parametrize('origin,filename', [('file','holdings.csv'), ('ocr','screen.png')])
def test_uploaded_holding_current_source_matches_reviewed_values(isolated, monkeypatch, origin, filename):
    from psycopg2.extras import RealDictCursor
    from domain.investments.holding_evidence_writer import verify_historical_holding_source
    from domain.investments.valuation_input_evidence import current_holding_source
    web, params = isolated
    monkeypatch.setattr(store, 'parse', lambda content, *args: {'origin':origin, 'rows':[
        {'fields':holding(), 'source':{'row':1}, 'issues':[]}]})
    batch = web.post('/api/account-import/batches', json={'account_code':'tonghuashun',
        'mode':'upload','kind':'holding'}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    upload = web.post(path + '/files?filename=' + filename, content=b'synthetic')
    assert upload.status_code == 200, upload.text
    batch = upload.json()
    if origin == 'ocr':
        reviewed = web.post(path + '/files/' + batch['files'][0]['file_id'] + '/review',
                            json={'revision':batch['revision']})
        assert reviewed.status_code == 200, reviewed.text
        batch = reviewed.json()
    else:
        saved = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
        assert saved.status_code == 200, saved.text
        batch = saved.json()
    check = preview(web, batch)
    assert check['can_publish'], check
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    row = sql(params, 'SELECT id FROM pams_holdings_snapshot')[0]
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with monkeypatch.context() as disabled:
                disabled.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'false')
                assert current_holding_source(cur, row['id']) == {'status':'recorded', 'source_item_id':batch['rows'][0]['row_id']}
                assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'])
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        proof_cache = {}
        assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
        cur.execute('SAVEPOINT evidence_corruption')
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE file_id=%s", (batch['files'][0]['file_id'],))
        cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        assert not verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
        assert current_holding_source(cur, row['id'])['status'] == 'unknown'
        cur.execute('ROLLBACK TO SAVEPOINT evidence_corruption')
        assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
        assert current_holding_source(cur, row['id'])['status'] == 'recorded'
        cur.execute('SAVEPOINT extra_item')
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status)
            VALUES (%s,%s,'unexpected','manual',%s,'excluded')""", (str(uuid4()), batch['batch_id'], '0'*64))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
        assert not verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
        cur.execute('ROLLBACK TO SAVEPOINT extra_item')
        assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
    # Only the disposable database is altered to model lost/corrupt original bytes.
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
    # A savepoint rollback releases acquired row locks. Cache reuse must lock
    # all source rows again before another connection can mutate them.
    try:
        with psycopg2.connect(**params) as lock_conn, lock_conn.cursor(cursor_factory=RealDictCursor) as lock_cur:
            proof_cache = {}
            lock_cur.execute('SAVEPOINT cached_lock')
            assert verify_historical_holding_source(lock_cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
            lock_cur.execute('ROLLBACK TO SAVEPOINT cached_lock')
            assert verify_historical_holding_source(lock_cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
            with psycopg2.connect(**params) as writer, writer.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='200ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE file_id=%s", (batch['files'][0]['file_id'],))
                writer.rollback()
                cur.execute("SET LOCAL lock_timeout='200ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status)
                        VALUES (%s,%s,'concurrent','manual',%s,'excluded')""", (str(uuid4()), batch['batch_id'], '0'*64))
                writer.rollback()
            lock_conn.rollback()
    except Exception:
        with psycopg2.connect(**params) as conn, conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
        raise
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
    snapshot_conn = psycopg2.connect(**params)
    write_conn = psycopg2.connect(**params)
    proof_cache = {}
    try:
        with write_conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
        write_conn.commit()
        snapshot_conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with snapshot_conn.cursor(cursor_factory=RealDictCursor) as snapshot_cur:
            assert current_holding_source(snapshot_cur, row['id'])['status'] == 'recorded'
            assert verify_historical_holding_source(snapshot_cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'])
            with psycopg2.connect(**params) as conn, conn.cursor() as cur:
                cur.execute("UPDATE source_files SET content=content || decode('00','hex') WHERE file_id=%s", (batch['files'][0]['file_id'],))
            # The original read-only transaction must retain one coherent MVCC snapshot.
            assert current_holding_source(snapshot_cur, row['id'])['status'] == 'recorded'
            assert verify_historical_holding_source(snapshot_cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'])
            with psycopg2.connect(**params) as conn:
                conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    assert current_holding_source(cur, row['id']) == {'status':'unknown', 'source_item_id':None}
                    assert not verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'])
            with write_conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert not verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
            write_conn.rollback()
            with psycopg2.connect(**params) as other, other.cursor(cursor_factory=RealDictCursor) as cur:
                assert not verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
                other.rollback()
            assert publish(web, batch, check).status_code >= 400
            with psycopg2.connect(**params) as conn, conn.cursor() as cur:
                cur.execute('UPDATE source_files SET content=%s WHERE file_id=%s', (psycopg2.Binary(b'synthetic'), batch['files'][0]['file_id']))
            with write_conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert verify_historical_holding_source(cur, holding_id=row['id'], source_item_id=batch['rows'][0]['row_id'], snapshot_cache=proof_cache)
            write_conn.rollback()
    finally:
        snapshot_conn.close()
        write_conn.close()
        with psycopg2.connect(**params) as conn, conn.cursor() as cur:
            cur.execute('UPDATE source_files SET content=%s WHERE file_id=%s', (psycopg2.Binary(b'synthetic'), batch['files'][0]['file_id']))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
    assert publish(web, batch, check).json()['replayed']
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert current_holding_source(cur, row['id']) == {'status':'recorded', 'source_item_id':batch['rows'][0]['row_id']}
