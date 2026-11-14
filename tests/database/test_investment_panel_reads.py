"""Investment panel reads share current inputs without dropping evidence checks."""
from hashlib import sha256
from uuid import uuid4

import psycopg2
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_hash_only_verification_is_scoped_to_the_actual_original(empty_source_db):
    from domain.imports.source_original_read import SourceOriginals

    content = b'synthetic source'
    digest = sha256(content).hexdigest()
    good, bad = uuid4(), uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for ident, value in ((good, content), (bad, b'different bytes')):
                cur.execute("""INSERT INTO source_records
                    (source_id,account_code,source_name,acquisition_method,document_type,content,sha256)
                    VALUES (%s,'ibkr','fixture','api','cash',%s,%s)""", (str(ident), value, digest))
            cur.execute('INSERT INTO ibkr_source_originals(sha256,content) VALUES (%s,%s)', (digest, content))
            inline = {'source_account':'ibkr', 'sha256':digest, 'original_source_id':good, 'inline_original':True}
            rows = [inline, {**inline, 'original_source_id':bad}, {**inline, 'source_account':'tonghuashun'},
                    {**inline, 'inline_original':False}, {**inline, 'sha256':None, 'inline_original':False},
                    {**inline, 'sha256':None}]
            checks = SourceOriginals(cur)
            checks.verify_many(rows)
            assert not checks._cache  # The hash-only path did not store any original bytes.
            assert [checks.verify(row) for row in rows] == [True, False, False, True, True, False]
            assert [checks.read(row)[1] for row in rows] == [True, False, False, True, True, False]


def test_risk_and_config_use_one_current_read_and_no_history(empty_source_db, monkeypatch):
    import main
    from domain.investments import portfolio_state
    from domain.risk import portfolio_panels, risk_state

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(portfolio_panels, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(risk_state, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(risk_state, '_latest_ibkr_performance', lambda: None)
    loads = []
    loader = portfolio_panels.load_current_portfolio_inputs

    def counted(cur, **kwargs):
        loads.append(kwargs['base_currency'])
        assert cur.connection.readonly
        return loader(cur, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError('current risk and allocation should not read historical facts')

    monkeypatch.setattr(portfolio_panels, 'load_current_portfolio_inputs', counted)
    for name in ('load_cash_transactions', 'load_investment_transactions', 'load_transfer_matches', 'load_valuation_series'):
        monkeypatch.setattr(portfolio_state, name, forbidden)
    with TestClient(main.app) as client:
        grouped = client.get('/api/portfolio/risk-and-config')
        assert grouped.status_code == 200
        assert loads == ['CNY']
        assert grouped.json()['risk'] == client.get('/api/portfolio/risk-disclosure').json()
        assert grouped.json()['config'] == client.get('/api/portfolio/config').json()


def test_accounts_view_uses_one_balance_verification(empty_source_db, monkeypatch):
    import main
    from endpoints import accounts

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(accounts, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    reads = []
    overview = accounts._overview_payload

    def counted(*, cur, **kwargs):
        reads.append(cur)
        assert cur.connection.readonly
        return overview(cur=cur, **kwargs)

    monkeypatch.setattr(accounts, '_overview_payload', counted)
    with TestClient(main.app) as client:
        combined = client.get('/api/investment/accounts-view')
        assert combined.status_code == 200
        assert len(reads) == 1
        payload = combined.json()
        assert 'accounts' in payload['overview']
        standalone = client.get('/api/account-performance/tonghuashun')
        assert standalone.status_code == 404
        assert 'tonghuashun' in payload
