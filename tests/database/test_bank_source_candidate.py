"""Bank statement evidence and cash facts using synthetic isolated data."""
import os
import sys
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import Json, RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))

from collectors.bank_statement_parser import parse_statement
from domain.imports.bank_source_candidate import publish_bank_source, stage_bank_source
from domain.imports.unified_source import lock_source_review, review_item
from domain.ledger.cashflow_fact import build_cashflow_fact, insert_cashflow_facts

from scripts.bootstrap_empty_database import initialize_empty_database
from tests.support.account_import_support import scratch_database

CONTENT = (b'transaction_date,amount,currency,balance,summary,description\n'
           b'2026-01-01,-25.10,CNY,974.90,one,first\n'
           b'2026-01-02,50.00,CNY,1024.90,two,second\n')
PROFILE = {'profile_code': 'synthetic', 'account_code': 'bank_test', 'account_kind': 'manual',
           'bank': 'Manual', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}


@pytest.fixture
def bank_data(tmp_path, monkeypatch):
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    path = tmp_path / 'synthetic.csv'
    path.write_bytes(CONTENT)
    parsed = parse_statement(path, PROFILE)
    with scratch_database() as params:
        with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            for name in ('022_bank_reconcile.sql', '074_unified_source.sql', '075_source_fact_links.sql'):
                cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
            cur.execute("""CREATE TABLE pams_reconciliation_case (
                case_id bigint PRIMARY KEY, source_ref_table text, source_ref_id text,
                target_ref_table text, target_ref_id text, status text, note text, reviewed_at timestamptz)""")
            cur.execute("""INSERT INTO pams_bank_statement_imports(profile_code,bank,account_code,account_kind,source_file,row_count)
                           VALUES ('synthetic','Manual','bank_test','manual',%s,2) RETURNING id""", (str(path),))
            import_id = cur.fetchone()['id']
            for row in parsed.rows:
                cur.execute("""INSERT INTO pams_bank_statement_rows(import_id,source_uid,profile_code,bank,account_code,
                   account_kind,transaction_date,posting_date,amount,currency,balance,original_amount,original_currency,
                   category,subcategory,summary,description,counterparty,raw_json)
                   VALUES (%s,%s,'synthetic','Manual','bank_test','manual',%s,%s,%s,%s,%s,%s,%s,
                           'category','subcategory',%s,%s,%s,%s)""",
                   (import_id, row['source_uid'], row['transaction_date'], row['posting_date'], row['amount'],
                    row['currency'], row['balance'], row['original_amount'], row['original_currency'], row['summary'],
                    row['description'], row['counterparty'], Json(row['raw'])))
            cur.execute('SELECT * FROM pams_bank_statement_rows ORDER BY id')
            first, _ = cur.fetchall()
            cur.execute("""INSERT INTO pams_reconciliation_case VALUES
                (1,'pams_bank_statement_rows',%s,NULL,NULL,'confirmed','manual decision',now())""", (str(first['id']),))
            cur.execute("""INSERT INTO pams_bank_transfer_matches(match_key,match_type,relation_code,from_account_code,
                to_account_code,source_row_id,amount,currency,status,note) VALUES
                ('confirmed-pair','investment_funding','manual','bank_test','tonghuashun',%s,25.10,'CNY','confirmed','keep this note')""", (first['id'],))
            cur.execute('SELECT * FROM pams_bank_transfer_matches')
            match = dict(cur.fetchone())
            old_fact = build_cashflow_fact(first, match)
            old_fact['review_status'] = 'confirmed'
            assert insert_cashflow_facts(cur, [old_fact]) == 1
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact')
            cashflow_id = cur.fetchone()['cashflow_id']
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,trade_type,net_cash,
                           cashflow_id,source_table,source_id,source_hash,review_status)
                           VALUES ('2026-01-01','tonghuashun','deposit',25.10,%s,'synthetic_trade',
                                   'funding-leg',%s,'confirmed')""", (cashflow_id, 'f' * 64))
        yield params, import_id


def reviewed(params, import_id):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        batch = stage_bank_source(cur, import_id=import_id, profile=PROFILE, filename='synthetic.csv', content=CONTENT)
        cur.execute('SELECT source_item_id,parsed_fields FROM source_items WHERE batch_id=%s ORDER BY source_uid', (str(batch),))
        items = cur.fetchall()
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (str(batch),))
        rev = cur.fetchone()['revision']
        for item in items:
            rev = review_item(cur, batch_id=batch, item_id=item['source_item_id'], expected_revision=rev,
                              parsed_fields=item['parsed_fields'], review_status='confirmed', review_note='reviewed')
        snap = lock_source_review(cur, batch_id=batch, expected_revision=rev)
        return batch, rev, snap['source_snapshot_sha256']


def test_original_to_fact_is_atomic_idempotent_and_preserves_confirmation(bank_data):
    params, import_id = bank_data
    batch, rev, digest = reviewed(params, import_id)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)
            assert not result['replayed'] and len(result['records']) == 2
            cur.execute("SELECT * FROM pams_cashflow_fact WHERE review_status='confirmed'")
            fact = cur.fetchone()
            assert fact['description'] == 'first' and fact['transfer_group_id'] == 'confirmed-pair'
            cur.execute('SELECT cashflow_id,review_status FROM pams_investment_trade_fact')
            funding = cur.fetchone()
            assert funding['cashflow_id'] == fact['cashflow_id'] and funding['review_status'] == 'confirmed'
            cur.execute('SELECT status,note FROM pams_bank_transfer_matches')
            assert tuple(cur.fetchone().values()) == ('confirmed', 'keep this note')
            cur.execute('SELECT status,note FROM pams_reconciliation_case')
            assert tuple(cur.fetchone().values()) == ('confirmed', 'manual decision')
            cur.execute('SELECT content FROM source_files WHERE batch_id=%s', (str(batch),))
            assert bytes(cur.fetchone()['content']) == CONTENT
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert stage_bank_source(cur, import_id=import_id, profile=PROFILE, filename='synthetic.csv', content=CONTENT) == batch
            assert publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)['replayed']
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 2


def test_unverified_original_and_changed_confirmation_are_rejected(bank_data):
    params, import_id = bank_data
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ValueError, match='original does not match'):
            stage_bank_source(cur, import_id=import_id, profile=PROFILE, filename='synthetic.csv',
                              content=CONTENT.replace(b'first', b'changed'))
        cur.execute('SELECT count(*) AS n FROM source_batches')
        assert cur.fetchone()['n'] == 0
    _, rev, digest = reviewed(params, import_id)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE pams_bank_transfer_matches SET note='changed'")
            with pytest.raises(ValueError, match='unchanged confirmed'):
                publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 1
            conn.rollback()


def test_fact_failure_rolls_back_all_associations(bank_data):
    params, import_id = bank_data
    batch, rev, digest = reviewed(params, import_id)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""CREATE FUNCTION reject_synthetic_fact() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic failure'; END $$""")
        cur.execute('CREATE TRIGGER reject_synthetic_fact BEFORE INSERT ON pams_cashflow_fact FOR EACH ROW EXECUTE FUNCTION reject_synthetic_fact()')
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(psycopg2.errors.RaiseException):
                publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)
            cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (str(batch),))
            assert cur.fetchone()['status'] == 'draft'
            cur.execute('SELECT source_item_id FROM pams_cashflow_fact')
            assert cur.fetchone()['source_item_id'] is None


