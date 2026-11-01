"""Synthetic source publication and strictly read-only snapshot preview."""
from datetime import date, datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import psycopg2
from psycopg2 import sql as pgsql
import pytest
from psycopg2.extras import RealDictCursor

from tests.imports.test_account_import_publish import client, draft, preview, publish, sql, trade, DAY
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates
from domain.investments.trade_snapshot_source import load_verified_equity_events
from domain.investments.trade_snapshot_writer import preview as writer_preview, publish_isolated
from domain.investments.trade_snapshot_quotes import stage_isolated_quotes
from domain.investments.trade_snapshot_display import enrich_trade_snapshot_costs
from domain.investments.holdings_positions import _apply_owner_ratio


def test_verified_trade_builds_preview_without_writing_holdings(client):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    check = preview(web, batch)
    assert publish(web, batch, check).status_code == 200
    before = sql(params, 'SELECT trade_id FROM pams_investment_trade_fact')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            events, identities = load_verified_equity_events(cur, 'tonghuashun')
            day = date.fromisoformat(DAY)
            result = build_trade_snapshot_candidates(
                events=events,
                identities=identities,
                quotes={'000001': {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'),
                                   'price_date': day, 'price_time': datetime(day.year, day.month, day.day, tzinfo=timezone.utc)}},
                as_of=day, closed_adjustments=[])
            missing_time = [{**row, 'trade_time': None} for row in events]
            with pytest.raises(ValueError, match='ordering'):
                build_trade_snapshot_candidates(events=missing_time, identities={}, quotes={}, as_of=day)
    assert len(result) == 1
    assert result[0]['remaining_cost'] == Decimal('1005')
    assert result[0]['market_value'] == Decimal('1200')
    assert result[0]['total_pnl_status'] == 'coverage_unverified'
    assert not sql(params, 'SELECT * FROM pams_holdings_snapshot')
    assert sql(params, 'SELECT trade_id FROM pams_investment_trade_fact') == before
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=net_cash-1')
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='source verification failed'):
                load_verified_equity_events(cur, 'tonghuashun')


def test_isolated_snapshot_write_binds_inputs_and_rejects_replay(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    sql(params, '''CREATE TABLE market_observations (
        quote_id bigint primary key, observation_type text, symbol text, asset_id text, currency text,
        provider text, source text, as_of date, quote_date date, price numeric, price_time timestamptz)''')
    sql(params, '''INSERT INTO market_observations VALUES
        (1,'quote','sz000001','TEST_STOCK','CNY','tencent','tencent_stock_quote',%s,%s,12,%s)''',
        (DAY, DAY, datetime.fromisoformat(DAY).replace(tzinfo=timezone.utc)))
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            candidate = writer_preview(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY), quote_ids={'000001': 1})
    with pytest.raises(ValueError, match='preview is stale'):
        with psycopg2.connect(**params) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                 quote_ids={'000001': 1}, expected_digest='0' * 64, confirmed_scope=True)
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    import domain.investments.trade_snapshot_writer as writer
    original = writer.finish_holding_change
    def fail_before_evidence(*args, **kwargs):
        raise RuntimeError('synthetic evidence failure')
    monkeypatch.setattr(writer, 'finish_holding_change', fail_before_evidence)
    with pytest.raises(RuntimeError, match='synthetic evidence failure'):
        with psycopg2.connect(**params) as conn:
            conn.set_session(isolation_level='SERIALIZABLE')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                 quote_ids={'000001': 1}, expected_digest=candidate['digest'], confirmed_scope=True)
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    monkeypatch.setattr(writer, 'finish_holding_change', original)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                      quote_ids={'000001': 1}, expected_digest=candidate['digest'], confirmed_scope=True)
    assert len(result['holding_ids']) == 1
    assert result['replayed'] is False
    rows = sql(params, 'SELECT quantity,cost,market_value FROM pams_holdings_snapshot')
    assert rows[0]['quantity'] == Decimal('100')
    assert rows[0]['cost'] == Decimal('1005')
    assert rows[0]['market_value'] == Decimal('1200')
    assert sql(params, 'SELECT calculation_version FROM valuation_evidence WHERE calculation_version=%s', ('holding-change-after',))
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            replay = publish_isolated(cur, account_code='tonghuashun', as_of=date.fromisoformat(DAY),
                                      quote_ids={'000001': 1}, expected_digest=candidate['digest'], confirmed_scope=True)
            assert replay['replayed'] is True
    assert len(sql(params, 'SELECT id FROM pams_holdings_snapshot')) == 1


