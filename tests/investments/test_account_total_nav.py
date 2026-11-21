from datetime import date
from decimal import Decimal
from uuid import uuid4
import os
import pytest

from domain.investments.valuation_nav import _apply_account_totals

D = Decimal


def test_total_replaces_covered_components_once_and_preserves_other_accounts():
    day=date(2025,12,31)
    row={'date':day,'available_net_worth':D('210'),'net_worth':None,
         'available_accounts':['platform','cash','other'], 'unavailable_accounts':['platform','cash','other'],
         '_account_contributions':{'platform':D('100'),'cash':D('10'),'other':D('100')}}
    total={'account_code':'platform','currency':'CNY','net_asset':'120','covered_account_codes':['platform','cash']}
    _apply_account_totals(row,[total],lambda _:D(1),lambda *_:D(1))
    assert row['available_net_worth']==D('220')
    assert row['_account_contributions']=={'platform':D('120'),'other':D('100')}
    assert row['unavailable_accounts']==['other']
    _apply_account_totals(row,[total],lambda _:D(1),lambda *_:D(1))
    assert row['available_net_worth']==D('220')
    other={**row,'_account_contributions':{'other':D('100')},'available_accounts':['platform','other']}
    _apply_account_totals(other,[total],lambda _:D(1),lambda *_:D(1))
    assert other['available_net_worth']==D('220')
    conflicting=[total,{'account_code':'cash','currency':'CNY','net_asset':'10','covered_account_codes':['cash']}]
    before=row['available_net_worth']
    _apply_account_totals(row,conflicting,lambda _:D(1),lambda *_:D(1))
    assert row['available_net_worth']==before


def test_total_does_not_hide_source_evidence_failure():
    row={'date':date(2025,12,31), 'net_worth':None, 'available_net_worth':D('20'),
         'available_accounts':['cash'], 'unavailable_accounts':['platform'],
         'source_evidence_unavailable_accounts':['platform'], '_account_contributions':{'cash':D('20')}}
    total={'account_code':'platform','currency':'CNY','net_asset':'120','covered_account_codes':['platform','cash']}
    _apply_account_totals(row,[total],lambda _:D(1),lambda *_:D(1))
    assert row['available_net_worth']==D('20')
    assert row['unavailable_accounts']==['platform']


def test_existing_partial_estimates_are_replaced_without_recounting():
    from domain.investments.valuation_nav import build_weekly_nav_rows, ValuationEvidenceSeries, _add_known_display_values
    day=date(2025,12,31)
    banks={day:{'net_worth':D('20'),'assets':D('20'),'liabilities':D(0),
                'available_accounts':['cash'],'account_values':{'cash':D('20')},'unavailable_accounts':['platform']}}
    snapshots=ValuationEvidenceSeries({day:D(0)}); snapshots.unknown_accounts_by_date[day]=['platform']
    row=build_weekly_nav_rows(dates=[day],bank_series=banks,snapshot_series=snapshots,
                              a_share_series=ValuationEvidenceSeries(),ibkr_values={})[0]
    _add_known_display_values(row,{'platform':(D('80'),day,day)})
    total={'account_code':'platform','currency':'CNY','net_asset':'120','covered_account_codes':['platform','cash']}
    _apply_account_totals(row,[total],lambda _:D(1),lambda *_:D(1))
    assert row['available_net_worth']==D('120')