def test_duplicate_source_identity_rejects_whole_publication(bank_data):
    params, import_id = bank_data
    batch, rev, digest = reviewed(params, import_id)
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM pams_bank_statement_rows ORDER BY id DESC LIMIT 1')
        duplicate = dict(cur.fetchone())
        duplicate['id'] = 999999
        assert insert_cashflow_facts(cur, [build_cashflow_fact(duplicate)]) == 1
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='duplicate bank source identity'):
                publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)
            cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (str(batch),))
            assert cur.fetchone()['status'] == 'draft'
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact WHERE source_item_id IS NOT NULL')
            assert cur.fetchone()['n'] == 0


def test_failed_staging_rolls_back_candidate_rows(bank_data):
    params, import_id = bank_data
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""CREATE FUNCTION reject_synthetic_source_item() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic source failure'; END $$""")
        cur.execute('CREATE TRIGGER reject_synthetic_source_item BEFORE INSERT ON source_items FOR EACH ROW EXECUTE FUNCTION reject_synthetic_source_item()')
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(psycopg2.errors.RaiseException):
            stage_bank_source(cur, import_id=import_id, profile=PROFILE, filename='synthetic.csv', content=CONTENT)
        cur.execute('SELECT count(*) AS n FROM source_batches')
        assert cur.fetchone()['n'] == 0
        cur.execute('SELECT count(*) AS n FROM source_files')
        assert cur.fetchone()['n'] == 0


