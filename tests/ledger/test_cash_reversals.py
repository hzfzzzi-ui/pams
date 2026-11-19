from datetime import date
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_transaction_fx_evidence import _publish_synthetic_cashflow
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_reversal_is_idempotent_and_preserves_period_cash(empty_source_db):
    from domain.ledger import cash_reversals
    from domain.ledger.ledger_view import build_ledger_workbench_payload
    from domain.ledger.manual_transfer_pairs import pending_rows

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank')")
        for day, amount in [('2026-01-31', '-100'), ('2026-02-01', '100')]:
            _publish_synthetic_cashflow(conn, account='test_bank', fields={
                'business_date': day, 'net_cash': amount, 'currency': 'CNY',
            }, classification='internal_transfer')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact ORDER BY event_date')
            debit, credit = [row['cashflow_id'] for row in cur.fetchall()]
            first = cash_reversals.confirm(cur, debit_id=debit, credit_id=credit, note='User confirmed', confirmed=True)
            assert cash_reversals.confirm(cur, debit_id=debit, credit_id=credit, note='User confirmed', confirmed=True)['replayed']
            assert first['replayed'] is False
            assert cash_reversals.confirmed_cashflow_ids(cur) == {debit, credit}
            jan = build_ledger_workbench_payload(cur, month='2026-01', view='overview')['periods']['month']
            feb = build_ledger_workbench_payload(cur, month='2026-02', view='overview')['periods']['month']
            assert jan['income'] == jan['expense'] == 0 and jan['cash_net'] == -100
            assert feb['income'] == feb['expense'] == 0 and feb['cash_net'] == 100
            assert not pending_rows(cur, start=date(2026, 1, 1), end=date(2026, 2, 28))
            cur.execute('SELECT count(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 2
            assert not cash_reversals.revoke(cur, confirmation_id=first['confirmation_id'], note='Undo', confirmed=True)['replayed']
            assert cash_reversals.revoke(cur, confirmation_id=first['confirmation_id'], note='Undo', confirmed=True)['replayed']
            assert cash_reversals.confirmed_cashflow_ids(cur) == set()
            assert len(pending_rows(cur, start=date(2026, 1, 1), end=date(2026, 2, 28))) == 2


def test_reversal_validation_rejects_nonmatching_facts():
    from domain.ledger.cash_reversals import _valid
    left = {'kind': 'cashflow', 'id': 1, 'account_code': 'a', 'currency': 'CNY',
            'source_item_id': 'a', 'day': '2026-01-01', 'amount': Decimal('-100')}
    right = {**left, 'id': 2, 'source_item_id': 'b', 'day': '2026-01-02', 'amount': Decimal('100')}
    assert _valid(left, right)
    for changes in ({'amount': '99'}, {'currency': 'USD'}, {'account_code': 'b'},
                    {'day': '2025-12-31'}, {'id': 1}, {'source_item_id': 'a'}):
        assert not _valid(left, {**right, **changes})


def test_existing_database_upgrade_accepts_valid_reversal(empty_source_db):
    from pathlib import Path
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE evidence_confirmation DROP CONSTRAINT evidence_confirmation_confirmation_type_check')
            cur.execute("ALTER TABLE evidence_confirmation ADD CONSTRAINT evidence_confirmation_confirmation_type_check CHECK (confirmation_type IN ('transfer','bank_source','payment_proxy','account_review'))")
            cur.execute(Path('database/postgres/upgrades/087_cash_reversal.sql').read_text(encoding='utf-8'))
            cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='public.evidence_confirmation'::regclass AND conname='evidence_confirmation_confirmation_type_check'")
            assert 'cash_reversal' in cur.fetchone()[0]


def test_reversal_is_available_in_manual_matching(empty_source_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import bank_reconcile, ledger

    def connect():
        return psycopg2.connect(**empty_source_db)

    with connect() as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Bank')")
        for amount, summary in [('-100', '基金申购 123456'), ('100', '基金申购撤单 123456')]:
            _publish_synthetic_cashflow(conn, account='test_bank', fields={
                'business_date': '2026-01-22', 'net_cash': amount, 'currency': 'CNY', 'summary': summary,
            }, classification='internal_transfer')
        with conn.cursor() as cur:
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact ORDER BY amount')
            debit, credit = [row[0] for row in cur.fetchall()]
    monkeypatch.setattr(bank_reconcile, 'get_conn', connect)
    monkeypatch.setattr(ledger, 'get_conn', connect)
    app = FastAPI(); app.include_router(bank_reconcile.router, prefix='/api'); app.include_router(ledger.router, prefix='/api')
    with TestClient(app) as client:
        options = client.get(f'/api/bank-reconcile/transfers/manual-pending/{debit}/options').raise_for_status().json()
        assert [(row['id'], row['match_type']) for row in options['rows']] == [(credit, 'cash_reversal')]
        result = client.post('/api/bank-reconcile/cash-reversals/confirm', json={
            'debit_id': debit, 'credit_id': credit, 'note': 'User confirmed cancellation', 'confirmed': True,
        }).raise_for_status().json()
        assert not result['replayed']
        result = client.get('/api/ledger/workbench?view=transfers&month=2026-01&manualPending=true').raise_for_status().json()
        assert result['manual_pending'] == []
