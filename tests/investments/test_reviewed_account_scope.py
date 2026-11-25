from datetime import date
from decimal import Decimal
import os
from uuid import uuid4

import pytest

from domain.investments.valuation import account_total_evidence as totals

from domain.investments.valuation import reviewed_account_scope as scopes
from domain.investments.valuation.valuation_nav import _apply_account_totals


def test_attested_partial_members_are_dated_and_replayable():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import isolation, source_records_v2 as sources
    from domain.investments.holdings.account_snapshot_evidence import record_source_scope
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    day = date(2025,12,31)
    code = 'synthetic_equity'
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_enabled(cur)
                cur.execute('INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,%s)',(code,'demo'))
                source, item = uuid4(), uuid4()
                sources.create_source(cur,source_id=source,account_code=code,source_name='example',
                                      acquisition_method='manual',document_type='holding')
                fields = dict(business_date=str(day),symbol='HELD',currency='CNY',quantity='5',price='10',market_value='50')
                sources.add_manual_item(cur,source_id=source,item_id=item,expected_revision=1,parsed_fields=fields)
                sources.review_item(cur,source_id=source,item_id=item,expected_revision=2,parsed_fields=fields,
                                    review_status='confirmed',note='confirmed original')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s",(str(source),))
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,asset_liability,
                    currency,market_value,quantity,current_price,major_category,minor_category,source_item_id)
                    VALUES (%s,%s,'HELD','stock','资产','CNY',50,5,10,'A股','个股',%s) RETURNING id""",
                    (code,day,str(item)))
                holding = cur.fetchone()['id']
                record_source_scope(cur,source_id=source,holding_ids=[holding],as_of=day,scope=None)
                members = scopes.verified_members(cur,code,day,'CNY','dated_positions')
                args = dict(account=code,day=day,currency='CNY',basis='dated_positions',members=members,
                            net_asset='50',note='Confirmed sole position, zero cash, no other assets or debts')
                assert scopes.record(cur,**args) == scopes.record(cur,**args)
                assert set(totals.load(cur,dates=[day,date(2026,1,1)],accounts={code})) == {(day,code)}
                with pytest.raises(ValueError):
                    scopes.record(cur,**{**args,'net_asset':'51'})
                cur.execute('SELECT count(*) n FROM pams_investment_trade_fact WHERE account_code=%s',(code,))
                assert cur.fetchone()['n']==0
                cur.execute('UPDATE pams_holdings_snapshot SET quantity=6 WHERE id=%s',(holding,))
                assert not totals.load(cur,dates=[day],accounts={code})
        finally:
            conn.rollback()


def test_confirmed_components_need_a_matching_personal_contribution():
    total = {'account_code':'pool','currency':'CNY','net_asset':'160','covered_account_codes':['pool'],
             'confirmed_components':'verified source hash'}
    row = {'date':date(2025,12,31),'available_net_worth':Decimal(80),'net_worth':None,
           'available_accounts':['pool'],'unavailable_accounts':['pool'],'_account_contributions':{'pool':Decimal(80)}}
    for owner in (None, Decimal('.75')):
        _apply_account_totals(row,[total],lambda _:owner,lambda *_:Decimal(1))
        assert row['unavailable_accounts'] == ['pool']
    for _ in range(2):
        _apply_account_totals(row,[total],lambda _:Decimal('.5'),lambda *_:Decimal(1))
    assert row['unavailable_accounts'] == [] and row['available_net_worth'] == Decimal(80)
