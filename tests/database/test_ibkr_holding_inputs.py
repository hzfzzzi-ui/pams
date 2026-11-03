"""Isolated report positions must not manufacture missing holding facts."""
# ruff: noqa: F811
from datetime import date
import re

import psycopg2
import psycopg2.extras
import pytest
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from collectors.ibkr_flex import parse_flex_report, _validate_isolated_holding_inputs
from domain.investments.holding_write_evidence import reject_identity_conflicts

DAY=date(2026,10,22)


@pytest.mark.parametrize('old,new',[
    ('currency="USD"',''),
    ('position="2"',''),
    ('reportDate="20261022"',''),
    ('marketValue="381" positionValue="381"',''),
    ('position="2"','position="2.1234567"'),
    ('marketValue="381" positionValue="381"','marketValue="381.00001" positionValue="381.00001"'),
    ('markPrice="190.5"','markPrice="190.5000001"'),
    ('position="2"','position="2" multiplier="0"'),
])
def test_missing_or_unrepresentable_position_fields_are_rejected(old,new):
    position=re.search(r'<OpenPosition\b[^>]*/>',XML).group()
    text=XML.replace(position,position.replace(old,new))
    parsed=parse_flex_report(text,account_code='ibkr')
    with pytest.raises(ValueError):
        _validate_isolated_holding_inputs(parsed,DAY,'ibkr',text)


def test_missing_cost_remains_unknown_but_known_quantity_and_value_survive():
    position=re.search(r'<OpenPosition\b[^>]*/>',XML).group()
    text=XML.replace(position,position.replace('costBasisMoney="340"',''))
    parsed=parse_flex_report(text,account_code='ibkr')
    _validate_isolated_holding_inputs(parsed,DAY,'ibkr',text)
    assert next(row for row in parsed.rows if row['major_category']=='美股')['cost'] is None


def test_missing_prices_and_multiplier_do_not_invent_unit_prices():
    position=re.search(r'<OpenPosition\b[^>]*/>',XML).group()
    text=XML.replace(position,position.replace('markPrice="190.5"','').replace('costBasisPrice="170"',''))
    parsed=parse_flex_report(text,account_code='ibkr')
    _validate_isolated_holding_inputs(parsed,DAY,'ibkr',text)
    holding=next(row for row in parsed.rows if row['major_category']=='美股')
    assert holding['quantity']==2 and holding['market_value']==381 and holding['cost']==340
    assert holding['current_price'] is None and holding['buy_price'] is None


def test_cross_day_same_name_different_symbol_or_currency_is_blocked(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value)
                VALUES ('ibkr','Holding','FIRST','USD',%s,%s,'stock','2026-10-21',1,10,10)""",('资产','美股'))
        with pytest.raises(ValueError,match='identity or currency'):
            reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,
                incoming=[{'name':'Holding','symbol':'SECOND','currency':'USD'}])
        with pytest.raises(ValueError,match='identity or currency'):
            reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,
                incoming=[{'name':'Holding','symbol':'FIRST','currency':'EUR'}])


def test_report_keeps_option_and_unknown_classification():
    options=('''<OpenPosition accountId="U_TEST" symbol="OPT_TEST" description="Synthetic option"
             currency="USD" reportDate="20261022" position="1" markPrice="15" marketValue="15" positionValue="15"
             costBasisMoney="10" costBasisPrice="10" multiplier="1" assetCategory="OPT" />
             <OpenPosition accountId="U_TEST" symbol="UNKNOWN_TEST" description="Unknown type"
             currency="USD" reportDate="20261022" position="1" markPrice="20" marketValue="20" positionValue="20"
             costBasisMoney="12" costBasisPrice="12" multiplier="1" />''')
    text = XML.replace('<CashReportCurrency', options + '<CashReportCurrency', 1)
    parsed = parse_flex_report(text, account_code='ibkr')
    _validate_isolated_holding_inputs(parsed, DAY, 'ibkr', text)
    rows = {row['symbol']: row for row in parsed.rows}
    option, unknown = rows['OPT_TEST'], rows['UNKNOWN_TEST']
    assert (option['major_category'], option['minor_category'], option['market_value']) == ('其他', '期权', 15)
    assert (unknown['major_category'], unknown['minor_category'], unknown['market_value']) == ('其他', '待分类', 20)
    assert (option['quantity'], option['current_price'], option['cost']) == (1, 15, 10)
    assert (unknown['quantity'], unknown['current_price'], unknown['cost']) == (1, 20, 12)


def test_reviewed_option_conid_cannot_be_replaced_by_same_symbol_other_contract(empty_source_db):
    from uuid import UUID
    from domain.brokers.ibkr.ibkr_holding_identity import review_holding_identity
    from domain.imports.unified_source import lock_source_review
    from tests.database.test_ibkr_holding_identity_review import prepare, XML as IDENTITY_XML
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            original=IDENTITY_XML.replace(b'conid="1001"',b'conid="1001" assetCategory="OPT"')
            holding,source,rev=prepare(cur,content=original)
            cur.execute("UPDATE pams_holdings_snapshot SET major_category='其他',minor_category='期权' WHERE id=%s",(holding,))
            rev=review_holding_identity(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],
                                        holding_id=holding,expected_revision=rev,review_note='synthetic option identity')
            snapshot=lock_source_review(cur,batch_id=UUID(source['batch_id']),expected_revision=rev)
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                        (snapshot['source_snapshot_sha256'],source['batch_id']))
        conn.commit()
        current={'name':'Test','symbol':'TEST','currency':'USD','major_category':'其他',
                 'minor_category':'期权','ibkr_conid':'1001','ibkr_report_account':'U_TEST'}
        reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,incoming=[current])
        with pytest.raises(ValueError,match='contract conflicts'):
            reject_identity_conflicts(conn,account_code='ibkr',business_date=DAY,
                incoming=[{**current,'ibkr_conid':'9999'}])
        conn.rollback()
