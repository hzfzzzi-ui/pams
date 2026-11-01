"""Isolated verification of facts against their stored IBKR report."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import re
from xml.etree import ElementTree as ET
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_report_batch import report, execute
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, EvidenceUnavailable, _report_node, verify_ibkr_fact
from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch
from domain.brokers.ibkr.ibkr_cash_events import import_cash_events
from collectors.ibkr_flex import parse_flex_report

DAY=date(2026,10,22)

def original():
    return report().replace(b'tradePrice="170"',b'tradePrice="170" multiplier="1" fifoPnlRealized="0"')

def prepare(conn,content=None):
    content=content or original()
    initialize_isolated_sources(conn)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('other','Synthetic')")
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','USD',%s,7,'synthetic')",(DAY,))
        execute(cur,content)
        cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade' ORDER BY trade_id")
        ids=[row['trade_id'] for row in cur.fetchall()]
    conn.commit()
    return ids


def test_verify_stored_report_without_caller_bytes(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=verify_ibkr_fact(cur,trade_id=ids[0])
            assert result['status']=='partial' and result['unknown_fields']==['settle_date']
            assert result['report_sha256']==sha256(original()).hexdigest()
            assert not result['source_authenticated'] and not result['authorizes_publication']
            assert verify_ibkr_fact(cur,trade_id=ids[0],report_bytes=b'forged')['status']=='unavailable'
        with conn.cursor() as cur:
            assert verify_ibkr_fact(cur,trade_id=ids[0])['status']=='partial'


@pytest.mark.parametrize('column,value',[
    ('gross_amount',341),('net_cash',-1),('currency','EUR'),('account_code','other'),
    ('trade_date',date(2026,10,21)),('side','sell'),('quantity',-2),('price',171),
    ('fee_amount',2),('fee_currency','EUR'),('tax_amount',1),('symbol','OTHER'),
    ('realized_pnl',1),('market','OPT'),('name','OTHER'),
])
def test_changed_fact_does_not_verify(empty_source_db,column,value):
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f'UPDATE pams_investment_trade_fact SET {column}=%s WHERE trade_id=%s',(value,ids[0]))
            assert verify_ibkr_fact(cur,trade_id=ids[0])['status']=='unavailable'
        conn.rollback()


def test_unlinked_fact_without_original_is_not_verified(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,currency,source_table,source_id,source_key,source_hash)
                VALUES (%s,'ibkr','USD','ibkr_flex_trade','unlinked','unlinked',%s) RETURNING trade_id""",
                (DAY,'a'*64))
            result=verify_ibkr_fact(cur,trade_id=cur.fetchone()['trade_id'])
            assert result['status']=='unavailable' and result['reason']=='missing_fact_source'
        conn.rollback()


@pytest.mark.parametrize('target',['file_bytes','item_raw','item_note','batch_result'])
def test_corrupted_stored_evidence_is_not_verified(empty_source_db,target):
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if target=='file_bytes':
                cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
                cur.execute("UPDATE source_files SET content=decode('00','hex')")
                cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
            elif target in {'item_raw','item_note'}:
                cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
                if target=='item_raw':
                    cur.execute("UPDATE source_items SET raw_fields=jsonb_set(raw_fields,'{source_account_id}',to_jsonb(%s::text)) WHERE source_uid<>'scope'",('OTHER',))
                else:
                    cur.execute("UPDATE source_items SET review_note='forged' WHERE source_uid<>'scope'")
                cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            else:
                cur.execute('ALTER TABLE source_batches DISABLE TRIGGER USER')
                cur.execute("UPDATE source_batches SET published_result=jsonb_set(published_result,'{records}','[]'::jsonb)")
                cur.execute('ALTER TABLE source_batches ENABLE TRIGGER USER')
            assert verify_ibkr_fact(cur,trade_id=ids[0])['status']=='unavailable'


def test_duplicate_official_identifier_cannot_be_verified():
    payload=original(); trade=re.search(rb'<Trade\b[^>]*/>',payload).group()
    duplicated=payload.replace(trade,trade+trade)
    attrs=dict(ET.fromstring(trade).attrib)
    raw={'official_attributes':attrs,'source_account_id':'U_TEST','report_evidence':{
        'broker_source':'ibkr_flex','source_sha256':sha256(duplicated).hexdigest(),
        'confirmed_day':DAY.isoformat(),'evidence_role':'source_report'}}
    with pytest.raises(EvidenceUnavailable,match='source_identity_not_unique'):
        _report_node(raw,duplicated,'ibkr_flex_trade',None)


