"""Actual IBKR/TradingView holding writers with explicitly unknown inputs."""
# ruff: noqa: F811
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from collectors import ibkr_flex, tradingview_portfolio as tv
from tests.database.test_tradingview_source_publish import review_all
from domain.investments import valuation_input_evidence as valuation
from domain.investments import holding_write_evidence as holding_evidence
from domain.investments import holding_evidence_writer as shared_writer
from tests.database.test_ibkr_zero_holding_source import stage as stage_ibkr, confirm as confirm_ibkr, xml as ibkr_xml, ZERO

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 22)


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE pams_accounts ADD COLUMN is_asset boolean DEFAULT true')
        cur.execute('ALTER TABLE pams_asset_alias ADD COLUMN priority integer DEFAULT 1')
        cur.execute('''CREATE TABLE pams_asset_categories(major_category text,minor_category text,
                       UNIQUE(major_category,minor_category))''')
        cur.execute('''CREATE TABLE pams_fx_rates_daily(as_of date,base_currency text,quote_currency text,
                       rate numeric,source text,UNIQUE(as_of,base_currency,quote_currency))''')
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    return params


def ibkr_row():
    return {'account_code': 'ibkr', 'symbol': 'SYNTH', 'name': 'Synthetic', 'asset_liability': '资产',
            'currency': 'USD', 'major_category': '美股', 'minor_category': '美股基金',
            'market_value': 100, 'cost': 80, 'quantity': 10, 'buy_price': 8, 'current_price': 10, 'as_of': DAY}


def tv_inputs():
    rows = [tv.TradingViewTransaction('fixture.csv', 'a' * 64, 'h1', '$CASH', None, 'Deposit',
                Decimal('1000'), None, Decimal('0'), datetime(2026, 10, 1), {}),
            tv.TradingViewTransaction('fixture.csv', 'a' * 64, 'h2', 'SSE:600000', '600000', 'Buy',
                Decimal('10'), Decimal('8'), Decimal('1'), datetime(2026, 10, 2), {})]
    result = tv.calculate_portfolio(rows, {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')},
                                    as_of=DAY, account_code='tonghuashun')
    return rows, result


def verified_tv_inputs(isolated, tmp_path):
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import publish

    path = tmp_path / 'verified.csv'
    path.write_text('Symbol,Side,Qty,Fill Price,Commission,Closing Time,Tax\n'
                    '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
                    'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,0\n', encoding='utf-8')
    batch_id = uuid4()
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        stage_csvs(cur, batch_id=batch_id, account_code='tonghuashun', files=[(path.name, path.read_bytes())])
        review_all(cur,batch_id)
        publish(cur, batch_id=batch_id, confirm=True)
    rows = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(rows, {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')},
                                    as_of=DAY, account_code='tonghuashun')
    return rows, result


def test_ibkr_actual_writer_records_report_as_unknown(isolated):
    with psycopg2.connect(**isolated) as conn:
        assert ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY) == 1
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT version_key FROM valuation_evidence')
            key = cur.fetchone()['version_key']
            assert valuation.bound_holding_freshness(cur, key) == 'unknown'
    values = sql(isolated, 'SELECT evidence FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb)')
    unknown = [row['evidence'] for row in values if row['evidence']['role'] == 'ibkr_flex_statement']
    assert len(unknown) == 1 and unknown[0]['source_ref'] is None and unknown[0]['snapshot'] is None


def test_ibkr_mismatched_account_is_rejected_before_write(isolated):
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='account conflicts'):
            ibkr_flex._write_holdings(conn, [{**ibkr_row(), 'account_code': 'tonghuashun'}], account_code='ibkr', as_of=DAY)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0


def test_same_name_but_different_symbol_is_not_silently_replaced(isolated):
    sql(isolated, "INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,as_of,market_value) VALUES ('ibkr','Synthetic','DIFFERENT','USD',%s,123)", (DAY,))
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='identity or currency'):
            ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
    assert sql(isolated, "SELECT symbol,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr'")[0]['market_value'] == Decimal('123')


