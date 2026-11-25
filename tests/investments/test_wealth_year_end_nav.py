"""Reviewed wealth marks augment a partial NAV once on their evidenced day."""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from domain.imports.sources import source_records_v2 as sources

from domain.imports.publication import trade_supplemental_evidence as supplement
from domain.investments.holdings.position_nav_projection import load_position_nav_estimates


def test_reviewed_wealth_anchor_is_dated_and_not_double_counted():
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('wealth_test','Test')")
                cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency) VALUES ('wealth_asset','Synthetic wealth','WTEST','wealth_management','CNY')")
                trades = []
                for typ, day, cash in [('buy','2025-12-12','-100'),('sell','2026-03-24','110')]:
                    src, item = uuid4(), uuid4()
                    fields = {'business_date':day,'trade_type':typ,'currency':'CNY','asset_id':'wealth_asset','symbol':'WTEST','net_cash':cash}
                    sources.create_source(cur, source_id=src, account_code='wealth_test', source_name='trade', acquisition_method='manual', document_type='trade')
                    sources.add_item(cur,source_id=src,item_id=item,parse_method='manual',raw_fields={'entered_fields':fields},parsed_fields=fields)
                    sources.review_item(cur,source_id=src,item_id=item,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                    cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s",(str(src),))
                    cur.execute("""INSERT INTO pams_investment_trade_fact(account_code,trade_date,trade_type,side,currency,asset_id,symbol,net_cash,source_item_id,fact_role)
                        VALUES ('wealth_test',%s,%s,%s,'CNY','wealth_asset','WTEST',%s,%s,'primary') RETURNING trade_id""",(day,typ,typ,cash,str(item)))
                    trades.append((cur.fetchone()['trade_id'],fields))
                buy, sell = trades
                extra_buy = {**buy[1],'quantity':'100','valuation_date':'2025-12-31','valuation_unit_price':'1.05',
                    'valuation_observations':[{'date':'2025-12-31','cumulative_nav':'1.05'},
                                              {'date':'2026-01-05','cumulative_nav':'1.07'},
                                              {'date':'2026-03-20','cumulative_nav':'1.1'}]}
                extra_sell = {**sell[1],'quantity':'100','execution_date':'2026-03-20','unit_nav':'1.1',
                    'complete_exit':True,'complete_history':True,'complete_trade_ids':[buy[0],sell[0]]}
                confirmations = []
                for ident, fields in [(buy[0],extra_buy),(sell[0],extra_sell)]:
                    src,item = uuid4(),uuid4()
                    sources.create_source(cur,source_id=src,account_code='wealth_test',source_name='reviewed',acquisition_method='manual',document_type='trade')
                    sources.add_item(cur,source_id=src,item_id=item,parse_method='manual',raw_fields={'entered_fields':fields},parsed_fields=fields)
                    sources.review_item(cur,source_id=src,item_id=item,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                    confirmation = supplement.attach(cur,source_id=src,item_id=item,trade_id=ident,expected_revision=3,note='Synthetic review',confirmed=True)
                    confirmations.append(confirmation['confirmation_id'])
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,currency,asset_liability,major_category,minor_category,market_value)
                    VALUES ('wealth_test','2025-12-31','OTHER','Other','CNY','资产','其他','其他',5)""")
                args = dict(accounts={'wealth_test':{}},dates=[date(2025,12,31),date(2026,1,1),date(2026,1,5),
                                                              date(2026,1,6),date(2026,3,20)],
                    owner_ratio_case_sql=lambda _:('1::numeric',[]),account_owner_ratio=lambda _:Decimal('1'),rate_at=lambda *_:Decimal('1'))
                values = load_position_nav_estimates(cur,**args)
                assert values[date(2025,12,31)]['wealth_test'][0] == Decimal('110')
                assert values[date(2026,1,1)]['wealth_test'] == (Decimal('110'),date(2025,12,31),date(2025,12,31))
                assert values[date(2026,1,5)]['wealth_test'][0] == Decimal('112')
                assert values[date(2026,1,6)]['wealth_test'][0] == Decimal('5')
                assert values[date(2026,3,20)]['wealth_test'][0] == Decimal('5')
                cur.execute('SAVEPOINT revoke_proof')
                supplement.revoke(cur,confirmation_id=confirmations[0],note='Synthetic revocation',confirmed=True)
                assert load_position_nav_estimates(cur,**args)[date(2025,12,31)]['wealth_test'][0] == Decimal('5')
                cur.execute('ROLLBACK TO SAVEPOINT revoke_proof')
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,asset_id,currency,asset_liability,major_category,minor_category,market_value)
                    VALUES ('wealth_test','2025-12-31','WTEST','Synthetic wealth','wealth_asset','CNY','资产','其他','其他',105)""")
                assert load_position_nav_estimates(cur,**args)[date(2025,12,31)]['wealth_test'][0] == Decimal('110')
        finally:
            conn.rollback()
