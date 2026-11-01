"""Verify formal source-to-fact constraints against the fact schema."""
# ruff: noqa: F811
from pathlib import Path
from uuid import uuid4

from psycopg2 import errors
import pytest

from tests.database.test_source_schema_contract import isolated_database  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def test_existing_facts_allow_one_source_to_many_facts_and_reject_orphans(isolated_database):
    conn = isolated_database
    with conn.cursor() as cur:
        for name in ('023_asset_master.sql', '024_cashflow_and_trade_fact.sql',
                     '025a_asset_snapshots.sql', '075_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
        batch, item = str(uuid4()), str(uuid4())
        cur.execute("""INSERT INTO source_batches(batch_id, account_code, business_type, institution, acquisition_method)
                       VALUES (%s, 'synthetic-a', 'trade', 'synthetic', 'manual')""", (batch,))
        cur.execute("""INSERT INTO source_items(source_item_id, batch_id, source_uid, parse_method, record_hash)
                       VALUES (%s, %s, 'row-1', 'manual', %s)""", (item, batch, 'a' * 64))
        cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,amount_cny,source_table,source_id,source_hash,source_item_id)
                       VALUES ('2026-01-01','synthetic-b',10,10,'fixture','cash-1',%s,%s) RETURNING cashflow_id""", ('b' * 64, item))
        cash_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,source_table,source_id,source_hash,cashflow_id,source_item_id)
                       VALUES ('2026-01-01','synthetic-a','fixture','trade-1',%s,%s,%s) RETURNING trade_id""", ('c' * 64, cash_id, item))
        trade_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO pams_holdings_snapshot(name,asset_liability,currency,account_code,major_category,minor_category,market_value,as_of,source_item_id)
                       VALUES ('fixture','资产','CNY','synthetic-a','test','test',10,'2026-01-01',%s) RETURNING id""", (item,))
        holding_id = cur.fetchone()[0]
    conn.commit()

    for table, pk, ident in (('pams_cashflow_fact', 'cashflow_id', cash_id),
                              ('pams_investment_trade_fact', 'trade_id', trade_id),
                              ('pams_holdings_snapshot', 'id', holding_id)):
        with pytest.raises(errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(f'UPDATE {table} SET source_item_id = %s WHERE {pk} = %s', (str(uuid4()), ident))
        conn.rollback()
    with pytest.raises(errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute('DELETE FROM source_items WHERE source_item_id = %s', (item,))
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SELECT source_item_id FROM pams_cashflow_fact WHERE cashflow_id=%s', (cash_id,))
        assert str(cur.fetchone()[0]) == item
