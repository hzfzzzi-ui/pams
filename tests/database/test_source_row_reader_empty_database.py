"""New-source reads do not require creating a legacy source table."""
from datetime import date
from hashlib import sha256
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.imports.source_row_read import source_row_relation
from domain.ledger.ledger_view import _load_cash_rows
from endpoints.accounts import _fetch_statement_balances
from tests.support.account_import_support import scratch_database


def test_new_source_and_published_facts_work_without_legacy_source_table(monkeypatch):
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    with scratch_database() as target:
        with psycopg2.connect(**target) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            for filename in ('074_unified_source.sql', '075_source_fact_links.sql'):
                cur.execute((ROOT / 'database/postgres/schema' / filename).read_text(encoding='utf-8'))
            cur.execute("ALTER TABLE pams_accounts ADD COLUMN account_type text, ADD COLUMN is_asset boolean DEFAULT true")
            cur.execute("ALTER TABLE pams_cashflow_fact ADD COLUMN source_channel text")
            cur.execute("SELECT to_regclass('public.pams_source_row') AS table_name")
            assert cur.fetchone()['table_name'] is None

            legacy = {'source_system': 'legacy-bank-verified', 'source_table': 'pams_bank_statement_rows',
                      'source_id': 'saved-bank', 'source_uid': 'uid-saved-bank', 'account_code': 'bank_test',
                      'event_date': '2026-01-02', 'imported_at': '2025-12-31T10:00:00+00:00',
                      'created_at': '2025-12-31T11:00:00+00:00'}
            malformed = {**legacy, 'source_id': 'invalid-bank', 'source_uid': 'uid-invalid-bank',
                         'imported_at': 'not-a-timestamp', 'created_at': None}
            for source_id, raw, source_table, status in (
                ('new-bank', {}, 'pams_bank_statement_rows', 'published'),
                ('old-bank', {'id': '42'}, 'pams_bank_statement_rows', 'published'),
                ('saved-bank', {'id': 'saved-bank', 'legacy_source_row': legacy}, 'pams_bank_statement_rows', 'published'),
                ('invalid-bank', {'id': 'invalid-bank', 'legacy_source_row': malformed}, 'pams_bank_statement_rows', 'published'),
                ('native-cash', {}, 'pams_account_import_batch', 'published'),
                ('draft-bank', {}, 'pams_bank_statement_rows', 'draft'),
            ):
                batch_id, item_id = str(uuid4()), str(uuid4())
                cur.execute("""INSERT INTO source_batches
                    (batch_id,account_code,business_type,institution,acquisition_method)
                    VALUES (%s,'bank_test','cash','test','manual')""", (batch_id,))
                cur.execute("""INSERT INTO source_items
                    (source_item_id,batch_id,source_uid,parse_method,record_hash,review_status,raw_fields,created_at)
                    VALUES (%s,%s,%s,'manual',%s,'confirmed',%s,'2026-01-03T12:00:00Z')""",
                    (item_id, batch_id, 'uid-' + source_id, 'a' * 64, Json(raw)))
                cur.execute("""INSERT INTO pams_cashflow_fact
                    (event_date,account_code,amount,currency,amount_cny,cashflow_type,
                     source_table,source_id,source_uid,source_hash,source_item_id)
                    VALUES ('2026-01-02','bank_test',-10,'CNY',-10,'operating_expense',%s,%s,%s,%s,%s)""",
                    (source_table, source_id, 'uid-' + source_id, sha256(source_id.encode()).hexdigest(), item_id))
                if status == 'published':
                    cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                                ('f' * 64, batch_id))

            relation = source_row_relation(cur)
            cur.execute(f"""SELECT source_system,source_table,source_id,event_date,imported_at,created_at
                FROM {relation} AS s ORDER BY source_id""")
            rows = [dict(row) for row in cur.fetchall()]
            indexed = {row['source_id']: row for row in rows}
            assert set(indexed) == {'native-cash', 'new-bank', 'old-bank', 'saved-bank', 'invalid-bank'}
            assert indexed['new-bank']['event_date'] == date(2026, 1, 2) and indexed['new-bank']['imported_at'] is not None
            assert indexed['old-bank']['imported_at'] is None and indexed['old-bank']['created_at'] is None
            assert indexed['invalid-bank']['imported_at'] is None and indexed['invalid-bank']['created_at'] is None
            assert indexed['saved-bank']['imported_at'].isoformat() == '2025-12-31T10:00:00+00:00'
            assert indexed['saved-bank']['source_system'] == 'legacy-bank-verified'
            cur.execute(f"""SELECT max(event_date) AS as_of,max(created_at) AS updated_at,count(*) AS rows
                FROM {relation} AS s WHERE source_table='pams_bank_statement_rows'""")
            assert cur.fetchone()['rows'] == 4
            assert sum(row['row_count'] for row in _fetch_statement_balances(cur)['bank_test']) == 4
            facts = _load_cash_rows(cur, start=date(2026,1,1), end=date(2026,1,31), base_currency='CNY')
            assert len(facts) == 6
            published = next(row for row in facts if row['source_id'] == 'new-bank')
            assert published['amount'] == -10 and published['imported_at'] == indexed['new-bank']['imported_at']
