# ruff: noqa: F811
from datetime import date
from hashlib import sha256
import json
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.ledger.ledger_view import _load_pending_transfer_cases


def test_new_source_candidates_require_consistent_evidence(empty_source_db):
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            ids = []
            for index, amount in enumerate(('-10', '10')):
                account = f'test_transfer_{index}'
                source, item = uuid4(), uuid4()
                raw = {'row': index}
                digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
                fields = {'business_date': day.isoformat(), 'net_cash': amount, 'currency': 'CNY'}
                cur.execute('INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,%s)', (account, account))
                cur.execute("""INSERT INTO source_records(source_id,account_code,source_name,acquisition_method,document_type)
                    VALUES (%s,%s,'synthetic','manual','cash')""", (str(source), account))
                cur.execute("""INSERT INTO source_items(source_item_id,source_id,parse_method,raw_fields,parsed_fields,
                    record_hash,review_status) VALUES (%s,%s,'manual',%s,%s,%s,'confirmed')""",
                    (str(item), str(source), Json(raw), Json(fields), digest))
                cur.execute("""INSERT INTO pams_cashflow_fact(account_code,event_date,amount,currency,source_item_id,fact_role)
                    VALUES (%s,%s,%s,'CNY',%s,'cash') RETURNING cashflow_id""", (account, day, amount, str(item)))
                ident = cur.fetchone()[0]
                ids.append(ident)
                cur.execute("""INSERT INTO cashflow_classification(cashflow_id,cashflow_type,status,classification_source)
                    VALUES (%s,'internal_transfer','confirmed','manual')""", (ident,))
                cur.execute("UPDATE source_records SET status='published' WHERE source_id=%s", (str(source),))
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cases = _load_pending_transfer_cases(cur, start=day, end=day)
            by_id = {case['case_id']: case for case in cases}
            assert by_id[f'cashflow:{ids[0]}']['status'] == 'unique_candidate'
            assert by_id[f'cashflow:{ids[0]}']['candidate_ids'] == [ids[1]]
            cur.execute('UPDATE pams_cashflow_fact SET amount=11 WHERE cashflow_id=%s', (ids[1],))
            cases = _load_pending_transfer_cases(cur, start=day, end=day)
            by_id = {case['case_id']: case for case in cases}
            assert by_id[f'cashflow:{ids[0]}']['status'] == 'unmatched'
            assert by_id[f'cashflow:{ids[1]}']['status'] == 'source_unknown'
            assert by_id[f'cashflow:{ids[1]}']['amount'] is None