def test_cross_report_unlinked_observation_does_not_override_origin(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            second=original()+b'\n'
            assert execute(cur,second)['existing_verified']==1
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=verify_ibkr_fact(cur,trade_id=ids[0])
            assert result['report_sha256']==sha256(original()).hexdigest()
            assert verify_ibkr_fact(cur,trade_id=ids[0],report_bytes=second)['reason']=='report_bytes_mismatch'


def test_missing_tax_is_unknown_in_the_actual_report_batch(empty_source_db):
    payload=original().replace(b' taxAmount="0"',b'')
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn,payload)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=verify_ibkr_fact(cur,trade_id=ids[0])
            assert result['status']=='partial'
            assert {'tax_amount','net_reconciliation'}.issubset(set(result['unknown_fields']))


def test_readonly_repeatable_read_cache_and_concurrent_changes(empty_source_db):
    trade=re.search(rb'<Trade\b[^>]*/>',original()).group()
    content=original().replace(trade,trade+trade.replace(b'tradeID="T1"',b'tradeID="T2"'))
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn,content)
    reader=psycopg2.connect(**empty_source_db); writer=psycopg2.connect(**empty_source_db); cache=EvidenceCache()
    try:
        reader.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with reader.cursor(cursor_factory=RealDictCursor) as cur:
            first=verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)
            assert first['status']=='partial'
            assert verify_ibkr_fact(cur,trade_id=ids[1],cache=cache)['status']=='partial'
            identity=cache.identity
            assert len(cache.batches)==1 and len(cache.files)==1 and len(cache.documents)==1 and len(cache.sources)==2
            with writer.cursor() as other:
                other.execute('UPDATE pams_investment_trade_fact SET gross_amount=341 WHERE trade_id=%s',(ids[0],))
            writer.commit()
            assert verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)==first and cache.identity==identity
        reader.rollback()
        with reader.cursor() as cur:
            assert verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)['status']=='unavailable'
            assert cache.identity!=identity
            second_identity=cache.identity
        reader.commit()
        with reader.cursor() as cur:
            assert verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)['status']=='unavailable'
            assert cache.identity!=second_identity
        cache.clear()
        assert not cache.sources and not cache.batches and not cache.files and not cache.documents
    finally:
        reader.close(); writer.close()


def test_readonly_read_committed_is_unknown(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn)
    conn=psycopg2.connect(**empty_source_db)
    try:
        conn.set_session(readonly=True,isolation_level='READ COMMITTED')
        with conn.cursor() as cur:
            result=verify_ibkr_fact(cur,trade_id=ids[0],cache=EvidenceCache())
            assert result['status']=='unavailable' and result['reason']=='read_only_requires_repeatable_read'
    finally:
        conn.close()


def test_writable_cache_rechecks_source_after_savepoint_and_holds_account_lock(empty_source_db):
    conn=psycopg2.connect(**empty_source_db); other=None; cache=EvidenceCache()
    try:
        ids=prepare(conn)
        other=psycopg2.connect(**empty_source_db)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            first=verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)
            assert first['status']=='partial' and not cache.sources and len(cache.documents)==1
            with other.cursor() as probe:
                probe.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
                assert probe.fetchone()[0] is False
            other.rollback()
            cur.execute('SAVEPOINT tamper')
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
            cur.execute("UPDATE source_items SET review_note='forged' WHERE source_uid<>'scope'")
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            assert verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)['status']=='unavailable'
            cur.execute('ROLLBACK TO SAVEPOINT tamper')
            assert verify_ibkr_fact(cur,trade_id=ids[0],cache=cache)==first
        conn.rollback()
        with other.cursor() as probe:
            probe.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
            assert probe.fetchone()[0] is True
    finally:
        conn.close()
        if other is not None: other.close()


