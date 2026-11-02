from pathlib import Path
import psycopg2
import pytest
from tests.support.account_import_support import scratch_database

SQL = Path(__file__).resolve().parents[2] / 'database/postgres/schema/079_source_records.sql'


def test_source_records_require_original_content_and_hash_for_file():
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute(SQL.read_text(encoding='utf-8-sig'))
        cur.execute("""INSERT INTO source_records(source_id,account_code,source_name,acquisition_method,document_type)
                    VALUES ('00000000-0000-0000-0000-000000000001','bank_test','Synthetic bank','manual','cash')""")
        cur.execute("SAVEPOINT invalid_source")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("""INSERT INTO source_records(source_id,account_code,source_name,acquisition_method,
                         document_type,filename,file_format,content) VALUES
                         ('00000000-0000-0000-0000-000000000002','bank_test','Synthetic bank','file',
                          'cash','sample.csv','csv',decode('01','hex'))""")

        cur.execute("ROLLBACK TO SAVEPOINT invalid_source")
        cur.execute("SELECT count(*) FROM source_records")
        assert cur.fetchone()[0] == 1


def test_source_records_validate_account_and_period():
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute(SQL.read_text(encoding="utf-8-sig"))
        cases = [
            ("missing-account", "2026-01-01", "2026-01-31", psycopg2.errors.ForeignKeyViolation),
            ("bank_test", "2026-02-01", "2026-01-01", psycopg2.errors.CheckViolation),
        ]
        for account, start, end, error in cases:
            cur.execute("SAVEPOINT bad")
            with pytest.raises(error):
                cur.execute("""INSERT INTO source_records(source_id,account_code,source_name,acquisition_method,
                    document_type,period_start,period_end) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    ("00000000-0000-0000-0000-000000000003", account, "Synthetic bank", "manual", "cash", start, end))
            cur.execute("ROLLBACK TO SAVEPOINT bad")
