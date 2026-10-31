"""Price, fund and bank writers preserve isolated holding evidence."""
# ruff: noqa: F811
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.imports import account_import_statement as statement
from domain.investments.holding_evidence_writer import finish_holding_change
from domain.investments.valuation_input_evidence import current_holding_source
from domain.market.fund_nav_estimates import insert_or_update_generated_snapshot
from endpoints import prices
from endpoints.bank_reconcile import _sync_holding_snapshot_from_statement

from tests.database.test_native_holding_versions import isolated, publish_holding  # noqa: F401
from tests.imports.test_account_import_publish import (  # noqa: F401
    DAY,
    client,
    draft,
    holding,
    preview,
    publish,
    sql,
)


def _current(params, identifier):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return current_holding_source(cur, identifier)


def test_price_upsert_and_direct_update_preserve_history_and_noop_source(isolated, monkeypatch):
    web, params = isolated
    first, _, _ = publish_holding(web)
    row = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**params))
    day = date.fromisoformat(DAY)
    assert prices._upsert_price_snapshots([row], {'000001': 10}, day) == 1
    assert _current(params, row['id']) == {'status':'recorded', 'source_item_id':first['rows'][0]['row_id']}
    assert prices._upsert_price_snapshots([row], {'000001': 11}, day) == 1
    changed = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    assert changed['market_value'] == 1100 and changed['source_item_id'] == row['source_item_id']
    assert _current(params, row['id'])['status'] == 'unknown'
    assert prices._update_prices([(12, 1200, row['id'])]) == 1
    assert sql(params, 'SELECT market_value FROM pams_holdings_snapshot')[0]['market_value'] == 1200
    old_versions = sql(params, "SELECT evidence FROM pams_valuation_input_evidence WHERE input_kind='holding'")
    values = {str(item['evidence']['snapshot']['content']['market_value']) for item in old_versions}
    assert {'1000.0000', '1100.0000', '1200.0000'} <= values


def test_fund_nav_upsert_preserves_old_value_and_marks_quote_unknown(isolated):
    web, params = isolated
    publish_holding(web, symbol='011613', asset_type='fund')
    row = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        assert insert_or_update_generated_snapshot(cur, holding=row, as_of=date.fromisoformat(DAY),
            market_value=Decimal(1200), units=Decimal(100), unit_nav=Decimal(12), update_existing=True)
    changed = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    assert changed['market_value'] == 1200 and changed['source_item_id'] == row['source_item_id']
    assert _current(params, row['id'])['status'] == 'unknown'


def test_multiple_fund_updates_in_same_transaction_remain_valid(isolated):
    web, params = isolated
    publish_holding(web, symbol='011613', asset_type='fund')
    row = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        assert insert_or_update_generated_snapshot(cur, holding=row, as_of=date.fromisoformat(DAY),
            market_value=Decimal(1100), units=Decimal(100), unit_nav=Decimal(11), update_existing=True)
        assert insert_or_update_generated_snapshot(cur, holding=row, as_of=date.fromisoformat(DAY),
            market_value=Decimal(1200), units=Decimal(100), unit_nav=Decimal(12), update_existing=True)
    assert sql(params, 'SELECT market_value FROM pams_holdings_snapshot')[0]['market_value'] == 1200


def test_bank_balance_upsert_preserves_original_source(isolated, monkeypatch):
    web, params = isolated
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    batch = draft(web, [{'kind':'cash', 'business_date':DAY, 'currency':'CNY', 'action':'balance', 'net_cash':'100'}], account='bank_test')
    assert publish(web, batch, preview(web, batch)).status_code == 200
    old = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    result = {'account_code':'bank_test','account_kind':'debit','latest_balance':Decimal(150),
              'latest_balance_date':date.fromisoformat(DAY),'rows':[{'currency':'CNY'}]}
    profile = {'account_code':'bank_test','currency':'CNY','pams_target': {'account_code':'bank_test',
        'display_name':'Synthetic','is_asset':True,'snapshot': {'asset_liability':'资产','major_category':'现金',
        'minor_category':'现金/活期','name':'Cash CNY'}}}
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('ALTER TABLE pams_accounts ADD COLUMN is_asset boolean DEFAULT true')
        _sync_holding_snapshot_from_statement(cur, result, profile)
    changed = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    assert changed['market_value'] == 150 and changed['source_item_id'] == old['source_item_id']
    assert _current(params, old['id'])['status'] == 'unknown'


def test_gold_spot_update_records_unknown_quote_without_losing_creation_source(isolated, monkeypatch):
    _, params = isolated
    batch, item = str(uuid4()), str(uuid4())
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
            VALUES (%s,'tonghuashun','holding','synthetic','manual')""", (batch,))
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status)
            VALUES (%s,%s,'gold','manual',%s,'confirmed')""", (item, batch, 'a'*64))
        cur.execute("""INSERT INTO pams_holdings_snapshot(name,symbol,asset_liability,currency,account_code,
            major_category,minor_category,market_value,cost,as_of,quantity,current_price,source_item_id)
            VALUES ('Synthetic gold','GOLD','资产','CNY','tonghuashun','贵金属','黄金',100,90,%s,10,10,%s) RETURNING id""", (DAY,item))
        identifier = cur.fetchone()['id']
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s", ('b'*64,batch))
        finish_holding_change(cur, holding_id=identifier, source_item_id=item)
        cur.execute("""CREATE TABLE pams_asset_market_daily(as_of date,asset_id text,close_price numeric,source text,updated_at timestamptz DEFAULT now())""")
        cur.execute("INSERT INTO pams_asset_market_daily(as_of,asset_id,close_price,source) VALUES (%s,'BENCH_GOLD_SPOT_AU9999',11,'synthetic')", (DAY,))
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**params))
    assert prices._sync_gold_accum_snapshots_from_spot(date.fromisoformat(DAY), date.fromisoformat(DAY)) == 1
    row = sql(params, 'SELECT market_value,source_item_id FROM pams_holdings_snapshot')[0]
    assert row['market_value'] == 110 and str(row['source_item_id']) == item
    assert _current(params, identifier)['status'] == 'unknown'


def test_price_refresh_and_manual_correction_do_not_overwrite_each_other(isolated, monkeypatch):
    web, params = isolated
    publish_holding(web)
    stale = sql(params, 'SELECT * FROM pams_holdings_snapshot')[0]
    batch = draft(web, [holding(quantity='110', market_value='1100', cost='1000')])
    check = preview(web, batch)
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**params))
    barrier = Barrier(2)

    def manual():
        barrier.wait(timeout=5)
        return publish(web, batch, check).status_code

    def market():
        barrier.wait(timeout=5)
        try:
            prices._upsert_price_snapshots([stale], {'000001':12}, date.fromisoformat(DAY))
            return 'updated'
        except (ValueError, psycopg2.errors.SerializationFailure):
            return 'stale'

    with ThreadPoolExecutor(max_workers=2) as executor:
        user_future = executor.submit(manual)
        market_future = executor.submit(market)
        user_status, market_status = user_future.result(timeout=15), market_future.result(timeout=15)
    current = sql(params, 'SELECT quantity,market_value FROM pams_holdings_snapshot')[0]
    if user_status == 200:
        assert market_status == 'stale' and current['quantity'] == 110 and current['market_value'] == 1100
    else:
        assert user_status == 409 and market_status == 'updated'
        assert current['quantity'] == 100 and current['market_value'] == 1200