def test_screenshot_total_publication_and_date_scope():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import isolation,source_records_v2 as sources
    from domain.investments import account_total_evidence as total
    dsn=os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn: pytest.skip('requires explicitly isolated PostgreSQL')
    day=date(2025,12,31)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_enabled(cur)
                cur.execute("insert into pams_accounts(account_code,display_name) values ('platform','p'),('cash','c')")
                source,item=uuid4(),uuid4()
                sources.create_source(cur,source_id=source,account_code='platform',source_name='synthetic',acquisition_method='screenshot',document_type='statement',filename='test.jpg',file_format='jpg',content=b'synthetic')
                fields={'kind':'account_portfolio_value','value_scope':'platform_total','business_date':day.isoformat(),'currency':'CNY','net_asset':'120','covered_account_codes':['platform','cash']}
                sources.add_item(cur,source_id=source,item_id=item,parse_method='manual',raw_fields={'entered_fields':fields},parsed_fields=fields)
                sources.review_item(cur,source_id=source,item_id=item,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                total.publish(cur,source_id=source,revision=3)
                found=total.load(cur,dates=[day,date(2026,1,1)],accounts={'platform','cash'})
                assert found[(day,'platform')]['net_asset']=='120'
                assert found[(day,'platform')]['observed_total']
                assert len(found)==1
                assert total.load(cur,dates=[day],accounts={'platform'})=={}
                cur.execute("SELECT version_key FROM valuation_evidence WHERE calculation_version=%s",(total.VERSION,))
                key=cur.fetchone()['version_key']
                jan1=date(2026,1,1)
                for amount in ('121','120'):
                    follow,follow_item=uuid4(),uuid4()
                    next_fields=dict(fields,business_date=jan1.isoformat(),net_asset=amount,
                                     attestation='unchanged_account_total',prior_version_key=key)
                    sources.create_source(cur,source_id=follow,account_code='platform',source_name='Synthetic',
                                          acquisition_method='manual',document_type='statement')
                    sources.add_item(cur,source_id=follow,item_id=follow_item,parse_method='manual',
                                     raw_fields={'entered_fields':next_fields},parsed_fields=next_fields)
                    sources.review_item(cur,source_id=follow,item_id=follow_item,expected_revision=2,
                                        parsed_fields=next_fields,review_status='confirmed',note='User confirmed total at this close')
                    if amount=='121':
                        with pytest.raises(ValueError): total.publish(cur,source_id=follow,revision=3)
                    else:
                        total.publish(cur,source_id=follow,revision=3)
                found=total.load(cur,dates=[day,jan1,date(2026,1,2)],accounts={'platform','cash'})
                assert set(found)=={(day,'platform'),(jan1,'platform')}
                assert found[(jan1,'platform')]['net_asset']=='120'
                row = {'date': jan1, 'net_worth': None, 'available_net_worth': D(10),
                       'available_accounts': ['cash'], '_account_contributions': {'cash': D(10)},
                       'estimated_accounts': ['cash', 'other']}
                _apply_account_totals(row, [found[(jan1, 'platform')]], lambda _: D(1), lambda *_: D(1))
                assert row['available_net_worth'] == D(120) and row['estimated_accounts'] == ['other']
                with pytest.raises(ValueError): total.publish(cur,source_id=source,revision=3)
                second, item2 = uuid4(), uuid4()
                revised = dict(fields, net_asset='125')
                sources.create_source(cur, source_id=second, account_code='platform', source_name='revised',
                                      acquisition_method='screenshot', document_type='statement',
                                      filename='other.jpg', file_format='jpg', content=b'other')
                sources.add_item(cur, source_id=second, item_id=item2, parse_method='manual',
                                 raw_fields={'entered_fields': revised}, parsed_fields=revised)
                sources.review_item(cur, source_id=second, item_id=item2, expected_revision=2,
                                    parsed_fields=revised, review_status='confirmed')
                total.publish(cur, source_id=second, revision=3)
                assert total.load(cur, dates=[day, jan1], accounts={'platform','cash'}) == {}
        finally:
            conn.rollback()

def test_confirmed_empty_account_is_a_single_day_total_without_holdings():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import isolation, source_records_v2 as sources
    from domain.investments import account_total_evidence as total

    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    day = date(2025,12,31)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('empty_scope','Synthetic')")
                for net_asset in ('1','0'):
                    source, item = uuid4(), uuid4()
                    sources.create_source(cur, source_id=source, account_code='empty_scope', source_name='Synthetic',
                                          acquisition_method='manual', document_type='statement')
                    fields = dict(kind='account_portfolio_value', value_scope='account_total', currency='CNY',
                                  net_asset=net_asset, covered_account_codes=['empty_scope'],
                                  business_date=day.isoformat(), attestation='account_empty')
                    sources.add_item(cur,source_id=source,item_id=item,parse_method='manual',
                                     raw_fields={'entered_fields':fields},parsed_fields=fields)
                    sources.review_item(cur,source_id=source,item_id=item,expected_revision=2,parsed_fields=fields,
                                        review_status='confirmed',note='User confirms no positions or cash that day')
                    if net_asset=='1':
                        with pytest.raises(ValueError):
                            total.publish(cur, source_id=source, revision=3)
                    else:
                        total.publish(cur, source_id=source, revision=3)
                dates=[day,date(2026,1,1)]
                found=total.load(cur,dates=dates,accounts={'empty_scope'})
                assert list(found)==[(day,'empty_scope')] and found[(day,'empty_scope')]['confirmed_empty']
                cur.execute("SELECT count(*) n FROM pams_holdings_snapshot WHERE account_code='empty_scope'")
                assert cur.fetchone()['n']==0
        finally:
            conn.rollback()