def test_ibkr_evidence_error_rolls_back_replacement(isolated, monkeypatch):
    sql(isolated, "INSERT INTO pams_accounts(account_code,display_name,base_currency,active) VALUES ('ibkr','Old','USD',true)")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,as_of,market_value) VALUES ('ibkr','Synthetic','SYNTH','USD',%s,123)", (DAY,))
    monkeypatch.setattr(shared_writer, 'record_bound_holding_version', lambda *a, **k: (_ for _ in ()).throw(ValueError('synthetic failure')))
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='synthetic failure'):
            ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
        with conn.cursor() as cur:
            cur.execute("SELECT name,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr'")
            assert cur.fetchall() == [('Synthetic', Decimal('123.0000'))]


def test_ibkr_recalculation_invalidates_old_result_without_fabricating_inputs(isolated):
    with psycopg2.connect(**isolated) as conn:
        ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT version_key FROM valuation_evidence')
            old_version = cur.fetchone()['version_key']
        updated = {**ibkr_row(), 'market_value': 120, 'current_price': 12}
        ibkr_flex._write_holdings(conn, [updated], account_code='ibkr', as_of=DAY)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert valuation.bound_holding_freshness(cur, old_version) == 'changed'
            cur.execute("SELECT version_key FROM valuation_evidence WHERE version_key<>%s AND calculation_version='holding-change-after'", (old_version,))
            new_version = cur.fetchone()['version_key']
            assert valuation.bound_holding_freshness(cur, new_version) == 'unknown'


def test_same_holding_replay_preserves_identity_and_stabilizes_versions(isolated):
    with psycopg2.connect(**isolated) as conn:
        ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
        ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
        with conn.cursor() as cur:
            cur.execute('SELECT id,market_value,updated_at FROM pams_holdings_snapshot')
            before = cur.fetchone()
            cur.execute('SELECT count(*) FROM valuation_evidence')
            versions = cur.fetchone()[0]
        ibkr_flex._write_holdings(conn, [ibkr_row()], account_code='ibkr', as_of=DAY)
        with conn.cursor() as cur:
            cur.execute('SELECT id,market_value,updated_at FROM pams_holdings_snapshot')
            assert cur.fetchone() == before
            cur.execute('SELECT count(*) FROM valuation_evidence')
            assert cur.fetchone()[0] == versions


def test_tradingview_actual_writer_records_proven_trades_and_unknown_mark(isolated, tmp_path):
    rows, result = verified_tv_inputs(isolated, tmp_path)
    with psycopg2.connect(**isolated) as conn:
        tv.sync_portfolio_to_db(conn, result, rows)
    evidence = [row['evidence'] for row in sql(isolated, 'SELECT evidence FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb)')]
    assert sum(item['role'] == 'tradingview_transaction_input' for item in evidence) == 3
    assert sum(item['role'] == 'tradingview_mark_input' for item in evidence) == 1
    assert all(item['source_ref'] is None and item['snapshot'] is None and item['quality'] == 'unknown'
               for item in evidence if item['role'] == 'tradingview_mark_input')
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 2