def test_candidate_rejects_disabled_isolation(bank_data, monkeypatch):
    params, import_id = bank_data
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    with (psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur,
          pytest.raises(ValueError, match='isolated database')):
        stage_bank_source(cur, import_id=import_id, profile=PROFILE, filename='synthetic.csv', content=CONTENT)


def test_confirmed_reconciliation_cannot_be_ignored_when_fact_missing(bank_data):
    params, import_id = bank_data
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT id FROM pams_bank_statement_rows ORDER BY id DESC LIMIT 1')
        row_id = str(cur.fetchone()['id'])
        cur.execute("""INSERT INTO pams_reconciliation_case VALUES
            (2,NULL,NULL,'pams_bank_statement_rows',%s,'confirmed','do not discard',now())""", (row_id,))
    batch, rev, digest = reviewed(params, import_id)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='requires explicit fact mapping'):
                publish_bank_source(cur, import_id=import_id, expected_revision=rev, source_snapshot_sha256=digest)
            cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (str(batch),))
            assert cur.fetchone()['status'] == 'draft'
            cur.execute('SELECT note FROM pams_reconciliation_case WHERE case_id=2')
            assert cur.fetchone()['note'] == 'do not discard'


def test_concurrent_bank_publication_is_idempotent(bank_data):
    params, import_id = bank_data
    _, rev, digest = reviewed(params, import_id)
    barrier = Barrier(2)

    def attempt():
        conn = psycopg2.connect(**params)
        try:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                barrier.wait(timeout=5)
                return publish_bank_source(cur, import_id=import_id, expected_revision=rev,
                                           source_snapshot_sha256=digest)['replayed']
        except psycopg2.errors.SerializationFailure:
            return 'retry'
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))
    assert outcomes.count(False) == 1 and all(value in (False, True, 'retry') for value in outcomes)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert publish_bank_source(cur, import_id=import_id, expected_revision=rev,
                                       source_snapshot_sha256=digest)['replayed']
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 2


