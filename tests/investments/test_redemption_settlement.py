from datetime import date
from decimal import Decimal as D
import os
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from domain.imports.isolation import source_enabled
from domain.imports.publication import holding_publication_v2 as publication
from domain.imports.sources import source_records_v2 as sources
from domain.investments.trades import redemption_settlement as settlement
from domain.investments.holdings.position_nav_projection import load_position_nav_estimates


def test_redeemed_units_require_a_dated_confirmed_receipt_and_matching_net_proceeds(monkeypatch):
    left = dict(kind='trade', id=42, account_code='fund', currency='CNY', amount='-19.80', execution_day='2026-01-05')
    right = dict(kind='cashflow', id=25, account_code='bank', currency='CNY', amount='19.80', day='2026-01-07')
    pair = dict(from_evidence=left, to_evidence=right, from_account_code='fund',
                evidence_status='confirmed', from_trade_id=42, to_cashflow_id=25)
    trade = dict(source_evidence_status='verified', account_code='fund', asset_id='A', symbol='A', currency='CNY',
                 trade_type='sell', trade_id=42, net_cash=D('19.80'), fee_amount=D('0.20'))
    extra = [dict(status='verified', fields=dict(execution_date=left['execution_day'], quantity='10', unit_nav='2'))]
    assert settlement._redemption(pair, trade, extra)['amount'] == D('19.80')
    assert settlement._redemption(pair, trade, []) is None
    assert settlement._redemption({**pair, 'evidence_status':'revoked'}, trade, extra) is None
    assert settlement._redemption({**pair, 'to_evidence':{**right,'day':left['execution_day']}}, trade, extra) is None
    assert settlement._redemption({**pair, 'to_evidence':{**right,'amount':'20'}}, trade, extra) is None

    # A conflicting second allocation of the same receipt cannot produce
    # two claims, even if each source otherwise verifies separately.
    monkeypatch.setattr(settlement.transfers,'read',lambda *_,**__: [pair, {**pair, 'from_trade_id':99}])
    class Cursor:
        def execute(self, *_): pass
        def fetchone(self): return {'supported':True}
    assert not settlement.load_redemption_receivables(Cursor(), ['fund'], [date(2026,1,5)])


def test_dated_receivable_and_remaining_position_are_counted_once(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    day, receipt = date(2026,1,5), date(2026,1,7)
    code = 'redemption_test'
    claim = dict(account_code=code, currency='CNY', asset_id='A', symbol='A', amount=D(20),
                 confirmation=day, receipt=receipt, trade_id=42)
    monkeypatch.setattr('domain.investments.holdings.position_nav_projection.load_redemption_receivables',lambda *args:[claim])
    dates = [date(2026,1,4), day, date(2026,1,6), receipt]
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,'Synthetic')", (code,))
                kwargs = dict(accounts={code:{}}, dates=dates, owner_ratio_case_sql=lambda _:('1::numeric',[]),
                              account_owner_ratio=lambda _:D(1), rate_at=lambda *_:D(1))
                result = load_position_nav_estimates(cur, **kwargs)
                assert [result.get(d,{}).get(code,(None,))[0] for d in dates] == [None,20,20,None]
                # A stale observation includes all the units. A new reviewed
                # post-redemption observation can establish the remainder.
                def position(d, value):
                    cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,currency,
                        major_category,minor_category,asset_liability,market_value)
                        VALUES (%s,%s,'A','Synthetic','CNY','其他','其他','资产',%s) RETURNING id""", (code,d,value))
                    return cur.fetchone()['id']
                position(dates[0], 100)
                assert load_position_nav_estimates(cur,**kwargs)[day][code][0] == 100
                cur.execute("""INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency)
                    VALUES ('A','Synthetic','A','fund','CNY')""")
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code=code, source_name='closing position',
                                      acquisition_method='manual', document_type='holding')
                fields = dict(business_date=str(day), currency='CNY', asset_type='fund', symbol='A',
                              quantity='10', price='8', market_value='80', cost='80', cost_basis_type='holding_cost')
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual',
                                 raw_fields={'entered_fields': fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields,
                                    review_status='confirmed')
                plan = publication.preview(cur, source_id=source, revision=3)
                publication.publish(cur, source_id=source, revision=3, preview_token=plan['preview_token'])
                result = load_position_nav_estimates(cur, **kwargs)
                assert [result[d][code][0] for d in dates] == [100,100,100,80]
        finally:
            conn.rollback()


def test_fund_receivable_starts_when_shares_leave_and_ends_on_posting_day():
    trade = dict(trade_id=5, account_code='fund', asset_id='fund-A', symbol='A', trade_type='fund_redemption',
                 currency='CNY', trade_date=date(2026,3,30), fund_pricing_date='2026-03-31',
                 fund_confirmation_date='2026-04-01', fund_execution_confirmed=True, source_evidence_status='verified',
                 quantity=D('10.01'), price=D('3'), gross_amount=D('30.02'), fee_amount=D('.02'), net_cash=D('30'))
    cash = dict(kind='cashflow', account_code='cash', day='2026-03-30', amount='30', currency='CNY')
    sale = dict(kind='trade', id=5, account_code='fund', currency='CNY', amount='-30', cash_event_day=cash['day'],
                receipt_day='2026-04-01', settlement_confirmation_id='reviewed')
    pair = dict(evidence_status='confirmed', from_evidence=sale, to_evidence=cash)
    claim = settlement._redemption(pair, trade, [])
    assert (claim['confirmation'], claim['receipt'], claim['amount']) == (date(2026,3,31), date(2026,4,1), D(30))
    assert settlement._redemption(pair, {**trade, 'fund_execution_confirmed':False}, []) is None
    assert settlement._redemption(pair, {**trade, 'quantity':D('11')}, []) is None
    assert settlement._redemption(pair, {**trade, 'gross_amount':D('30.10')}, []) is None
    assert settlement._redemption(pair, {**trade, 'fund_confirmation_date':'2026-04-02'}, []) is None
    assert settlement._redemption({**pair, 'to_evidence':{**cash, 'day':'2026-03-31'}}, trade, []) is None
