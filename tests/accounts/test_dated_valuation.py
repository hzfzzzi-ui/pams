from datetime import date
from decimal import Decimal as D
import os

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from domain.accounts.monthly import dated_valuation as valuation
from domain.accounts.views.account_history import read_account_history
from domain.imports.isolation import source_enabled
from domain.investments.valuation.valuation_nav import build_nav_weekly_payload


def test_detail_and_portfolio_use_the_same_statement_across_months(monkeypatch):
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    first, second = date(2025, 3, 31), date(2025, 4, 30)
    code = 'synthetic_cash'
    cfg = {'account_code': code, 'account_type': 'funding', 'include_in_nav': True,
           'base_currency': 'CNY', 'owner_ratio': D('.25'),
           'primary_balance': {'as_of': second, 'amount': D(25), 'currency': 'CNY'}}
    monkeypatch.setattr(valuation, 'statement_balance_history', lambda *args, **kwargs: {
        code: [{'amount': '0', 'as_of': first}, {'amount': '160', 'as_of': second}]})
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert source_enabled(cur)
        history = read_account_history(cur, account_code=code, account_config={'accounts': {code: cfg}},
                                       start_date=first, end_date=second)
        assert {r['date']: r['net_value'] for r in history['rows']} == {first: 0, second: 160}
        for interval in ('weekly', 'daily'):
            nav = build_nav_weekly_payload(cur, start_date=first, terminal_date=second, interval=interval,
                base_currency='CNY', overview_payload={'accounts': [cfg]}, account_config={'accounts': {code: cfg}},
                include_ibkr=False, owner_ratio_case_sql=lambda _: ('1::numeric', []),
                account_owner_ratio=lambda _: D('.25'))
            assert D(nav['rows'][0]['netWorth']) == 0
            assert D(nav['rows'][-1]['netWorth']) == 40


def test_only_a_whole_dated_value_can_be_converted_to_personal_nav():
    day = date(2025, 2, 28)
    point = {'net_value': D(100), 'liabilities': D(50), 'position_value': D(140)}
    read = valuation.AccountValuation.from_measurement
    assert read(day, 'USD', point, 'net_value').personal_value(D('.5'), D(7)) == 350
    assert read(day, 'USD', point, 'net_value').personal_value(D('.5'), None) is None
    for field in ('liabilities', 'position_value'):
        assert read(day, 'USD', point, field).personal_value(D(1), D(7)) is None
    point.update(net_value_status='partial')
    assert read(day, 'USD', point, 'net_value').personal_value(D(1), D(7)) is None


def test_month_end_billed_cash_uses_posting_date_and_audited_interest_only_once():
    from domain.accounts.monthly.monthly_cash_bridge import posted_balances
    start, end, cutoff = date(2026, 3, 2), date(2026, 4, 2), date(2026, 3, 31)
    bounds = [dict(as_of=start, amount='-12'), dict(as_of=end, amount='-22')]
    def flow(value, posted):
        return dict(transaction_date=cutoff, posting_date=posted, currency='USD', amount=D(value),
                    source_evidence_status='verified')
    flows = [flow(-5,cutoff), flow(-3,end)]
    charge = {end:{('USD','2')}}
    calc = lambda rows, extras: posted_balances(bounds, rows, [cutoff], 'USD', extras)
    assert calc(flows,{}) == {}
    assert calc(flows,charge) == {cutoff:D(-17)}
    assert calc(flows + [flow(-2,end)],charge) == {cutoff:D(-17)}
    assert calc([flow(-5,None),flow(-3,end)],charge) == {}
    observation = valuation.AccountValuation(cutoff,'USD',D(-17),'posted_statement_bridge','billed')
    assert observation.including_unbilled(D(100)).amount == -117


