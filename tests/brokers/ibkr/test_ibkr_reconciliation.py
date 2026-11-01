"""Reconciliation remains server-enforced read only on the current source schema."""
from pathlib import Path
import sys
import psycopg2.extras
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend/api'))
from domain.brokers.ibkr.ibkr_reconciliation import read_ibkr_reconciliation
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state

def test_real_postgres_is_readonly_and_does_not_mutate_any_business_or_audit_rows(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_reconciliation as audit
    statements = []
    class GuardCursor(psycopg2.extras.RealDictCursor):
        def execute(self, query, args=None):
            assert self.connection.readonly is True
            assert self.connection.isolation_level == ISOLATION_LEVEL_REPEATABLE_READ
            statements.append(str(query))
            assert str(query).lstrip().upper().startswith(('SELECT','SET LOCAL','SHOW','WITH'))
            return super().execute(query,args)
    class GuardConnection(connection):
        def cursor(self, *args, **kwargs):
            kwargs['cursor_factory'] = GuardCursor
            return super().cursor(*args,**kwargs)
    monkeypatch.setattr(audit, 'get_conn', lambda: database(connection_factory=GuardConnection))
    client, _, _ = sync_case
    before = business_state(database)
    files = sorted(str(p) for p in tmp_path.rglob('*.xml'))
    for _ in range(2):
        response = client.get('/api/ibkr/reconciliation')
        assert response.status_code == 200
        result = response.json()
        assert result['status'] == 'review' and result['read_only'], (result, statements[-1:])
        assert result['scope']['source_authentication'] == 'unknown'
    assert statements and not any('CREATE ' in sql or 'UPDATE ' in sql for sql in statements)
    after = business_state(database)
    assert before == after and files == sorted(str(p) for p in tmp_path.rglob('*.xml'))


def test_server_rejects_accidental_write_and_diagnostics_do_not_hide_it(sync_case, database, monkeypatch):
    from domain.brokers.ibkr import ibkr_reconciliation as audit
    before = business_state(database)
    def write_by_mistake(conn, raw_dir):
        with conn.cursor() as cur:
            cur.execute('UPDATE pams_holdings_snapshot SET quantity=0')
        pytest.fail('PostgreSQL must reject the write')
    monkeypatch.setattr(audit,'select_report',write_by_mistake)
    result = read_ibkr_reconciliation(connection_factory=database)
    assert result['checks'][0]['issues'][0]['error_type'] == 'ReadOnlySqlTransaction'
    assert business_state(database) == before


def test_missing_table_is_not_created_at_runtime(sync_case, database):
    conn = database()
    with conn, conn.cursor() as cur:
        cur.execute('DROP TABLE pams_investment_trade_fact CASCADE')
    conn.close()
    result = read_ibkr_reconciliation(connection_factory=database)
    assert result['status'] == 'review', result
    assert result['checks'][0]['issues'][0]['reason'] == 'missing_table'
    conn = database()
    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.pams_investment_trade_fact')")
        assert cur.fetchone()[0] is None
    conn.close()




def test_unverified_legacy_report_is_not_read_or_accepted(sync_case, database, monkeypatch, tmp_path):
    from datetime import date
    from domain.brokers.ibkr import ibkr_reconciliation as audit
    from domain.brokers.ibkr.ibkr_report_state import PublishedReport
    report = PublishedReport(tmp_path / 'absent.xml', date(2026, 10, 22), 7, '0' * 64, None, legacy=True)
    monkeypatch.setattr(audit, 'select_report', lambda conn, raw_dir: (report, None))
    before = business_state(database)
    result = read_ibkr_reconciliation(connection_factory=database)
    assert result['status'] == 'review'
    assert result['scope']['source_status'] == 'unknown'
    assert result['checks'][0]['issues'][0]['reason'] == 'source_unverified'
    assert business_state(database) == before
