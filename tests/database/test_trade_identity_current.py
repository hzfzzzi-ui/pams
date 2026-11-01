"""Current identity review stays with its fact, without a process-history table."""
# ruff: noqa: F811
import sys
from pathlib import Path

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))

from endpoints import investment_transactions as trades
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_identity_review_keeps_latest_decision_and_existing_note(empty_source_db, monkeypatch):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('identity_test','Synthetic')")
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,currency,trade_type,side,source_table,source_id,source_hash,
                 initial_position_identity,current_position_identity,review_note)
                VALUES ('2026-01-02','identity_test','CNY','buy','buy','synthetic','one','source-hash',
                        'trend','trend','existing manual note') RETURNING trade_id""")
            trade_id = cur.fetchone()[0]
            cur.execute("SELECT to_regclass('public.pams_trade_identity_transition_event')")
            assert cur.fetchone()[0] is None

    monkeypatch.setattr(trades, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(trades, '_load_accounts_config', lambda: {})
    app = FastAPI()
    app.include_router(trades.router, prefix='/api')
    with TestClient(app) as web:
        result = web.post(f'/api/investment-transactions/{trade_id}/review', json={
            'current_position_identity': 'value',
            'identity_transition_type': 'research_reclassify',
            'identity_transition_triggers': ['research_added'],
            'identity_transition_evidence': 'synthetic research note',
        })
        assert result.status_code == 200, result.text
        row = result.json()['trade']
        assert row['initial_position_identity'] == 'trend'
        assert row['current_position_identity'] == 'value'
        assert row['review_note'] == 'existing manual note'
        assert row['identity_transition_type'] == 'research_reclassify'
        assert row['identity_transition_triggers'] == ['research_added']
        assert row['identity_transition_evidence'] == 'synthetic research note'
        assert row['identity_transition_quality'] == 'needs_review'
        assert row['identity_transition_at']
        # A later unrelated review must not erase the current transition or note.
        result = web.post(f'/api/investment-transactions/{trade_id}/review', json={'sell_condition': 'review'})
        assert result.status_code == 200, result.text
        assert result.json()['trade']['identity_transition_evidence'] == 'synthetic research note'
        assert result.json()['trade']['review_note'] == 'existing manual note'
        assert result.json()['trade']['review_status'] == 'reviewed'
        listed = web.get('/api/investment-transactions')
        assert listed.status_code == 200, listed.text
        assert listed.json()['rows'][0]['identity_transition_evidence'] == 'synthetic research note'

    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT source_table,source_id,source_hash,initial_position_identity,current_position_identity,"
                    "identity_transition_evidence,review_note FROM pams_investment_trade_fact WHERE trade_id=%s", (trade_id,))
        row = cur.fetchone()
        assert (row['source_table'], row['source_id'], row['source_hash']) == ('synthetic','one','source-hash')
        assert row['identity_transition_evidence'] == 'synthetic research note'
        assert row['review_note'] == 'existing manual note'
