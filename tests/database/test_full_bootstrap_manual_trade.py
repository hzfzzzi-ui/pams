# ruff: noqa: F811
"""Manual source facts remain readable without the old transaction table."""
# ruff: noqa: F401
import psycopg2
from fastapi.testclient import TestClient

from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db
from tests.imports.test_account_import_publish import trade


def test_ths_snapshot_reuses_published_trade_source(empty_source_db, monkeypatch):
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID
    from zoneinfo import ZoneInfo
    from psycopg2.extras import RealDictCursor
    import main
    from domain.imports import account_import_store as store
    from domain.investments import trade_snapshot_http
    from domain.investments.trade_snapshot_display import enrich_trade_snapshot_costs
    from scripts.link_ths_trade_assets import link_assets
    from tests.imports.test_account_import_ths import event, export

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    day = date(2025, 10, 22)
    quote = {'symbol': 'sz003123', 'price': Decimal('12'), 'price_date': day, 'source': 'tencent',
             'price_time': datetime(2025, 10, 22, 14, tzinfo=ZoneInfo('Asia/Shanghai'))}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz003123': quote})
    root = '/api/account-import'
    with TestClient(main.app, base_url='http://localhost') as web:
        web.headers['Origin'] = 'http://localhost'
        uploaded = web.post(root + '/source-records?account_code=tonghuashun&source_name=tonghuashun&filename=table.xls',
                            content=export(event(**{'证券代码': '003123', '交易市场': '深圳Ａ股'})))
        assert uploaded.status_code == 200, uploaded.text
        source = uploaded.json()
        source_path = root + '/source-records/' + source['source_id']
        revision = {'revision': source['revision']}
        checked = web.post(source_path + '/trade-preview', json=revision)
        assert checked.status_code == 200, checked.text
        saved = web.post(source_path + '/trade-publish', json={**revision, 'preview_token': checked.json()['preview_token']})
        assert saved.status_code == 200, saved.text

        with psycopg2.connect(**empty_source_db) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT raw_fields,parsed_fields,record_hash FROM source_items')
                before = cur.fetchall()
                source_id = UUID(source['source_id'])
                assert link_assets(cur, source_id, expected_count=1, apply=False)['new_links'] == 1
                assert link_assets(cur, source_id, expected_count=1, apply=True)['new_links'] == 1
                assert link_assets(cur, source_id, expected_count=1, apply=True)['new_links'] == 0
                cur.execute('SELECT raw_fields,parsed_fields,record_hash FROM source_items')
                assert cur.fetchall() == before

        checked = web.post(root + '/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
        assert checked.status_code == 200, checked.text
        value = checked.json()
        assert value['cash_coverage'] == 'unknown'
        holding, = value['candidates']
        assert Decimal(holding['quantity']) == 100
        assert Decimal(holding['market_value']) == 1200
        assert Decimal(holding['remaining_cost']) == Decimal(holding['diluted_cost'])
        body = {'preview_token': value['preview_token']}
        assert web.post(root + '/trade-snapshot/publish', json=body).status_code == 400
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_holdings_snapshot')
            assert cur.fetchone()[0] == 0
        body['confirmed_scope'] = True
        saved = web.post(root + '/trade-snapshot/publish', json=body)
        assert saved.status_code == 200, saved.text
        assert web.post(root + '/trade-snapshot/publish', json=body).json()['replayed'] is True

    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT *, id AS holding_id FROM pams_holdings_snapshot')
            row, = enrich_trade_snapshot_costs(cur, cur.fetchall())
        assert row['diluted_cost'] == Decimal(holding['diluted_cost'])


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
        root = '/api/account-import/source-records'
        response = web.post(root + '/manual', json={'account_code': 'tonghuashun', 'document_type': 'trade'})
        assert response.status_code == 200, response.text
        path = root + '/' + response.json()['source_id']
        response = web.put(path + '/reviews', json={'revision': 1, 'items': [
            {'parsed_fields': trade(), 'review_status': 'confirmed'}]})
        assert response.status_code == 200, response.text
        payload = {'revision': response.json()['revision']}
        check = web.post(path + '/trade-preview', json=payload)
        assert check.status_code == 200, check.text
        payload['preview_token'] = check.json()['preview_token']
        result = web.post(path + '/trade-publish', json=payload)
        assert result.status_code == 200, result.text
        replay = web.post(path + '/trade-publish', json=payload)
        assert replay.status_code >= 400
        response = web.get('/api/investment-transactions?account_code=tonghuashun')
        assert response.status_code == 200, response.text
        assert any(row['symbol'] == '000001' and float(row['quantity']) == 100
                   for row in response.json()['rows'])
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(source_item_id) FROM pams_investment_trade_fact")
        assert cur.fetchone() == (1, 1)
        cur.execute("SELECT to_regclass('public.pams_investment_transactions')")
        assert cur.fetchone()[0] is None


def test_ths_template_preflights_pending_rows_and_publishes_facts_without_reconciliation(empty_source_db, monkeypatch):
    from decimal import Decimal

    import main
    from domain.imports import account_import_store as store
    from tests.imports.test_account_import_ths import event, export

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(store, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    root = '/api/account-import/source-records'
    url = root + '?account_code=tonghuashun&source_name=tonghuashun&filename=table.xls'
    buy = event(**{'成交均价': '10.0001'})
    deposit = event('银行转证券', **{'定位串': 'transfer', '发生金额': '500'})
    invalid = event('未知业务', **{'定位串': 'invalid'})
    with TestClient(main.app, base_url='http://localhost') as web:
        web.headers['Origin'] = 'http://localhost'

        def stage(*rows):
            response = web.post(url, content=export(*rows))
            assert response.status_code == 200, response.text
            body = response.json()
            return root + '/' + body['source_id'], {'revision': body['revision']}

        path, revision = stage(buy)
        check = web.post(path + '/trade-preview', json=revision)
        assert check.status_code == 200, check.text
        assert check.json()['can_publish'] and check.json()['rows'][0]['status'] == 'ready'
        assert web.get(path).json()['items'][0]['review_status'] == 'pending'
        published = web.post(path + '/trade-publish', json={**revision, 'preview_token': check.json()['preview_token']})
        assert published.status_code == 200 and published.json()['published'] == 1, published.text

        path, revision = stage({**buy, '发生金额': '-1004'}, deposit, invalid)
        check = web.post(path + '/trade-preview', json=revision)
        assert check.status_code == 200, check.text
        value = check.json()
        assert not value['can_publish'] and value['can_publish_ready'] and not value['preview_token']
        assert sorted(row['status'] for row in value['rows']) == ['blocked', 'duplicate', 'ready']
        assert value['items'][0]['trade_type'] == 'deposit'
        ready = web.post(path + '/trade-preview', json={**revision, 'mode': 'ready'}).json()
        request = {**revision, 'mode': 'ready', 'preview_token': ready['preview_token']}
        assert web.post(path + '/trade-publish', json={**request, 'mode': 'all'}).status_code >= 400
        published = web.post(path + '/trade-publish', json=request)
        assert published.status_code == 200, published.text
        assert published.json()['published'] == 1 and published.json()['deferred_count'] == 2
        deferred = web.get(root + '/' + published.json()['deferred_source_id']).json()
        assert deferred['status'] == 'draft' and len(deferred['items']) == 2
        assert all(item['source_position']['deferred_from'] for item in deferred['items'])
        assert web.post(path + '/trade-publish', json=request).status_code >= 400

        no_ref = event(**{'成交日期': '20251021', '定位串': '', '成交编号': '000'})
        path, revision = stage(no_ref, {**no_ref, '发生金额': '-1003.1000', '定位串': 'new-ref'},
                               {**no_ref, '成交时间': '10:00:00'})
        check = web.post(path + '/trade-preview', json=revision)
        assert check.status_code == 200, check.text
        assert sorted(row['status'] for row in check.json()['rows']) == ['duplicate', 'duplicate', 'ready']
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT trade_type,net_cash,asset_id FROM pams_investment_trade_fact ORDER BY trade_type')
        assert cur.fetchall() == [('buy', Decimal('-1003.100'), None), ('deposit', Decimal('500'), None)]
        cur.execute('SELECT (SELECT count(*) FROM pams_cashflow_fact), (SELECT count(*) FROM evidence_confirmation)')
        assert cur.fetchone() == (0, 0)
