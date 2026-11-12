# ruff: noqa: F811
from datetime import date
from decimal import Decimal
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


def test_manual_pending_matches_an_unclassified_opposite_leg_across_months(empty_source_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import bank_reconcile, ledger
    from tests.database.test_transaction_fx_evidence import _publish_synthetic_cashflow

    def connect():
        return psycopg2.connect(**empty_source_db)

    with connect() as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_accounts(account_code,display_name)
                           VALUES ('synthetic_mxn','Synthetic MXN'),('synthetic_cny','Synthetic CNY'),
                                  ('synthetic_other','Synthetic other')""")
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                           VALUES ('2026-10-31','CNY','MXN',0.35,'synthetic')""")
        for account, day, amount, currency, kind in (
            ('synthetic_mxn', '2026-10-31', '-1000', 'MXN', 'operating_expense'),
            ('synthetic_cny', '2026-11-01', '365', 'CNY', 'fx_conversion'),
            ('synthetic_other', '2026-10-31', '-360', 'CNY', 'operating_expense'),
        ):
            _publish_synthetic_cashflow(conn, account=account, fields={
                'business_date': day, 'net_cash': amount, 'currency': currency,
            }, classification=kind)
        with conn.cursor() as cur:
            cur.execute('SELECT account_code,cashflow_id FROM pams_cashflow_fact')
            ids = dict(cur.fetchall())
            cur.execute('DELETE FROM cashflow_classification WHERE cashflow_id=%s', (ids['synthetic_mxn'],))

    monkeypatch.setattr(bank_reconcile, 'get_conn', connect)
    monkeypatch.setattr(ledger, 'get_conn', connect)
    app = FastAPI()
    app.include_router(bank_reconcile.router, prefix='/api')
    app.include_router(ledger.router, prefix='/api')
    anchor, source = ids['synthetic_cny'], ids['synthetic_mxn']
    pending_url = '/api/ledger/workbench?view=transfers&month=2026-11&manualPending=true'
    overview_url = '/api/ledger/workbench?view=overview&month=2026-11'
    confirm_url = '/api/bank-reconcile/transfers/confirm'
    options_url = f'/api/bank-reconcile/transfers/manual-pending/{anchor}/options'
    payload = dict(from_kind='cashflow', from_id=source, to_kind='cashflow', to_id=anchor,
                   note='Synthetic receipts reviewed', confirmed=True)
    with TestClient(app) as client:
        overview = client.get(overview_url).raise_for_status().json()
        assert [row['id'] for row in client.get(pending_url).raise_for_status().json()['manual_pending']] == [anchor]
        assert client.get(options_url + '?month=2026-11').raise_for_status().json()['rows'] == []
        options = client.get(options_url).raise_for_status().json()['rows']
        assert [(row['id'], row['match_type']) for row in options] == [(source, 'fx_conversion')]
        assert client.post(confirm_url, json=payload).status_code == 409
        assert client.post(confirm_url, json={**payload, 'from_id': ids['synthetic_other'],
                                             'manual_anchor_id': anchor}).status_code == 409
        manual_payload = {**payload, 'manual_anchor_id': anchor}
        saved = client.post(confirm_url, json=manual_payload).raise_for_status().json()
        assert client.post(confirm_url, json=manual_payload).raise_for_status().json()['replayed']
        after = client.get(overview_url).raise_for_status().json()
        assert overview['periods']['ytd']['income'] - after['periods']['ytd']['income'] == 365
        assert overview['periods']['ytd']['expense'] - after['periods']['ytd']['expense'] == 350
        assert after['periods']['month']['income'] == 0
        before_months = {p['period']: p for p in overview['monthly_trend']}
        after_months = {p['period']: p for p in after['monthly_trend']}
        assert before_months['2026-10']['expense'] - after_months['2026-10']['expense'] == 350
        assert before_months['2026-11']['income'] == 365
        assert after_months.get('2026-11', {}).get('income', 0) == 0
        transfers = client.get(pending_url).raise_for_status().json()
        assert transfers['manual_pending'] == []
        pair, = transfers['transfer_pairs']
        assert pair['id'] == saved['transfer_id'] and pair['match_type'] == 'fx_conversion'
        assert (pair['currency_norm'], pair['target_currency'], pair['source_date'], pair['target_date']) == (
            'MXN', 'CNY', '2026-10-31', '2026-11-01',
        )
        assert Decimal(str(pair['amount_cny'])) == Decimal('350')
        assert client.get(options_url).status_code == 409
