# ruff: noqa: F811
"""Manual source facts remain readable without the old transaction table."""
# ruff: noqa: F401
import psycopg2
from fastapi.testclient import TestClient

from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db
from tests.imports.test_account_import_publish import draft, preview, publish, trade


def test_new_database_manual_trade_publish_replay_and_read(empty_source_db, monkeypatch):
    import main
    from domain.imports import account_import_store as store
    from endpoints import investment_transactions

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_investment_transactions')")
            assert cur.fetchone()[0] is None
            cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency)
                           VALUES ('TEST_STOCK','000001','Synthetic','stock','CNY')""")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    for module in (main, store, investment_transactions):
        monkeypatch.setattr(module, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(investment_transactions, '_load_accounts_config', lambda: {})
    with TestClient(main.app, base_url='http://localhost') as web:
        web.headers['Origin'] = 'http://localhost'
        batch = draft(web, [trade()])
        check = preview(web, batch)
        assert check['can_publish'], check['rows']
        result = publish(web, batch, check)
        assert result.status_code == 200, result.text
        replay = publish(web, batch, check)
        assert replay.status_code == 200 and replay.json()['replayed']
        response = web.get('/api/investment-transactions?account_code=tonghuashun')
        assert response.status_code == 200, response.text
        assert any(row['symbol'] == '000001' and float(row['quantity']) == 100
                   for row in response.json()['rows'])
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(source_item_id) FROM pams_investment_trade_fact")
        assert cur.fetchone() == (1, 1)
        cur.execute("SELECT to_regclass('public.pams_investment_transactions')")
        assert cur.fetchone()[0] is None
