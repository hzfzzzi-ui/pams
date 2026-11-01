"""Investment transaction reads use unified facts even if obsolete tables exist."""
# ruff: noqa: F811
import psycopg2
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_empty_fact_table_never_reads_old_broker_table(empty_source_db,monkeypatch):
    import main
    from endpoints import investment_transactions
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE pams_ibkr_trades (obsolete boolean)')
            cur.execute('INSERT INTO pams_ibkr_trades VALUES (true)')
    monkeypatch.setattr(investment_transactions,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    with TestClient(main.app) as web:
        response=web.get('/api/investment-transactions')
        assert response.status_code==200,response.text
        assert response.json()['rows']==[] and response.json()['summary']['data_source']=='trade_fact'