def test_bank_candidate_with_full_bootstrap_schema(tmp_path, monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Explicit disposable PostgreSQL DSN required')
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
        cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-empty-bootstrap'").format(sql.Identifier(name)))
    target = {**params, 'dbname': name}
    try:
        with psycopg2.connect(**target) as conn:
            initialize_empty_database(conn)
        with admin.cursor() as cur:
            cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
        monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
        path = tmp_path / 'synthetic.csv'
        path.write_bytes(CONTENT)
        parsed = parse_statement(path, PROFILE)
        with psycopg2.connect(**target) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_test','Synthetic','CNY')")
            # Only this test models an old database; new bootstrap does not
            # create the legacy source table.
            cur.execute("""CREATE TABLE public.pams_source_row (
                source_row_id bigserial PRIMARY KEY, source_system text NOT NULL,
                source_table text NOT NULL, source_id text NOT NULL, source_uid text,
                account_code text, event_date date, source_file text, row_hash text,
                amount numeric(28,10), currency text, raw_json jsonb NOT NULL DEFAULT '{}',
                imported_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (source_table, source_id))""")
            cur.execute("""INSERT INTO pams_bank_statement_imports(profile_code,bank,account_code,account_kind,source_file,row_count)
                           VALUES ('synthetic','Manual','bank_test','manual',%s,2) RETURNING id""", (str(path),))
            import_id = cur.fetchone()['id']
            for row in parsed.rows:
                cur.execute("""INSERT INTO pams_bank_statement_rows(import_id,source_uid,profile_code,bank,account_code,
                   account_kind,transaction_date,posting_date,amount,currency,balance,original_amount,original_currency,
                   summary,description,counterparty,raw_json)
                   VALUES (%s,%s,'synthetic','Manual','bank_test','manual',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (import_id, row['source_uid'], row['transaction_date'], row['posting_date'], row['amount'], row['currency'],
                    row['balance'], row['original_amount'], row['original_currency'], row['summary'], row['description'],
                    row['counterparty'], Json(row['raw'])))
            cur.execute("""INSERT INTO pams_source_row
                (source_system,source_table,source_id,source_uid,account_code,event_date,imported_at,created_at)
                SELECT 'bank_statement_row','pams_bank_statement_rows',id::text,source_uid,account_code,
                       transaction_date,created_at,'2025-12-31T00:00:00Z'
                FROM pams_bank_statement_rows WHERE import_id=%s""", (import_id,))
            cur.execute("""INSERT INTO pams_source_row
                (source_system,source_table,source_id,source_uid,account_code,event_date,imported_at,created_at)
                VALUES ('bank_statement_row','pams_bank_statement_rows','unmigrated','no-evidence',
                        'bank_test',NULL,NULL,'2025-12-31T00:00:00Z')""")
        _, revision, digest = reviewed(target, import_id)
        with psycopg2.connect(**target) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert len(publish_bank_source(cur, import_id=import_id, expected_revision=revision,
                                               source_snapshot_sha256=digest)['records']) == 2
        from domain.imports.source_row_read import source_row_relation
        from domain.ledger.ledger_view import _load_cash_rows, _load_investment_account_rows
        from endpoints.accounts import _fetch_statement_balances
        with psycopg2.connect(**target) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM source_items WHERE raw_fields ? 'legacy_source_row'")
            assert cur.fetchone()['n'] == 2

            def view(isolated):
                if isolated:
                    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
                else:
                    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED', raising=False)
                relation = source_row_relation(cur)
                cur.execute(f"""SELECT source_system,source_table,source_id,source_uid,account_code,event_date,imported_at,created_at
                    FROM {relation} AS s ORDER BY source_table,source_id""")
                rows = [dict(row) for row in cur.fetchall()]
                cur.execute(f"""SELECT max(event_date) AS as_of,max(created_at) AS updated_at,count(*) AS rows
                    FROM {relation} AS s WHERE source_table='pams_bank_statement_rows'""")
                health = dict(cur.fetchone())
                accounts = _fetch_statement_balances(cur)
                cash = _load_cash_rows(cur, start=date(2026,1,1), end=date(2026,1,31), base_currency='CNY')
                investments = _load_investment_account_rows(cur, start=date(2026,1,1), end=date(2026,1,31), base_currency='CNY')
                return rows, health, accounts, cash, investments

            assert view(True) == view(False)
            monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
            cur.execute(f"SELECT source_projection_origin,count(*) AS n FROM {source_row_relation(cur)} AS s GROUP BY source_projection_origin ORDER BY source_projection_origin")
            assert [dict(row) for row in cur.fetchall()] == [
                {'source_projection_origin': 'legacy_source_row', 'n': 1},
                {'source_projection_origin': 'unified_source_item', 'n': 2},
            ]
            # Once a legacy projection changes, the bridge must fall back to
            # its actual timestamp instead of replaying stale source evidence.
            cur.execute("UPDATE pams_source_row SET imported_at='2026-02-01T00:00:00Z' WHERE source_id=(SELECT min(source_id) FROM pams_source_row)")
            assert view(True) == view(False)
            monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
            cur.execute(f"SELECT source_projection_origin,count(*) AS n FROM {source_row_relation(cur)} AS s GROUP BY source_projection_origin ORDER BY source_projection_origin")
            assert [dict(row) for row in cur.fetchall()] == [
                {'source_projection_origin': 'legacy_source_row', 'n': 2},
                {'source_projection_origin': 'unified_source_item', 'n': 1},
            ]
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()
