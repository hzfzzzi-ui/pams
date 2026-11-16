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
from domain.investments.holding_evidence_writer import finish_holding_change
from domain.investments.valuation_input_evidence import current_holding_source
from domain.market.fund_nav_estimates import insert_or_update_generated_snapshot
from endpoints import prices

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


def test_price_upsert_preserves_history_and_noop_source(isolated, monkeypatch):
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
    assert prices._upsert_price_snapshots([changed], {'000001': 12}, day) == 1
    assert sql(params, 'SELECT market_value FROM pams_holdings_snapshot')[0]['market_value'] == 1200
    old_versions = sql(params, "SELECT evidence FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='holding'")
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


def test_gold_close_is_shared_and_preserves_bank_holding(isolated, monkeypatch):
    _, params = isolated
    batch, item = str(uuid4()), str(uuid4())
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method,workflow)
            VALUES (%s,'tonghuashun','holding','synthetic','manual','account_import')""", (batch,))
        from psycopg2.extras import Json
        from hashlib import sha256
        from domain.imports.unified_source import lock_source_review
        fields = {'fields': {'kind':'holding', 'business_date':DAY, 'asset_type':'gold',
                  'symbol':'GOLD', 'currency':'CNY', 'quantity':'10', 'price':'826.24',
                  'market_value':'8262.40', 'cost':'11998.30', 'cost_basis_type':'holding_cost'},
                  'excluded':False, 'issues':[], 'acknowledged':False}
        cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status,parsed_fields)
            VALUES (%s,%s,%s,'manual',%s,'confirmed',%s)""", (item, batch, item, sha256(b'{}').hexdigest(), Json(fields)))
        cur.execute("""INSERT INTO pams_holdings_snapshot(name,symbol,asset_liability,currency,account_code,
            major_category,minor_category,market_value,cost,as_of,quantity,current_price,source_item_id)
            VALUES ('Synthetic gold','GOLD','资产','CNY','tonghuashun','贵金属','黄金',8262.40,11998.30,%s,10,826.24,%s) RETURNING id""", (DAY,item))
        identifier = cur.fetchone()['id']
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch,))
        reviewed = lock_source_review(cur,batch_id=batch,expected_revision=cur.fetchone()['revision'])
        after = {'account_code':'tonghuashun', 'name':'Synthetic gold', 'symbol':'GOLD', 'currency':'CNY',
                 'as_of':DAY, 'quantity':'10', 'current_price':'826.24', 'cost':'11998.30', 'market_value':'8262.40'}
        result = {'records':[{'table':'pams_holdings_snapshot','id':identifier,'row_id':item,'after':after}]}
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_result=%s WHERE batch_id=%s",
                    (reviewed['source_snapshot_sha256'],Json(result),batch))
        finish_holding_change(cur, holding_id=identifier, source_item_id=item)
        cur.execute((ROOT / "database/postgres/schema/004_fx_rates.sql").read_text(encoding="utf-8"))
        schema = (ROOT / "database/postgres/schema/025a_asset_snapshots.sql").read_text(encoding="utf-8")
        cur.execute(schema[schema.index("CREATE TABLE IF NOT EXISTS public.market_observations"):])
        cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,
                    currency,source,confidence,raw_json)
                    VALUES ('BENCH_GOLD_SPOT_AU9999','Au99.99.SGE','Benchmark','gold',
                            'CNY','synthetic','high','{}')""")
    monkeypatch.setattr(prices, '_get_conn', lambda: psycopg2.connect(**params))
    day = date.fromisoformat(DAY)
    from domain.investments.investment_return_data import _snapshot_category_value_series
    from domain.investments.valuation_nav import _investment_snapshot_series, build_weekly_nav_rows
    from domain.market.quote_valuation import quote_valuation_cte

    history = [(day, None, None, None, 829.49)]
    assert prices._upsert_gold_spot_closes(history) == 1
    before = sql(params, "SELECT updated_at FROM market_observations")[0]
    prices._upsert_gold_spot_closes(history)
    assert sql(params, "SELECT updated_at FROM market_observations")[0] == before
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""INSERT INTO market_observations(observation_type,as_of,asset_id,close_price,source,source_key)
            VALUES ('daily',%s,'BENCH_GOLD_SPOT_AU9999',12,'akshare_sge_realtime','intraday')""", (day,))
        cur.execute(f"""WITH snapshot_view_cutoff AS (SELECT %s::date AS as_of),
            source_holdings AS (SELECT * FROM pams_holdings_snapshot WHERE id=%s),
            {quote_valuation_cte(True)} SELECT market_value,holding_pnl FROM h""", (day, identifier))
        assert cur.fetchone() == {"market_value": Decimal("8294.90"), "holding_pnl": Decimal("-3703.40")}
        args = dict(dates=[day], base_currency="CNY", owner_ratio_case_sql=lambda _: ("1", []))
        sections = _snapshot_category_value_series(
            cur, **args, major_category="贵金属", excluded_holding_accounts=set())
        assert sections[day] == Decimal("8294.90")
        # Projection must not alter scope evidence. Exercise the historical NAV
        # read with a complete scope, independently of manual import coverage.
        monkeypatch.setattr("domain.investments.snapshot_coverage.load_account_snapshot_coverage",
                            lambda cur, pairs: {pair: {"status": "complete"} for pair in pairs})
        assert _investment_snapshot_series(cur, **args, account_codes=["tonghuashun"])[day] == Decimal("8294.90")
        monkeypatch.setattr("domain.investments.snapshot_coverage.load_account_snapshot_coverage",
                            lambda cur, pairs: {pair: {"status": "partial", "member_ids": {identifier}}
                                                for pair in pairs})
        snapshot_series = _investment_snapshot_series(cur, **args, account_codes=["tonghuashun"])
        nav, = build_weekly_nav_rows(dates=[day], bank_series={}, snapshot_series=snapshot_series,
                                     a_share_series={}, ibkr_values={})
        assert nav["available_net_worth"] == Decimal("8294.90")
        assert nav["net_worth"] is None
        assert nav["estimated_accounts"] == ["tonghuashun"]
    row = sql(params, 'SELECT market_value,source_item_id FROM pams_holdings_snapshot')[0]
    assert row['market_value'] == Decimal('8262.40') and str(row['source_item_id']) == item
    assert _current(params, identifier)['status'] == 'recorded'


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