def test_concurrent_isolated_publication_never_duplicates_snapshot(client):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    sql(params, '''CREATE TABLE market_observations (
        quote_id bigint primary key, observation_type text, symbol text, asset_id text, currency text,
        provider text, source text, as_of date, quote_date date, price numeric, price_time timestamptz)''')
    sql(params, '''INSERT INTO market_observations VALUES
        (1,'quote','sz000001','TEST_STOCK','CNY','tencent','tencent_stock_quote',%s,%s,12,%s)''',
        (DAY, DAY, datetime.fromisoformat(DAY).replace(tzinfo=timezone.utc)))
    day = date.fromisoformat(DAY)
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            digest = writer_preview(cur, account_code='tonghuashun', as_of=day, quote_ids={'000001': 1})['digest']
    gate = Barrier(2)
    def run():
        try:
            with psycopg2.connect(**params) as conn:
                conn.set_session(isolation_level='SERIALIZABLE')
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    gate.wait(timeout=10)
                    return publish_isolated(cur, account_code='tonghuashun', as_of=day,
                                            quote_ids={'000001': 1}, expected_digest=digest, confirmed_scope=True)
        except psycopg2.errors.SerializationFailure:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert any(result and not result['replayed'] for result in results)
    assert len(sql(params, 'SELECT id FROM pams_holdings_snapshot')) == 1
    assert len(sql(params, "SELECT batch_id FROM source_batches WHERE workflow='trade_snapshot'")) == 1


def test_quote_staging_keeps_actual_time_and_does_not_claim_confirmed_close(client):
    _, params = client
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date.fromisoformat(DAY)
    stamp = datetime(day.year, day.month, day.day, 7, 15, tzinfo=timezone.utc)
    identities = {'000001': {'symbol': '000001', 'asset_id': 'TEST_STOCK', 'name': 'Test stock'}}
    quotes = {'000001': {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'),
                         'price_date': day, 'price_time': stamp}}
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            ids = stage_isolated_quotes(cur, as_of=day, identities=identities, quotes=quotes)
    assert ids['000001'] > 0
    row = sql(params, 'SELECT price,price_time,return_status FROM market_observations')[0]
    assert row['price'] == Decimal('12') and row['price_time'] == stamp
    assert row['return_status'] == 'pending'


