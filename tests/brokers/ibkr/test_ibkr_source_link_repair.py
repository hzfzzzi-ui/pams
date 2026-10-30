"""Production stays read-only; approved writes are confined to disposable databases."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from psycopg2.extras import Json
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import ibkr_source_link_repair as tool  # noqa: E402
from domain.brokers.ibkr.ibkr_lineage_rehearsal import fingerprint, protected_facts_fingerprint  # noqa: E402
from tests.brokers.ibkr.test_ibkr_lineage_rehearsal import make_case  # noqa: E402
from tests.brokers.ibkr.test_ibkr_sync_transaction import (  # noqa: E402
    database as database, sync_case as sync_case, rows, business_state, ObservedConnection,
)

MIGRATION = ROOT / 'database/postgres/schema/070_ibkr_source_link_repair_audit.sql'
AUDIT = 'pams_ibkr_source_link_repair_audit'


def approve(request):
    now = tool.utc_now()
    return {'purpose': tool.PURPOSE, 'request_sha256': request['request_sha256'],
            'approved_by': 'synthetic-test-approver', 'reference': 'isolated test fixture, NOT production permission',
            'issued_at': (now - timedelta(minutes=1)).isoformat(),
            'expires_at': (now + timedelta(hours=1)).isoformat(), 'accepted_risks': request['risks']}


@pytest.fixture
def batch(sync_case, database, tmp_path):
    client, _, report = sync_case
    initial = database()
    try:
        with initial, initial.cursor() as cur:
            cur.execute("DELETE FROM pams_investment_trade_fact WHERE source_table='pams_ibkr_trades'")
            cur.execute('DELETE FROM pams_ibkr_trades')
    finally:
        initial.close()
    report['xml'] = make_case(tmp_path, 2)['xml_text']
    assert client.post('/api/ibkr/flex/sync', json={}).json()['success']
    facts = [row for row in rows(database, 'pams_investment_trade_fact')
             if row['source_table'] == 'pams_ibkr_trades']
    assert len(facts) == 2
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            for row, other in zip(facts, facts[::-1]):
                raw = deepcopy(row['raw_json'])
                raw['source_row']['source_id'] = other['source_id']
                cur.execute('UPDATE pams_investment_trade_fact SET source_id=%s,raw_json=%s WHERE trade_id=%s',
                            ('fixture:' + str(row['trade_id']), Json(raw), row['trade_id']))
            for row, other in zip(facts, facts[::-1]):
                cur.execute('UPDATE pams_investment_trade_fact SET source_id=%s WHERE trade_id=%s',
                            (other['source_id'], row['trade_id']))
            cur.execute(sql.SQL('COMMENT ON DATABASE {} IS %s').format(sql.Identifier(conn.info.dbname)), (tool.MARKER,))
        raw_dir = tmp_path / 'raw/ibkr'
        result = tool.precheck(conn, raw_dir)
        assert not result['audit_schema_present']  # No runtime DDL, including on the precheck path.
        with conn, conn.cursor() as cur:
            cur.execute(MIGRATION.read_text(encoding='utf-8'))
        request = result['request']
        yield raw_dir, request, approve(request)
    finally:
        conn.close()


def invoke(database, batch):
    raw_dir, request, approval = batch
    conn = database()
    try:
        return tool.rehearse(conn, raw_dir, request, approval)
    finally:
        conn.close()


def test_precheck_is_server_readonly_deterministic_and_has_no_side_effects(batch, database, monkeypatch):
    raw_dir, request, _ = batch
    before = business_state(database)
    original = tool._request

    def checked(inputs, scope):
        assert inputs['quality']['status'] == 'ok'
        with active.cursor() as cur:
            cur.execute('SHOW transaction_read_only')
            assert cur.fetchone()[0] == 'on'
        return original(inputs, scope)

    monkeypatch.setattr(tool, '_request', checked)
    active = database()
    try:
        for _ in range(2):
            result = tool.precheck(active, raw_dir)
            assert result['request'] == request
            assert result['audit_schema_present'] and not result['executable']
            assert not result['production_write_supported']
            assert active.get_transaction_status() == TRANSACTION_STATUS_IDLE
    finally:
        active.close()
    assert business_state(database) == before and rows(database, AUDIT) == []


def test_real_cycle_changes_only_source_id_and_inserts_one_bound_journal(batch, database):
    before = business_state(database)
    first = invoke(database, batch)
    assert first['updated'] == 2 and first['status'] == 'applied_in_isolation'
    after = business_state(database)
    assert protected_facts_fingerprint(before['pams_investment_trade_fact']) == protected_facts_fingerprint(after['pams_investment_trade_fact'])
    for table in before.keys() - {'pams_investment_trade_fact'}:
        assert before[table] == after[table]
    journal = rows(database, AUDIT)
    assert len(journal) == 1 and journal[0]['rows_affected'] == 2
    assert journal[0]['request_json'] == batch[1] and journal[0]['approval_json'] == batch[2]
    assert journal[0]['database_operator'] == batch[1]['database_scope']['operator']
    assert invoke(database, batch)['updated'] == 0
    assert rows(database, AUDIT) == journal and business_state(database) == after


@pytest.mark.parametrize('change', ['missing', 'hash', 'purpose', 'approver', 'reference', 'expired',
    'future', 'naive', 'long_ttl', 'risks', 'extra', 'rehash_mapping', 'rehash_scope'])
def test_missing_invalid_or_reforged_approval_never_changes_facts(batch, database, change):
    raw_dir, request, approval = deepcopy(batch)
    if change == 'missing': approval = {}
    elif change == 'hash': approval['request_sha256'] = '0' * 64
    elif change == 'purpose': approval['purpose'] = 'production'
    elif change in ('approver', 'reference'): approval['approved_by' if change == 'approver' else 'reference'] = ' '
    elif change == 'expired': approval['expires_at'] = (tool.utc_now() - timedelta(seconds=1)).isoformat()
    elif change == 'future': approval['issued_at'] = (tool.utc_now() + timedelta(minutes=1)).isoformat()
    elif change == 'naive': approval['issued_at'] = '2026-10-22T00:00:00'
    elif change == 'long_ttl': approval['expires_at'] = (tool.utc_now() + timedelta(days=2)).isoformat()
    elif change == 'risks': approval['accepted_risks'] = []
    elif change == 'extra': approval['force'] = True
    else:
        if change == 'rehash_mapping': request['mappings'][0]['after'] = '999'
        else: request['database_scope']['database'] = 'pams'
        request['request_sha256'] = fingerprint([{k: v for k, v in request.items() if k != 'request_sha256'}])
        approval = approve(request)
    before = business_state(database)
    with pytest.raises(ValueError):
        invoke(database, (raw_dir, request, approval))
    assert business_state(database) == before and not rows(database, AUDIT)


@pytest.mark.parametrize('change', ['dsn', 'marker', 'busy', 'autocommit', 'missing_schema'])
def test_execution_guards_refuse_before_writes(batch, database, monkeypatch, change):
    conn = database()
    try:
        if change == 'dsn': monkeypatch.delenv('PAMS_TEST_POSTGRES_DSN')
        elif change == 'marker':
            with conn, conn.cursor() as cur:
                cur.execute(sql.SQL('COMMENT ON DATABASE {} IS NULL').format(sql.Identifier(conn.info.dbname)))
        elif change == 'missing_schema':
            with conn, conn.cursor() as cur: cur.execute(f'DROP TABLE {AUDIT}')
        elif change == 'busy':
            with conn.cursor() as cur: cur.execute('SELECT 1')
        else: conn.autocommit = True
        before = business_state(database)
        with pytest.raises((ValueError, psycopg2.Error)):
            tool.rehearse(conn, *batch)
        assert business_state(database) == before
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize('change', ['source', 'financial', 'partial', 'report', 'publication', 'outside'])
def test_drift_and_incomplete_scope_are_not_approved(batch, database, change):
    raw_dir, request, _ = batch
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            if change == 'source': cur.execute('UPDATE pams_ibkr_trades SET price=999')
            elif change == 'financial': cur.execute("UPDATE pams_investment_trade_fact SET quantity=999 WHERE source_table='pams_ibkr_trades'")
            elif change == 'partial':
                row = request['mappings'][0]
                cur.execute('UPDATE pams_investment_trade_fact SET source_id=%s WHERE trade_id=%s', ('partial', row['fact_id']))
            elif change == 'publication': cur.execute('UPDATE pams_data_sync_runs SET status=\'failed\'')
            elif change == 'outside': cur.execute("UPDATE pams_investment_trade_fact SET source_key='ibkr:outsider' WHERE trade_id=%s", (request['mappings'][0]['fact_id'],))
            else:
                # Changing the bytes invalidates the actual published SHA, not just a supplied plan.
                for path in raw_dir.glob('*.xml'):
                    path.write_text('<changed/>', encoding='utf-8')
        before = business_state(database)
        with pytest.raises(ValueError): invoke(database, batch)
        assert business_state(database) == before and not rows(database, AUDIT)
    finally:
        conn.close()


@pytest.mark.parametrize('fault', ['stage', 'first_final', 'protected_field', 'audit', 'deferred', 'commit'])
def test_real_failure_rolls_back_links_and_applied_journal(batch, database, monkeypatch, fault):
    conn = database(connection_factory=ObservedConnection)
    try:
        with conn, conn.cursor() as cur:
            if fault == 'protected_field':
                body = 'NEW.price=999; RETURN NEW;'
            elif fault == 'stage':
                body = "IF NEW.source_id LIKE 'rehearsal:%' THEN RAISE EXCEPTION 'stage fault'; END IF; RETURN NEW;"
            elif fault == 'first_final':
                body = "IF OLD.source_id LIKE 'rehearsal:%' THEN RAISE EXCEPTION 'final fault'; END IF; RETURN NEW;"
            else: body = "RAISE EXCEPTION 'deferred fault';"
            if fault in ('stage', 'first_final', 'protected_field', 'deferred'):
                cur.execute(f'CREATE FUNCTION rehearsal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} END $$')
                if fault == 'deferred':
                    cur.execute('CREATE CONSTRAINT TRIGGER rehearsal_fault AFTER UPDATE ON pams_investment_trade_fact DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rehearsal_fault()')
                else:
                    cur.execute('CREATE TRIGGER rehearsal_fault BEFORE UPDATE ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION rehearsal_fault()')
            elif fault == 'audit':
                cur.execute(f'ALTER TABLE {AUDIT} ADD CONSTRAINT injected_fault CHECK (rows_affected < 0)')
        if fault == 'commit': conn.fail_commit = True
        before = business_state(database)
        with pytest.raises((ValueError, RuntimeError, psycopg2.Error)):
            tool.rehearse(conn, *batch)
        assert business_state(database) == before and not rows(database, AUDIT)
    finally:
        conn.close()


@pytest.mark.parametrize('table', ['pams_ibkr_trades', 'pams_investment_trade_fact', 'pams_data_sync_runs', AUDIT])
def test_concurrent_writer_is_blocked_without_partial_batch(batch, database, table):
    blocker = database()
    try:
        with blocker.cursor() as cur:
            cur.execute(sql.SQL('LOCK TABLE {} IN ROW EXCLUSIVE MODE').format(sql.Identifier(table)))
        before = business_state(database)
        with pytest.raises(psycopg2.errors.LockNotAvailable): invoke(database, batch)
        assert business_state(database) == before and not rows(database, AUDIT)
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize('change', ['no_journal', 'audit_approval', 'audit_request', 'new_approval', 'link'])
def test_applied_batch_requires_matching_whole_state_and_original_audit(batch, database, change):
    invoke(database, batch)
    raw_dir, request, approval = deepcopy(batch)
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            if change == 'no_journal': cur.execute(f'DELETE FROM {AUDIT}')
            elif change == 'audit_approval': cur.execute(f"UPDATE {AUDIT} SET approval_sha256=%s", ('0' * 64,))
            elif change == 'audit_request':
                cur.execute(f"UPDATE {AUDIT} SET request_json=jsonb_set(request_json,'{{plan_sha256}}','\"bad\"')")
            elif change == 'link':
                cur.execute("UPDATE pams_investment_trade_fact SET source_id='changed' WHERE trade_id=%s", (request['mappings'][0]['fact_id'],))
            else: approval['reference'] = 'different attestation'
        before = business_state(database)
        journal = rows(database, AUDIT)
        with pytest.raises(ValueError): invoke(database, (raw_dir, request, approval))
        assert business_state(database) == before and rows(database, AUDIT) == journal
    finally:
        conn.close()


def test_cli_defaults_to_readonly_and_sanitizes_errors(batch, database, monkeypatch, capsys):
    raw_dir, request, _ = batch
    monkeypatch.setenv('PAMS_REPAIR_DSN', 'explicit-test-dsn')
    active = database()
    with monkeypatch.context() as patch:
        patch.setattr(tool.psycopg2, 'connect', lambda *args, **kwargs: active)
        assert tool.main(['--raw-dir', str(raw_dir)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['request'] == request and result['read_only'] and not rows(database, AUDIT)
    monkeypatch.delenv('PAMS_REPAIR_DSN')
    monkeypatch.setenv('PGPASSWORD', 'do-not-disclose')
    assert tool.main(['--raw-dir', str(raw_dir)]) == 1
    result = capsys.readouterr().out
    assert 'ValueError' in result and 'do-not-disclose' not in result


@pytest.mark.parametrize('args', [['--rehearse'], ['--request', 'x'], ['--approval', 'x'], ['--apply'], ['--force']])
def test_cli_has_no_implicit_approval_or_production_apply(args):
    with pytest.raises(SystemExit) as exc:
        tool.main(['--raw-dir', '.', *args])
    assert exc.value.code == 2


def test_migration_is_additive_and_idempotent(batch, database):
    before = business_state(database)
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            for _ in range(2): cur.execute(MIGRATION.read_text(encoding='utf-8'))
        assert business_state(database) == before and not rows(database, AUDIT)
    finally:
        conn.close()


def test_tool_is_outside_api_and_not_wired_to_scheduler_or_endpoint():
    assert 'COPY scripts' not in (ROOT / 'backend/api/Dockerfile').read_text()
    for folder in ('backend/api/endpoints', 'backend/scheduler'):
        assert all('ibkr_source_link_repair' not in path.read_text(encoding='utf-8')
                   for path in (ROOT / folder).glob('*.py'))


def test_normal_database_refused_before_any_sql(monkeypatch):
    monkeypatch.setenv('PAMS_TEST_POSTGRES_DSN', 'host=localhost user=pams port=52543')

    class NormalDatabase:
        autocommit = False

        class info:
            dsn_parameters = {'host': 'localhost', 'port': '52543', 'user': 'pams', 'dbname': 'pams'}

        def get_transaction_status(self): return TRANSACTION_STATUS_IDLE
        def cursor(self): raise AssertionError('Normal database must never reach SQL execution')

    with pytest.raises(ValueError, match='disposable'):
        tool._isolated(NormalDatabase())


def test_approval_expiring_during_verification_rolls_back(batch, database, monkeypatch):
    before = business_state(database)
    now = tool.utc_now()
    times = iter([now, now + timedelta(hours=2)])
    monkeypatch.setattr(tool, 'utc_now', lambda: next(times))
    with pytest.raises(ValueError, match='expired'): invoke(database, batch)
    assert business_state(database) == before and not rows(database, AUDIT)


def test_lost_commit_acknowledgement_is_recovered_without_duplicate_write(batch, database):
    class LostAckConnection(ObservedConnection):
        def commit(self):
            super().commit()
            raise RuntimeError('Injected lost acknowledgement AFTER successful commit')

    conn = database(connection_factory=LostAckConnection)
    try:
        with pytest.raises(RuntimeError, match='AFTER successful commit'):
            tool.rehearse(conn, *batch)
    finally:
        conn.close()
    assert len(rows(database, AUDIT)) == 1
    committed = business_state(database)
    assert invoke(database, batch)['updated'] == 0
    assert business_state(database) == committed and len(rows(database, AUDIT)) == 1