@pytest.mark.parametrize('tamper_before_capture', [False, True])
def test_proven_tradingview_trade_is_bound_and_change_detected(isolated, tmp_path, tamper_before_capture):
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import publish

    path = tmp_path / 'verified.csv'
    path.write_text('Symbol,Side,Qty,Fill Price,Commission,Closing Time,Tax\n'
                    '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
                    'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,0\n', encoding='utf-8')
    batch_id = uuid4()
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        stage_csvs(cur, batch_id=batch_id, account_code='tonghuashun', files=[(path.name, path.read_bytes())])
        review_all(cur,batch_id)
        publish(cur, batch_id=batch_id, confirm=True)
        if tamper_before_capture:
            cur.execute('UPDATE pams_investment_trade_fact SET fee_amount=99')
    rows = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(rows, {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')},
                                    as_of=DAY, account_code='tonghuashun')
    with psycopg2.connect(**isolated) as conn:
        if tamper_before_capture:
            with pytest.raises(ValueError, match='verified source and fact'):
                tv.sync_portfolio_to_db(conn, result, rows)
            return
        tv.sync_portfolio_to_db(conn, result, rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT version_key FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE evidence->>'role'='tradingview_transaction_input' LIMIT 1")
            found = cur.fetchone()
            version = found['version_key']
            cur.execute("SELECT (evidence->'source_ref'->'key'->>'trade_id')::bigint AS id FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE version_key=%s AND evidence->>'role'='tradingview_transaction_input' LIMIT 1", (version,))
            trade_id = cur.fetchone()['id']
            assert valuation.bound_holding_freshness(cur, version) == 'unknown'
            cur.execute('UPDATE pams_investment_trade_fact SET fee_amount=fee_amount+1 WHERE trade_id=%s', (trade_id,))
            assert valuation.bound_holding_freshness(cur, version) == 'changed'


def test_published_holding_source_allows_ibkr_update_with_immutable_old_snapshot(isolated):
    row = ibkr_row()
    batch_id, item_id = str(uuid4()), str(uuid4())
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
        cur.execute("INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method,date_from,date_to) VALUES (%s,'ibkr','holding','ibkr','api',%s,%s)", (batch_id,DAY,DAY))
        cur.execute("INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status) VALUES (%s,%s,'origin','api',%s,'confirmed')", (item_id,batch_id,'a'*64))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,as_of,market_value,source_item_id) VALUES ('ibkr','Synthetic','SYNTH','USD',%s,123,%s)", (DAY,item_id))
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s", ('b'*64,batch_id))
    with psycopg2.connect(**isolated) as conn:
        ibkr_flex._write_holdings(conn, [row], account_code='ibkr', as_of=DAY)
        with conn.cursor() as cur:
            cur.execute("SELECT name,market_value,source_item_id,id FROM pams_holdings_snapshot WHERE account_code='ibkr'")
            old = cur.fetchone()
            assert old[0] == 'Synthetic' and old[1] == Decimal('100') and str(old[2]) == item_id
            assert valuation.current_holding_source(cur, old[3])['status'] == 'unknown'
            cur.execute("SELECT evidence->'snapshot'->'content'->>'market_value' FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='holding'")
            assert {Decimal(item[0]) for item in cur.fetchall()} == {Decimal('123'), Decimal('100')}


def test_published_holding_source_allows_tradingview_update_with_old_snapshot(isolated, tmp_path):
    batch_id, item_id = str(uuid4()), str(uuid4())
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method,date_from,date_to) VALUES (%s,'tonghuashun','holding','test','manual',%s,%s)", (batch_id,DAY,DAY))
        cur.execute("INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status) VALUES (%s,%s,'origin','manual',%s,'confirmed')", (item_id,batch_id,'a'*64))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,source_item_id) VALUES ('tonghuashun','Synthetic stock','600000','CNY','A股',%s,123,%s)", (DAY,item_id))
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s", ('b'*64,batch_id))
    rows, result = verified_tv_inputs(isolated, tmp_path)
    with psycopg2.connect(**isolated) as conn:
        tv.sync_portfolio_to_db(conn, result, rows)
    old = sql(isolated, "SELECT name,market_value,source_item_id FROM pams_holdings_snapshot WHERE account_code='tonghuashun' AND name='Synthetic stock'")[0]
    assert old['name'] == 'Synthetic stock' and old['market_value'] == Decimal('90') and str(old['source_item_id']) == item_id
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM pams_holdings_snapshot WHERE account_code='tonghuashun' AND name='Synthetic stock'")
        assert valuation.current_holding_source(cur, cur.fetchone()['id'])['status'] == 'unknown'
    values = sql(isolated, "SELECT evidence->'snapshot'->'content'->>'market_value' AS value FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='holding'")
    assert Decimal('123') in {Decimal(item['value']) for item in values}