def test_synthetic_verified_trade_and_quote_flow_to_snapshot_with_provenance(client):
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date.fromisoformat(DAY)
    identities = {'000001': {'symbol': '000001', 'asset_id': 'TEST_STOCK', 'name': 'Test stock'}}
    quotes = {'000001': {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'),
                         'price_date': day, 'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}}
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            quote_ids = stage_isolated_quotes(cur, as_of=day, identities=identities, quotes=quotes)
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            candidate = writer_preview(cur, account_code='tonghuashun', as_of=day, quote_ids=quote_ids)
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish_isolated(cur, account_code='tonghuashun', as_of=day, quote_ids=quote_ids,
                                      expected_digest=candidate['digest'], confirmed_scope=True)
    assert result['replayed'] is False
    row = sql(params, 'SELECT quantity,cost,market_value,current_price FROM pams_holdings_snapshot')[0]
    assert (row['quantity'], row['cost'], row['market_value'], row['current_price']) == (
        Decimal('100'), Decimal('1005'), Decimal('1200'), Decimal('12'))
    assert sql(params, "SELECT batch_id FROM source_batches WHERE workflow='trade_snapshot'")


def test_http_preview_and_publish_are_signed_and_idempotent(client, monkeypatch):
    from domain.investments import trade_snapshot_http
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date.fromisoformat(DAY)
    quote = {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'), 'price_date': day,
             'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz000001': quote})
    check = web.post('/api/account-import/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
    assert check.status_code == 200, check.text
    data = check.json()
    assert data['as_of'] == DAY and data['scope'] == 'securities_only'
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    token = data['preview_token']
    expired_payload = trade_snapshot_http._open(token)
    expired_payload['issued'] -= 901
    expired = trade_snapshot_http._seal(expired_payload)
    assert web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': expired, 'confirmed_scope': True}).status_code == 400
    wrong_origin = web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': token, 'confirmed_scope': True},
                            headers={'Origin': 'http://untrusted.example'})
    assert wrong_origin.status_code == 403
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'false')
    assert web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': token, 'confirmed_scope': True}).status_code == 403
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=net_cash-1')
    changed = web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': token, 'confirmed_scope': True})
    assert changed.status_code == 400
    assert not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    assert not sql(params, 'SELECT quote_id FROM market_observations')
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=net_cash+1')
    denied = web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': token, 'confirmed_scope': False})
    assert denied.status_code == 400 and not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    bad = web.post('/api/account-import/trade-snapshot/publish', json={'preview_token': token + 'x', 'confirmed_scope': True})
    assert bad.status_code == 400 and not sql(params, 'SELECT id FROM pams_holdings_snapshot')
    body = {'preview_token': token, 'confirmed_scope': True}
    response = web.post('/api/account-import/trade-snapshot/publish', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['replayed'] is False
    second = web.post('/api/account-import/trade-snapshot/publish', json=body)
    assert second.status_code == 200, second.text
    assert second.json()['replayed'] is True
    assert len(sql(params, 'SELECT id FROM pams_holdings_snapshot')) == 1
    assert len(sql(params, "SELECT quote_id FROM market_observations WHERE observation_type='quote'")) == 1
    def displayed():
        with psycopg2.connect(**params) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''SELECT id AS holding_id, account_code AS account,symbol,currency,asset_id,quantity,
                                      cost,market_value,as_of FROM pams_holdings_snapshot''')
                return enrich_trade_snapshot_costs(cur, [dict(cur.fetchone())])[0]
    visible = displayed()
    assert visible['diluted_cost'] == Decimal('1005')
    assert visible['holding_pnl'] == Decimal('195')
    assert visible['realized_trade_pnl'] == visible['net_distributions'] == 0
    sql(params, 'UPDATE pams_investment_trade_fact SET net_cash=net_cash-1')
    assert 'diluted_cost_source' not in displayed()


def test_published_dual_cost_does_not_double_count_realized_profit(client, monkeypatch):
    from domain.investments import trade_snapshot_http
    web, params = client
    batch = draft(web, [
        trade(trade_time='09:00:00', quantity='200', gross_amount='2000', fee_amount='0', net_cash='-2000'),
        trade(trade_time='10:00:00', action='sell', quantity='100', price='12', gross_amount='1200', fee_amount='0', net_cash='1200'),
    ])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date.fromisoformat(DAY)
    quote = {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('15'), 'price_date': day,
             'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz000001': quote})
    check = web.post('/api/account-import/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
    assert check.status_code == 200, check.text
    response = web.post('/api/account-import/trade-snapshot/publish',
                        json={'preview_token': check.json()['preview_token'], 'confirmed_scope': True})
    assert response.status_code == 200, response.text
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''SELECT id AS holding_id,account_code AS account,symbol,currency,asset_id,quantity,
                                  cost,market_value,as_of FROM pams_holdings_snapshot''')
            visible = enrich_trade_snapshot_costs(cur, [dict(cur.fetchone())])[0]
    assert visible['cost'] == Decimal('1000')
    assert visible['diluted_cost'] == Decimal('800')
    assert visible['realized_trade_pnl'] == Decimal('200')
    assert visible['holding_pnl'] == Decimal('700')


