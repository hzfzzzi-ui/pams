"""Isolated CLI and existing trade-fact publication using synthetic CSVs."""
# ruff: noqa: F811
import base64
import json
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import make_dsn
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from tests.database.test_tradingview_source import HEADER, ROW1, ROW2
from domain.imports.tradingview_source import stage_csvs
from domain.imports.tradingview_source_publish import publish, review_row
from domain.imports import tradingview_source_cli as cli

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    return params


def stage(params, content):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return stage_csvs(cur, batch_id=uuid4(), account_code='tonghuashun', files=[('input.csv', content)])['batch_id']


def review_all(cur, batch_id):
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (str(batch_id),))
    revision=cur.fetchone()['revision']
    cur.execute('SELECT source_item_id FROM source_items WHERE batch_id=%s ORDER BY source_uid', (str(batch_id),))
    items=cur.fetchall()
    for item in items:
        result=review_row(cur,batch_id=batch_id,item_id=item['source_item_id'],expected_revision=revision,note='synthetic row reviewed')
        revision=result['revision']
    return revision


def review_batch(params,batch_id):
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        return review_all(cur,batch_id)


def run_publish(params, batch_id, **kwargs):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return publish(cur, batch_id=batch_id, **kwargs)


def test_confirmed_publication_and_replay_leave_holdings_untouched(isolated):
    batch_id = stage(isolated, (HEADER + ROW1 + ROW2).encode())
    with pytest.raises(ValueError, match='explicit confirmation'):
        run_publish(isolated, batch_id)
    with pytest.raises(ValueError, match='individual review'):
        run_publish(isolated, batch_id, confirm=True)
    review_batch(isolated,batch_id)
    assert run_publish(isolated, batch_id, confirm=True)['published_count'] == 2
    assert run_publish(isolated, batch_id, confirm=True)['replayed']
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_item_id IS NOT NULL')[0]['n'] == 2
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0


def test_replay_blocks_changed_fact_content(isolated):
    batch_id = stage(isolated, (HEADER + ROW1).encode())
    review_batch(isolated,batch_id)
    run_publish(isolated, batch_id, confirm=True)
    sql(isolated, 'UPDATE pams_investment_trade_fact SET fee_amount=99')
    with pytest.raises(ValueError, match='Conflicting immutable trade source'):
        run_publish(isolated, batch_id, confirm=True)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1