@pytest.mark.parametrize('origin', ['ibkr', 'tradingview'])
def test_unknown_omission_does_not_retire_existing_position(isolated, origin):
    account = 'ibkr' if origin == 'ibkr' else 'tonghuashun'
    if origin == 'ibkr':
        sql(isolated, "INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value) VALUES (%s,'Omitted','OLD','CNY','A股',%s,123)", (account, DAY))
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='omission requires explicit'):
            if origin == 'ibkr':
                ibkr_flex._write_holdings(conn, [ibkr_row()], account_code=account, as_of=DAY)
            else:
                rows, result = tv_inputs()
                tv.sync_portfolio_to_db(conn, result, rows)
    old = sql(isolated, 'SELECT name,market_value FROM pams_holdings_snapshot WHERE account_code=%s', (account,))
    assert len(old) == 1 and old[0]['name'] == 'Omitted' and old[0]['market_value'] == Decimal('123')


@pytest.mark.parametrize('invalid_reason', [None, 'missing_cost', 'currency_mismatch'])
def test_ibkr_zero_without_bound_contract_identity_is_never_retired(isolated, invalid_reason):
    batch_id, original_item = str(uuid4()), str(uuid4())
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
        cur.execute("INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method,date_from,date_to) VALUES (%s,'ibkr','holding','ibkr','api',%s,%s)", (batch_id,DAY,DAY))
        cur.execute("INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status) VALUES (%s,%s,'original','api',%s,'confirmed')", (original_item,batch_id,'a'*64))
        cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity,source_item_id) VALUES ('ibkr','Synthetic','SYNTH','USD','美股',%s,123,80,10,%s)", (DAY,original_item))
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s", ('b'*64,batch_id))
    position = ZERO.replace('symbol="TEST"', 'symbol="SYNTH"')
    if invalid_reason == 'missing_cost':
        position = position.replace(' costBasisMoney="0"', '')
    elif invalid_reason == 'currency_mismatch':
        position = position.replace(' currency="USD"', ' currency="EUR"')
    staged = stage_ibkr(isolated, ibkr_xml(position))
    confirm_ibkr(isolated, staged)
    ref = {'Synthetic': {'batch_id': staged['batch_id'], 'item_id': staged['position_item_ids'][0]}}
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='verified existing conid identity'):
            ibkr_flex._write_holdings(conn, [], account_code='ibkr', as_of=DAY, retirement_sources=ref)
    old = sql(isolated, "SELECT market_value,source_item_id FROM pams_holdings_snapshot WHERE account_code='ibkr'")[0]
    assert old['market_value'] == Decimal('123')
    assert str(old['source_item_id']) == original_item


def test_ibkr_retirement_guard_runs_before_any_holding_or_evidence_write(isolated, monkeypatch):
    sql(isolated, "INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity) VALUES ('ibkr','Synthetic','SYNTH','USD','美股',%s,123,80,10)", (DAY,))
    staged = stage_ibkr(isolated, ibkr_xml(ZERO.replace('symbol="TEST"', 'symbol="SYNTH"')))
    confirm_ibkr(isolated, staged)
    ref = {'Synthetic': {'batch_id': staged['batch_id'], 'item_id': staged['position_item_ids'][0]}}
    original = shared_writer.record_bound_holding_version
    def fail_after_write(cur, **kwargs):
        if kwargs['calculation_version'] == 'holding-change-after':
            raise ValueError('synthetic final evidence failure')
        return original(cur, **kwargs)
    monkeypatch.setattr(shared_writer, 'record_bound_holding_version', fail_after_write)
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='verified existing conid identity'):
            ibkr_flex._write_holdings(conn, [], account_code='ibkr', as_of=DAY, retirement_sources=ref)
        with conn.cursor() as cur:
            cur.execute("SELECT market_value,cost,quantity FROM pams_holdings_snapshot WHERE account_code='ibkr'")
            assert cur.fetchone() == (Decimal('123'), Decimal('80'), Decimal('10'))
            cur.execute('SELECT count(*) FROM valuation_evidence')
            assert cur.fetchone()[0] == 0