def test_native_dividend_and_tax_feed_diluted_cost_without_double_counting(client, monkeypatch):
    from tests.imports.test_account_import_ths import event, export
    from domain.investments import trade_snapshot_http
    web, params = client
    source = [
        event(**{'成交数量': '200', '成交金额': '2000', '发生金额': '-2000', '手续费': '0', '其他杂费': '0', '定位串': 'buy'}),
        event('证券卖出', **{'成交数量': '100', '成交均价': '12', '成交金额': '1200', '发生金额': '1200',
                            '手续费': '0', '印花税': '0', '其他杂费': '0', '成交时间': '10:00:00', '定位串': 'sell', '成交编号': 'sell'}),
        event('红利入账', **{'成交金额': '30', '发生金额': '30', '成交时间': '11:00:00', '定位串': 'dividend'}),
        event('红利税补扣', **{'发生金额': '-5', '成交时间': '12:00:00', '定位串': 'tax'}),
    ]
    batch = web.post('/api/account-import/batches', json={'account_code': 'tonghuashun', 'mode': 'upload',
                     'kind': 'trade', 'completeness': 'partial'}).json()
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls",
                        content=export(*source), headers={'Content-Type': 'application/vnd.ms-excel'})
    assert response.status_code == 200, response.text
    batch = response.json()
    response = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={'revision': batch['revision'], 'rows': batch['rows']})
    assert response.status_code == 200, response.text
    batch = response.json()
    check = preview(web, batch)
    assert check['can_publish'], {key: value for key, value in check.items() if key != 'rows'}
    assert publish(web, batch, check).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date(2025, 10, 20)
    quote = {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('15'), 'price_date': day,
             'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz000001': quote})
    response = web.post('/api/account-import/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
    assert response.status_code == 200, response.text
    response = web.post('/api/account-import/trade-snapshot/publish',
                        json={'preview_token': response.json()['preview_token'], 'confirmed_scope': True})
    assert response.status_code == 200, response.text
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''SELECT id AS holding_id,account_code AS account,symbol,currency,asset_id,quantity,
                                  cost,market_value,as_of FROM pams_holdings_snapshot''')
            visible = enrich_trade_snapshot_costs(cur, [dict(cur.fetchone())])[0]
    assert visible['cost'] == Decimal('1000')
    assert visible['diluted_cost'] == Decimal('775')
    assert visible['realized_trade_pnl'] == Decimal('200')
    assert visible['net_distributions'] == Decimal('25')
    assert visible['holding_pnl'] == Decimal('725')
    visible.update(cost_cny=visible['cost'], market_value_cny=visible['market_value'])
    owned = _apply_owner_ratio(visible, owner_ratios={'tonghuashun': Decimal('0.625')}, default_owner_ratio=Decimal('1'))
    assert owned['holding_pnl_cny'] == Decimal('453.125')
    assert owned['diluted_cost_gross_cny'] == Decimal('775')


def test_large_verified_trade_set_can_publish_one_snapshot(client, monkeypatch):
    from domain.investments import trade_snapshot_http
    web, params = client
    rows = [trade(trade_time=f'09:{index // 60:02}:{index % 60:02}', quantity=str(index + 1),
                  gross_amount=str((index + 1) * 10), fee_amount='0', net_cash=str(-((index + 1) * 10)))
            for index in range(116)]
    batch = draft(web, rows)
    check = preview(web, batch)
    assert check['can_publish'], check['counts']
    assert publish(web, batch, check).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    day = date.fromisoformat(DAY)
    quote = {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'), 'price_date': day,
             'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz000001': quote})
    candidate = web.post('/api/account-import/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
    assert candidate.status_code == 200, candidate.text
    response = web.post('/api/account-import/trade-snapshot/publish',
                        json={'preview_token': candidate.json()['preview_token'], 'confirmed_scope': True})
    assert response.status_code == 200, response.text
    assert sql(params, 'SELECT quantity FROM pams_holdings_snapshot')[0]['quantity'] == Decimal(sum(range(1,117)))


def test_explicit_runtime_identity_and_publish_gate_in_disposable_database(client, monkeypatch):
    from domain.investments import trade_snapshot_http
    web, params = client
    batch = draft(web, [trade(trade_time='10:00:00')])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/025a_asset_snapshots.sql').read_text(encoding='utf-8')
    sql(params, schema[schema.index('-- One physical store'):])
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute(pgsql.SQL('COMMENT ON DATABASE {} IS {}').format(
            pgsql.Identifier(params['dbname']), pgsql.Literal('pams-empty-bootstrap')))
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_ENABLED', 'true')
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_DATABASE', params['dbname'])
    monkeypatch.setenv('PAMS_IMPORT_RUNTIME_TOKEN_KEY', 'b2' * 32)
    day = date.fromisoformat(DAY)
    quote = {'symbol': 'sz000001', 'source': 'tencent', 'price': Decimal('12'), 'price_date': day,
             'price_time': datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(trade_snapshot_http, 'fetch_tencent_stock_quotes', lambda symbols: {'sz000001': quote})
    candidate = web.post('/api/account-import/trade-snapshot/preview', json={'account_code': 'tonghuashun'})
    assert candidate.status_code == 200, candidate.text
    response = web.post('/api/account-import/trade-snapshot/publish',
                        json={'preview_token': candidate.json()['preview_token'], 'confirmed_scope': True})
    assert response.status_code == 200, response.text
    assert len(sql(params, 'SELECT id FROM pams_holdings_snapshot')) == 1
