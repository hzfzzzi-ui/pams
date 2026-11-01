"""Three fact tables share one source-association completeness check."""
# ruff: noqa: F811
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client  # noqa: F401
from domain.imports.fact_associations import check_all_source_links

ROOT = Path(__file__).resolve().parents[2]


def test_complete_missing_and_extra_associations_in_all_fact_types(client):
    _, params = client
    batch, cash_item, trade_item, holding_item = [str(uuid4()) for _ in range(4)]
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method) VALUES (%s,'tonghuashun','cash','synthetic','manual')", (batch,))
        for index, item in enumerate((cash_item, trade_item, holding_item)):
            cur.execute("INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash) VALUES (%s,%s,%s,'manual',%s)", (item,batch,str(index),str(index)*64))
        cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,amount_cny,source_table,source_id,source_hash,source_item_id)
            VALUES ('2026-10-22','tonghuashun',1,1,'test','cash',%s,%s) RETURNING cashflow_id""", ('a'*64,cash_item))
        cash_id = cur.fetchone()['cashflow_id']
        cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,source_table,source_id,source_hash,source_item_id)
            VALUES ('2026-10-22','tonghuashun','test','trade',%s,%s) RETURNING trade_id""", ('b'*64,trade_item))
        trade_id = cur.fetchone()['trade_id']
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,as_of,source_item_id)
            VALUES ('tonghuashun','synthetic','2026-10-22',%s) RETURNING id""", (holding_item,))
        holding_id = cur.fetchone()['id']
        records = [{'table':'pams_cashflow_fact','id':cash_id,'row_id':cash_item},
                   {'table':'pams_investment_trade_fact','id':trade_id,'row_id':trade_item},
                   {'table':'pams_holdings_snapshot','id':holding_id,'row_id':holding_item}]
        check_all_source_links(cur, batch, records)
        for removed in records:
            with pytest.raises(ValueError, match='unexpected or missing'):
                check_all_source_links(cur, batch, [item for item in records if item is not removed])
        cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,amount_cny,source_table,source_id,source_hash,source_item_id)
            VALUES ('2026-10-22','tonghuashun',2,2,'test','extra',%s,%s)""", ('c'*64,cash_item))
        with pytest.raises(ValueError, match='unexpected or missing'):
            check_all_source_links(cur, batch, records)