def test_ibkr_position_scope_cannot_retire_cash_balance(isolated):
    sql(isolated, "INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity) VALUES ('ibkr','Cash USD','CASH_USD','USD','现金',%s,123,123,123)", (DAY,))
    staged = stage_ibkr(isolated, ibkr_xml(ZERO.replace('symbol="TEST"', 'symbol="CASH_USD"')))
    confirm_ibkr(isolated, staged)
    ref = {'Cash USD': {'batch_id': staged['batch_id'], 'item_id': staged['position_item_ids'][0]}}
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='verified existing conid identity'):
            ibkr_flex._write_holdings(conn, [], account_code='ibkr', as_of=DAY, retirement_sources=ref)
    assert sql(isolated, "SELECT market_value FROM pams_holdings_snapshot WHERE account_code='ibkr'")[0]['market_value'] == Decimal('123')


def test_ibkr_zero_cannot_retire_ambiguous_existing_same_symbol(isolated):
    sql(isolated, "INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Test')")
    sql(isolated, "INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,as_of,market_value,cost,quantity) VALUES ('ibkr','One','SYNTH','USD','美股',%s,123,80,10),('ibkr','Two','SYNTH','USD','美股',%s,45,40,5)", (DAY,DAY))
    staged = stage_ibkr(isolated, ibkr_xml(ZERO.replace('symbol="TEST"', 'symbol="SYNTH"')))
    confirm_ibkr(isolated, staged)
    ref = {'batch_id': staged['batch_id'], 'item_id': staged['position_item_ids'][0]}
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='verified existing conid identity'):
            ibkr_flex._write_holdings(conn, [], account_code='ibkr', as_of=DAY, retirement_sources={'One': ref, 'Two': ref})
    assert sql(isolated, "SELECT sum(market_value) AS total FROM pams_holdings_snapshot WHERE account_code='ibkr'")[0]['total'] == Decimal('168')
def test_tradingview_evidence_error_rolls_back_all_writes(isolated, monkeypatch, tmp_path):
    rows, result = verified_tv_inputs(isolated, tmp_path)
    monkeypatch.setattr(shared_writer, 'record_bound_holding_version', lambda *a, **k: (_ for _ in ()).throw(ValueError('synthetic failure')))
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='synthetic failure'):
            tv.sync_portfolio_to_db(conn, result, rows)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0
    assert sql(isolated, "SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_table='tradingview_csv'")[0]['n'] == 2


