"""Account detail exposes only proven same-day, same-currency totals."""
# ruff: noqa: F811
from decimal import Decimal
from uuid import uuid4
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_account_complete_scope import scope_report, DAY
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_account_detail_checks_complete_scope_fx_and_source(empty_source_db,monkeypatch):
    import main
    from collectors import ibkr_flex
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review,review_identity,review_account_binding,review_account_scope,publish
    report=scope_report().replace(b'currency="USD" toDate="20261022" endingCash="50"',
                                  b'currency="USD" toDate="20261022" endingCash="-50"')
    report=report.replace(b' cash="110"',b' cash="10"').replace(b'total="233"',b'total="133"')
    report=report.replace(b'cashLong="110"',b'cashLong="60"').replace(b'cashShort="0"',b'cashShort="-50"')
    report=report.replace(b'totalLong="233"',b'totalLong="183"').replace(b'totalShort="0"',b'totalShort="-50"')
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        with TestClient(main.app) as web:
            empty=web.get('/api/accounts/ibkr')
            assert empty.status_code==200 and empty.json()['total_value'] is None
            assert empty.json()['holdings_count']==0 and empty.json()['currency_values']==[]
            parsed=ibkr_flex.parse_flex_report(report.decode())
            ibkr_flex._write_holdings(conn,parsed.rows,account_code='ibkr',as_of=DAY)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','USD',%s,7,'synthetic'),('CNY','EUR',%s,14,'synthetic')",(DAY,DAY))
            conn.commit()
            unproven=web.get('/api/accounts/ibkr').json()
            assert unproven['total_value'] is None and unproven['total_value_status']=='unknown'
            values={row['currency']:Decimal(row['amount']) for row in unproven['currency_values']}
            assert values=={'USD':Decimal('73'),'EUR':Decimal('30')}
            assert all(row['source_evidence']=='unknown' for row in unproven['currency_values'])
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report)
                cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
                for item in source['position_item_ids']:
                    cur.execute('SELECT raw_fields FROM source_items WHERE source_item_id=%s',(item,)); raw=cur.fetchone()['raw_fields']
                    cur.execute('SELECT id FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s AND symbol=%s',('ibkr',DAY,raw['symbol']))
                    hid=cur.fetchone()['id']
                    rev=review(cur,batch_id=source['batch_id'],item_id=item,expected_revision=rev,note='row')['revision']
                    rev=review_identity(cur,batch_id=source['batch_id'],item_id=item,holding_id=hid,expected_revision=rev,note='contract')['revision']
                rev=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=rev,note='positions',scope_complete=True)['revision']
                rev=review_account_binding(cur,batch_id=source['batch_id'],source_account_id='U_TEST',expected_revision=rev,note='account')['revision']
                rev=review_account_scope(cur,batch_id=source['batch_id'],expected_revision=rev,note='complete scope',confirmed=True)['revision']
                publish(cur,batch_id=source['batch_id'],expected_revision=rev)
            conn.commit()
            detail=web.get('/api/accounts/ibkr').json()
            assert detail['base_currency']=='USD' and detail['total_value']=='133.00' and detail['total_value_status']=='complete',detail
            assert all(row['source_evidence']=='recorded' for row in detail['currency_values'])
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='EUR' AND as_of=%s",(DAY,))
            conn.commit()
            missing=web.get('/api/accounts/ibkr').json()
            assert missing['total_value'] is None and missing['total_value_status']=='unknown'
            assert {row['currency']:Decimal(row['amount']) for row in missing['currency_values']}==values
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='EUR' AND as_of=%s",(DAY,))
                cur.execute('SELECT content FROM source_files WHERE batch_id=%s',(source['batch_id'],))
                original=bytes(cur.fetchone()[0])
                cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
                cur.execute('UPDATE source_files SET content=%s WHERE batch_id=%s',(b'tampered',source['batch_id']))
                cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
            conn.commit()
            tampered=web.get('/api/accounts/ibkr').json()
            assert tampered['total_value'] is None and tampered['total_value_status']=='unknown'
            assert all(row['source_evidence']=='unknown' for row in tampered['currency_values'])
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
                cur.execute('UPDATE source_files SET content=%s WHERE batch_id=%s',(original,source['batch_id']))
                cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
                cur.execute('DROP TABLE pams_fx_rates_daily')
            conn.commit()
            no_table=web.get('/api/accounts/ibkr')
            assert no_table.status_code==200,no_table.text
            assert no_table.json()['total_value'] is None and no_table.json()['total_value_status']=='unknown'


def test_complete_explicit_zero_is_distinct_from_empty_and_needs_no_fx(empty_source_db,monkeypatch):
    import re
    import main
    from collectors import ibkr_flex
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review,review_account_binding,review_account_scope,publish
    report=scope_report()
    report=re.sub(rb'<OpenPosition\b[^>]*/>',b'',report)
    for old,new in [(b' stock="123"',b' stock="0"'),(b'stockLong="123"',b'stockLong="0"'),
                    (b' cash="110"',b' cash="0"'),(b'cashLong="110"',b'cashLong="0"'),
                    (b'total="233"',b'total="0"'),(b'totalLong="233"',b'totalLong="0"'),
                    (b'endingCash="50"',b'endingCash="0"'),(b'endingCash="30"',b'endingCash="0"')]:
        report=report.replace(old,new)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        parsed=ibkr_flex.parse_flex_report(report.decode())
        assert len(parsed.rows)==2 and all(row['market_value']==0 for row in parsed.rows)
        ibkr_flex._write_holdings(conn,parsed.rows,account_code='ibkr',as_of=DAY)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report)
            assert source['position_item_ids']==[]
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=rev,note='empty positions',scope_complete=True)['revision']
            rev=review_account_binding(cur,batch_id=source['batch_id'],source_account_id='U_TEST',expected_revision=rev,note='account')['revision']
            rev=review_account_scope(cur,batch_id=source['batch_id'],expected_revision=rev,note='explicit zero cash',confirmed=True)['revision']
            publish(cur,batch_id=source['batch_id'],expected_revision=rev)
        conn.commit()
        from domain.investments.valuation_nav import _investment_snapshot_series
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            historical=_investment_snapshot_series(cur,account_codes=['ibkr'],dates=[DAY],base_currency='CNY',
                owner_ratio_case_sql=lambda alias:('1',[]))
            assert historical[DAY]==Decimal(0) and not historical.unknown_accounts_by_date
        conn.rollback()
        with TestClient(main.app) as web:
            detail=web.get('/api/accounts/ibkr').json()
            assert detail['total_value']=='0.00' and detail['total_value_status']=='complete',detail
            assert {v['currency']:Decimal(v['amount']) for v in detail['currency_values']}=={'USD':Decimal(0),'EUR':Decimal(0)}
