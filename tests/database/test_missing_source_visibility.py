"""Missing source schema never fabricates an empty verified bank history."""
# ruff: noqa: F811
import psycopg2
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from domain.imports.source_row_read import source_row_relation
from endpoints.accounts import _fetch_statement_balances


def test_missing_source_schema_is_explicitly_unknown(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            relation=source_row_relation(cur)
            cur.execute(f"""SELECT COUNT(*) FILTER (WHERE account_code IS NOT NULL) AS real_rows,
                COUNT(*) FILTER (WHERE is_bank_statement AND source_evidence_status='unknown') AS unavailable
                FROM {relation} AS s""")
            assert cur.fetchone()=={'real_rows':0,'unavailable':1}
            balances=_fetch_statement_balances(cur)
            assert balances == {} and balances.source_status == 'unavailable'


def test_partial_source_schema_cannot_look_ready(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('CREATE TABLE source_items(id int); CREATE TABLE source_batches(id int); CREATE TABLE pams_cashflow_fact(id int)')
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            balances=_fetch_statement_balances(cur)
            assert balances == {} and balances.source_status == 'unavailable'