def test_recovery_disables_cache_without_allocating_xid():
    class NoExecute:
        def execute(self,*args):
            raise AssertionError('standby must not allocate an xid')
    cache=EvidenceCache(); cache.sources['old']='stale'; cache.documents['old']='stale'
    assert not cache.bind(NoExecute(),True) and not cache.sources and not cache.documents


def cash_report(event_day=DAY):
    cash=('<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" '
          f'currency="EUR" dateTime="{event_day:%Y%m%d};120000" amount="10" type="Dividends" />'
          '</CashTransactions>').encode()
    return original().replace(b'</FlexStatement>',cash+b'</FlexStatement>')


def prepare_cash(conn,*,supplement,event_day=DAY):
    initialize_isolated_sources(conn)
    payload=cash_report(event_day)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','USD',%s,7,'synthetic')",(DAY,))
        if not supplement:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','EUR',%s,8,'synthetic')",(event_day,))
    conn.commit()
    parsed=parse_flex_report(payload.decode(),account_code='ibkr')
    envelope=parsed.cash_events[0]
    envelope['source_evidence']={'broker_source':'ibkr_flex','source_sha256':sha256(payload).hexdigest(),
                                 'confirmed_day':DAY.isoformat()}
    if supplement:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            publish_report_batch(cur,report_bytes=payload,source_account_id='U_TEST',day=DAY,
                                 report_sha256=sha256(payload).hexdigest(),cash_entries=[],cash_pending_count=1)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source) VALUES ('CNY','EUR',%s,8,'synthetic')",(event_day,))
    result=import_cash_events(conn,[envelope],account_code='ibkr',cutoff=date(2026,1,1),statement_as_of=DAY,
                              defer_source_link=True)
    assert result['inserted_count']==1
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        publish_report_batch(cur,report_bytes=payload,source_account_id='U_TEST',day=DAY,
                             report_sha256=sha256(payload).hexdigest(),cash_entries=result['_deferred_source_events'])
        cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_cash_transaction'")
        identifier=cur.fetchone()['trade_id']
    conn.commit()
    return identifier,payload


@pytest.mark.parametrize('supplement',[False,True],ids=['direct_cash','supplement_cash'])
def test_cash_sources_verify_original_fields_and_event_day_fx(empty_source_db,supplement):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,payload=prepare_cash(conn,supplement=supplement)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=verify_ibkr_fact(cur,trade_id=identifier)
            assert result['status']=='partial',result
            assert result['report_sha256']==sha256(payload).hexdigest()
            assert 'fx_rate_to_cny' not in result['unknown_fields']
            assert 'fx_rate_to_cny' not in result['not_checked']
            assert not result['source_authenticated'] and not result['authorizes_publication']
            cur.execute("UPDATE pams_fx_rates_daily SET rate=9 WHERE base_currency='CNY' AND quote_currency='EUR' AND as_of=%s",(DAY,))
            assert verify_ibkr_fact(cur,trade_id=identifier)['status']=='unavailable'
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pams_fx_rates_daily WHERE base_currency='CNY' AND quote_currency='EUR' AND as_of=%s",(DAY,))
            result=verify_ibkr_fact(cur,trade_id=identifier)
            assert result['status']=='partial' and 'fx_rate_to_cny' in result['unknown_fields']


@pytest.mark.parametrize('column,value',[
    ('gross_amount',11),('net_cash',-10),('currency','USD'),('trade_date',date(2026,10,21)),
    ('side','withdraw'),('net_cash_cny',81),('fx_rate_to_cny',9),('realized_pnl_cny',81),
])
def test_supplement_fact_changes_are_not_verified(empty_source_db,column,value):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,_=prepare_cash(conn,supplement=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f'UPDATE pams_investment_trade_fact SET {column}=%s WHERE trade_id=%s',(value,identifier))
            assert verify_ibkr_fact(cur,trade_id=identifier)['status']=='unavailable'