def test_tradingview_recalculation_makes_previous_versions_missing(isolated, tmp_path):
    from domain.imports.tradingview_source import stage_csvs
    from domain.imports.tradingview_source_publish import publish

    path = tmp_path / 'verified.csv'
    path.write_text('Symbol,Side,Qty,Fill Price,Commission,Closing Time,Tax\n'
                    '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
                    'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,0\n', encoding='utf-8')
    batch_id = uuid4()
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        stage_csvs(cur, batch_id=batch_id, account_code='tonghuashun', files=[(path.name, path.read_bytes())])
        review_all(cur,batch_id)
        publish(cur, batch_id=batch_id, confirm=True)
    rows = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(rows, {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')},
                                    as_of=DAY, account_code='tonghuashun')
    with psycopg2.connect(**isolated) as conn:
        tv.sync_portfolio_to_db(conn, result, rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT version_key FROM valuation_evidence')
            previous = [item['version_key'] for item in cur.fetchall()]
        conn.rollback()
        new_result = tv.calculate_portfolio(rows, {'600000': tv.MarketMark(price=Decimal('10'), name='Synthetic stock')},
                                             as_of=DAY, account_code='tonghuashun')
        tv.sync_portfolio_to_db(conn, new_result, rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert any(valuation.bound_holding_freshness(cur, key) == 'changed' for key in previous)
            cur.execute("SELECT version_key FROM valuation_evidence WHERE NOT (version_key=ANY(%s)) AND calculation_version='holding-change-after'", (previous,))
            assert all(valuation.bound_holding_freshness(cur, item['version_key']) == 'unknown' for item in cur.fetchall())


def test_unproven_tradingview_hash_cannot_overwrite_old_raw_evidence(isolated):
    rows, result = tv_inputs()
    with psycopg2.connect(**isolated) as conn:
        with pytest.raises(ValueError, match='verified source and fact'):
            tv.sync_portfolio_to_db(conn, result, rows)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0
    assert sql(isolated, "SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_table='tradingview_csv'")[0]['n'] == 0


def test_same_account_day_writer_waits_for_cooperating_writer(isolated):
    started, finished = Event(), Event()
    failures = []
    with psycopg2.connect(**isolated) as first:
        holding_evidence.lock_account(first, 'ibkr')
        def write_other():
            try:
                with psycopg2.connect(**isolated) as second:
                    started.set()
                    ibkr_flex._write_holdings(second, [ibkr_row()], account_code='ibkr', as_of=DAY)
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()
        worker = Thread(target=write_other)
        worker.start()
        assert started.wait(5) and not finished.wait(0.25)
        first.commit()
        assert finished.wait(10)
        worker.join()
    assert not failures


def test_explicit_real_inputs_are_idempotent_and_each_change_is_detected(isolated):
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""CREATE TABLE market_observations(quote_id bigserial PRIMARY KEY, observation_type text,
            as_of date, quote_date date GENERATED ALWAYS AS (as_of) STORED,
            symbol text,price numeric,price_time timestamptz,return_status text)""")
        cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,symbol,quantity,source_table,source_id,source_hash)
            VALUES (%s,'tonghuashun','TEST',10,'synthetic','trade-1',%s) RETURNING trade_id""", (DAY,'a'*64))
        trade = cur.fetchone()['trade_id']
        cur.execute("""INSERT INTO market_observations(observation_type,as_of,symbol,price,price_time,return_status)
            VALUES ('quote',%s,'TEST',9,'2026-10-22T12:00:00Z','confirmed') RETURNING quote_id""", (DAY,))
        quote = cur.fetchone()['quote_id']
        cur.execute("INSERT INTO pams_fx_rates_daily VALUES (%s,'CNY','USD',7,'synthetic')", (DAY,))
        # Compute the synthetic result directly from the exact persisted inputs.
        cur.execute('''SELECT f.quantity*q.price*x.rate AS value FROM pams_investment_trade_fact f
            CROSS JOIN market_observations q CROSS JOIN pams_fx_rates_daily x
            WHERE f.trade_id=%s AND q.quote_id=%s AND x.as_of=%s''', (trade, quote, DAY))
        result_value = cur.fetchone()['value']
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,as_of,market_value,quantity)
            VALUES ('tonghuashun','Synthetic USD','TEST','CNY',%s,%s,10) RETURNING id""", (DAY,result_value))
        holding = cur.fetchone()['id']
        refs = [{'kind': 'trade', 'key': {'trade_id': trade}, 'role': 'quantity'},
                {'kind': 'quote', 'key': {'quote_id': quote}, 'role': 'mark'},
                {'kind': 'fx', 'key': {'as_of': DAY, 'base_currency': 'CNY', 'quote_currency': 'USD'}, 'role': 'conversion'}]
        key = valuation.record_bound_holding_version(cur, holding_id=holding, calculation_version='synthetic_units_mark_fx_v1', input_refs=refs)
        assert key == valuation.record_bound_holding_version(cur, holding_id=holding, calculation_version='synthetic_units_mark_fx_v1', input_refs=refs)
        assert valuation.bound_holding_freshness(cur, key) == 'unknown'
        changes = [('UPDATE pams_investment_trade_fact SET quantity=11 WHERE trade_id=%s', (trade,)),
                   ('UPDATE market_observations SET price=10 WHERE quote_id=%s', (quote,)),
                   ("UPDATE pams_fx_rates_daily SET rate=8 WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'", (DAY,))]
        for statement, values in changes:
            cur.execute('SAVEPOINT input_change')
            cur.execute(statement, values)
            assert valuation.bound_holding_freshness(cur, key) == 'changed'
            cur.execute('ROLLBACK TO SAVEPOINT input_change')
            cur.execute('RELEASE SAVEPOINT input_change')