def test_recurring_credit_requires_matching_evidence_and_ends_at_confirmed_month():
    from domain.accounts.monthly.monthly_credit_evidence import confirmed_months
    start = date(2025, 12, 31)
    dates = [date(2026,1,30),date(2026,1,31),date(2026,2,28),date(2026,3,31)]
    proof = dict(anchor_date='2026-02-28',months=2,anchor_amount='140',result='120',monthly_credit='10',
                 other_movements='user_confirmed_none_2026_jan_feb')
    evidence = dict(kind='contribution_evidence',currency='CNY',period='2026-01/2026-02',monthly_personal_amount='10')
    calc = lambda: confirmed_months(start,D(120),proof,evidence,'CNY',dates)
    assert calc() == {dates[1]: D(130), dates[2]: D(140)}
    evidence['monthly_personal_amount'] = '11'
    assert calc() == {}
    evidence['monthly_personal_amount'] = '10'
    proof['other_movements'] = 'unknown'
    assert calc() == {}


def test_billed_table_uses_closed_period_then_latest_statement_without_double_count():
    from domain.accounts.monthly.monthly_cash_bridge import table_balances_at
    jan, feb, mar = [date(2026, n, 23) for n in (1, 2, 3)]
    jan_end, feb_end, mar_end, apr_end = date(2026,1,31), date(2026,2,28), date(2026,3,31), date(2026,4,30)
    closes = [{'as_of': jan,'amount':'-22'},{'as_of': feb,'amount':'-32'},{'as_of': mar,'amount':'-12'}]
    moves = {date(2026,1,30):D(-7),feb:D(-3),date(2026,2,27):D(20),date(2026,4,1):D(5)}
    targets = [jan_end,feb_end,mar_end,apr_end]
    result = table_balances_at(closes,moves,targets)
    assert result == {jan_end:(D(-29),'posted_statement_bridge',jan,feb),
                      feb_end:(D(-12),'posted_statement_bridge',feb,mar),
                      mar_end:(D(-12),'statement_plus_postings',mar,mar)}
    closes[-1]['amount']='-11'
    assert list(table_balances_at(closes,moves,targets)) == [jan_end]


def test_reviewed_statement_documents_and_native_table_feed_one_billed_value():
    from uuid import uuid4
    from domain.imports.sources import source_records_v2 as sources
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    code = 'synthetic_statement_table'
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert source_enabled(cur)
                cur.execute('INSERT INTO pams_accounts(account_code,display_name,base_currency,is_asset) VALUES (%s,%s,%s,false)',
                            (code,code,'CNY'))
                def publish(kind, entries):
                    source = uuid4()
                    ext = 'csv' if kind == 'cash' else 'pdf'
                    sources.create_source(cur,source_id=source,account_code=code,source_name='synthetic',
                                          acquisition_method='file',document_type=kind,filename=f'synthetic.{ext}',
                                          file_format=ext, content=str(source).encode())
                    for idx,(day,amount) in enumerate(entries):
                        item = uuid4()
                        fields = dict(business_date=day,currency='CNY',net_cash=amount)
                        if kind == 'statement':
                            fields['action']='balance'
                            position={'row':'statement_balance'}
                            source_fields={'balance_candidate':True}
                        else:
                            fields.update(action='expense',posting_date=day)
                            position={'row':idx}
                            source_fields={'statement_row':{'currency':'CNY','posting_date':day,'amount':amount}}
                        sources.add_item(cur,source_id=source,item_id=item,parse_method='template',parsed_fields=fields,
                                         raw_fields={'initial_fields':fields,'source':source_fields},source_position=position)
                        sources.review_item(cur,source_id=source,item_id=item,expected_revision=2+2*idx,
                                            parsed_fields=fields,review_status='confirmed')
                    cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s",(str(source),))
                publish('statement',[('2026-01-23','100')])
                publish('statement',[('2026-02-23','112')])
                publish('cash',[('2026-01-28','-5'),('2026-02-23','-7'),('2026-03-01','-3')])
                jan,feb = date(2026,1,31),date(2026,2,28)
                rows=valuation.statement_valuations(cur,{code:('CNY',True)},[jan,feb],table_cards=[code])
                assert (rows[code,jan].amount,rows[code,jan].quality)==(D(-105),'confirmed')
                assert (rows[code,feb].amount,rows[code,feb].quality)==(D(-112),'estimated')
                assert rows[code,feb].evidence_dates==(date(2026,2,23),date(2026,2,23))
        finally:
            conn.rollback()