def test_supplement_origin_and_ordinal_must_match(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,payload=prepare_cash(conn,supplement=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT i.raw_fields FROM source_items i JOIN pams_investment_trade_fact f ON f.source_item_id=i.source_item_id WHERE f.trade_id=%s',(identifier,))
            stored=cur.fetchone()['raw_fields']
            normalized={'official_attributes':stored['official_attributes'],'source_account_id':'U_TEST','cash_ordinal':2,
                'reported_cash_count':1,'report_evidence':{'broker_source':'ibkr_flex','source_sha256':sha256(payload).hexdigest(),
                'confirmed_day':DAY.isoformat()}}
            with pytest.raises(EvidenceUnavailable,match='cash_original_ordinal_mismatch'):
                _report_node(normalized,payload,'ibkr_flex_cash_transaction',None)
            cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
            cur.execute("UPDATE source_items SET raw_fields=jsonb_set(raw_fields,'{original_item_id}',to_jsonb(%s::text)) WHERE source_item_id=(SELECT source_item_id FROM pams_investment_trade_fact WHERE trade_id=%s)",('00000000-0000-0000-0000-000000000000',identifier))
            cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            assert verify_ibkr_fact(cur,trade_id=identifier)['status']=='unavailable'


@pytest.mark.parametrize('target',['original_file','original_scope','supplement_review','origin_file_reference'])
def test_supplement_and_original_frozen_evidence_tampering_is_unknown(empty_source_db,target):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,_=prepare_cash(conn,supplement=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if target=='original_file':
                cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
                cur.execute("UPDATE source_files SET content=decode('00','hex')")
                cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
            else:
                cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
                if target=='original_scope':
                    cur.execute("UPDATE source_items SET review_note='forged' WHERE source_uid='scope'")
                elif target=='supplement_review':
                    cur.execute("UPDATE source_items SET parsed_fields=jsonb_set(parsed_fields,'{dated_fx_rate}',to_jsonb(%s::text)) WHERE source_item_id=(SELECT source_item_id FROM pams_investment_trade_fact WHERE trade_id=%s)",('9',identifier))
                else:
                    cur.execute("UPDATE source_items SET origin_file_id=NULL WHERE source_item_id=(SELECT source_item_id FROM pams_investment_trade_fact WHERE trade_id=%s)",(identifier,))
                cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
            assert verify_ibkr_fact(cur,trade_id=identifier)['status']=='unavailable'


def test_cash_supplement_readonly_repeatable_read(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,_=prepare_cash(conn,supplement=True)
    reader=psycopg2.connect(**empty_source_db); writer=psycopg2.connect(**empty_source_db); cache=EvidenceCache()
    try:
        reader.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with reader.cursor(cursor_factory=RealDictCursor) as cur:
            first=verify_ibkr_fact(cur,trade_id=identifier,cache=cache)
            assert first['status']=='partial' and len(cache.batches)==2 and len(cache.files)==1
            with writer.cursor() as other:
                other.execute('UPDATE pams_investment_trade_fact SET net_cash_cny=81 WHERE trade_id=%s',(identifier,))
            writer.commit()
            assert verify_ibkr_fact(cur,trade_id=identifier,cache=cache)==first
        reader.rollback()
        with reader.cursor() as cur:
            assert verify_ibkr_fact(cur,trade_id=identifier,cache=cache)['status']=='unavailable'
    finally:
        reader.close(); writer.close()


def test_delayed_fx_for_prior_day_cash_uses_event_date_not_report_day(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        identifier,_=prepare_cash(conn,supplement=True,event_day=DAY-timedelta(days=1))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=verify_ibkr_fact(cur,trade_id=identifier)
            assert result['status']=='partial',result
            assert 'fx_rate_to_cny' not in result['unknown_fields']



def test_explicit_positive_tax_is_preserved_from_report_and_net_must_match(empty_source_db):
    payload=original().replace(b'taxAmount="0"',b'taxAmount="0.5" taxCurrency="USD"').replace(b'netCash="-341.25"',b'netCash="-341.75"')
    with psycopg2.connect(**empty_source_db) as conn:
        ids=prepare(conn,payload)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT tax_amount,net_cash FROM pams_investment_trade_fact WHERE trade_id=%s',(ids[0],))
            fact=cur.fetchone()
            assert fact['tax_amount']==Decimal('0.5') and fact['net_cash']==Decimal('-341.75')
            result=verify_ibkr_fact(cur,trade_id=ids[0])
            assert result['status'] in {'partial','consistent'} and 'tax_amount' not in result['unknown_fields']
            invalid=payload.replace(b'netCash="-341.75"',b'netCash="-341.25"')
            with pytest.raises(ValueError,match='amounts do not reconcile'):
                execute(cur,invalid)