def test_failure_rolls_back_source_and_fact(isolated):
    batch_id = stage(isolated, (HEADER + ROW1 + ROW2).encode())
    review_batch(isolated,batch_id)
    sql(isolated, "CREATE FUNCTION reject_tv_link() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic link error'; END $$")
    sql(isolated, 'CREATE TRIGGER reject_tv_link BEFORE UPDATE OF source_item_id ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION reject_tv_link()')
    with pytest.raises(psycopg2.errors.RaiseException):
        run_publish(isolated, batch_id, confirm=True)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0
    assert sql(isolated, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


def test_explicit_cli_stage_and_publish(isolated, monkeypatch, tmp_path, capsys):
    path = tmp_path / 'input.csv'
    path.write_text(HEADER + ROW1, encoding='utf-8')
    monkeypatch.setenv('PAMS_TEST_POSTGRES_DSN', make_dsn(**isolated))
    assert cli.main(['stage', '--account', 'tonghuashun', str(path)]) == 0
    batch_id = json.loads(capsys.readouterr().out)['batch_id']
    assert cli.main(['inspect',batch_id]) == 0
    inspection = json.loads(capsys.readouterr().out)
    item=inspection['items'][0]; file=inspection['files'][0]
    assert item['raw_fields']['raw_fields']['Note'] == 'first' and item['review_status']=='pending'
    assert cli.main(['inspect',batch_id,'--file-id',file['file_id']]) == 0
    original = json.loads(capsys.readouterr().out)
    assert original['original_csv'] == path.read_bytes().decode('utf-8-sig')
    assert base64.b64decode(original['original_base64']) == path.read_bytes()
    with pytest.raises(ValueError,match='individual review'):
        cli.main(['publish',batch_id,'--confirm'])
    assert cli.main(['review',batch_id,item['source_item_id'],'--revision',str(inspection['batch']['revision']),'--note','checked row']) == 0
    assert json.loads(capsys.readouterr().out)['review_status']=='confirmed'
    assert cli.main(['publish', batch_id, '--confirm']) == 0
    assert json.loads(capsys.readouterr().out)['published_count'] == 1



def test_each_row_requires_current_revision_and_explicit_confirmation(isolated):
    batch=stage(isolated,(HEADER+ROW1+ROW2).encode())
    with psycopg2.connect(**isolated) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(batch,))
        revision=cur.fetchone()['revision']
        cur.execute('SELECT source_item_id FROM source_items WHERE batch_id=%s ORDER BY source_uid',(batch,))
        ids=[row['source_item_id'] for row in cur.fetchall()]
        with pytest.raises(ValueError,match='requires a note'):
            review_row(cur,batch_id=batch,item_id=ids[0],expected_revision=revision,note='  ')
        result=review_row(cur,batch_id=batch,item_id=ids[0],expected_revision=revision,note='first checked')
        with pytest.raises(ValueError,match='stale'):
            review_row(cur,batch_id=batch,item_id=ids[1],expected_revision=revision,note='stale attempt')
        with pytest.raises(ValueError,match='individual review'):
            publish(cur,batch_id=batch,confirm=True)
        cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
        assert cur.fetchone()['n']==0
        review_row(cur,batch_id=batch,item_id=ids[1],expected_revision=result['revision'],note='second checked')
        assert publish(cur,batch_id=batch,confirm=True)['published_count']==2
        cur.execute('SELECT review_note FROM source_items WHERE batch_id=%s ORDER BY source_uid',(batch,))
        assert [row['review_note'] for row in cur.fetchall()]==['first checked','second checked']
    assert run_publish(isolated,batch,confirm=True)['replayed']


def test_review_rechecks_original_file_and_leaves_fact_absent_on_tamper(isolated):
    batch=stage(isolated,(HEADER+ROW1).encode())
    with psycopg2.connect(**isolated) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(batch,))
        revision=cur.fetchone()['revision']
        cur.execute('SELECT source_item_id FROM source_items WHERE batch_id=%s',(batch,))
        item=cur.fetchone()['source_item_id']
        cur.execute('SAVEPOINT before_tamper')
        cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
        cur.execute("UPDATE source_files SET content=convert_to('tampered','UTF8') WHERE batch_id=%s",(batch,))
        cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        with pytest.raises(ValueError,match='invalid or ambiguous|evidence changed'):
            review_row(cur,batch_id=batch,item_id=item,expected_revision=revision,note='checked')
        cur.execute('ROLLBACK TO SAVEPOINT before_tamper')
        cur.execute('SELECT review_status FROM source_items WHERE source_item_id=%s',(str(item),))
        assert cur.fetchone()['review_status']=='pending'
        cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
        assert cur.fetchone()['n']==0
        assert review_row(cur,batch_id=batch,item_id=item,expected_revision=revision,note='checked')['review_status']=='confirmed'

def test_cli_rejects_non_disposable_dsn_before_connect(monkeypatch, tmp_path):
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_TEST_POSTGRES_DSN', 'host=127.0.0.1 dbname=production user=invalid')
    monkeypatch.setattr(cli.psycopg2, 'connect', lambda *a, **k: pytest.fail('unexpected database connection'))
    path = tmp_path / 'input.csv'
    path.write_text(HEADER + ROW1, encoding='utf-8')
    with pytest.raises(ValueError, match='disposable'):
        cli.main(['stage', '--account', 'tonghuashun', str(path)])
