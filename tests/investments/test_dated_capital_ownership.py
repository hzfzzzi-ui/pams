from datetime import date
from decimal import Decimal as D
from uuid import uuid4
import os
import pytest

from domain.investments.capital_ownership import contribution_ratios, load_contribution_ratios


def test_partial_deposit_and_other_investor_withdrawal():
    d1, d2, d3, d4 = [date(2026, 1, day) for day in range(1, 5)]
    events = [dict(trade_date=d1, trade_type='deposit', net_cash=D(100)),
              dict(trade_date=d2, trade_type='withdraw', net_cash=D(-20)),
              dict(trade_date=d3, trade_type='deposit', net_cash=D(20))]
    legs = {(d1, 'deposit'): D(50), (d3, 'deposit'): D(20)}
    dates = [d1, d2, d3, d4]
    assert contribution_ratios(events, legs, dates, d3, D(70), D(100)) == {
        d1: D('.5'), d2: D('.625'), d3: D('.7')}
    assert contribution_ratios(events, legs, dates, d3, D(60), D(100)) == {}
    assert contribution_ratios(events, legs, dates, d3, D(70), D(80)) == {}


def test_only_original_backed_cash_contributes_on_receipt():
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
    from domain.imports import source_records_v2 as sources, isolation
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    first, receipt = date(2026, 1, 1), date(2026, 1, 2)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('source_bank','bank')")
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='source_bank', source_name='Original',
                    acquisition_method='file', document_type='cash', content=b'synthetic statement',
                    filename='example.csv', file_format='csv')
                fields = {'currency':'CNY','net_cash':'-50','business_date':str(first)}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='template',
                    raw_fields={'initial_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2,
                    parsed_fields=fields, review_status='confirmed')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(source),))
                cur.execute("""INSERT INTO pams_cashflow_fact(account_code,event_date,amount,currency,source_item_id,fact_role)
                    VALUES ('source_bank',%s,-50,'CNY',%s,'primary')""", (first,str(item)))
                cfg = {'funding_source':'source_bank','personal_capital':50,'capital_base':200,
                       'funding_history':{'as_of':str(receipt),'complete_personal_flows':True,
                                          'bank_receipts':{str(item):str(receipt)}}}
                events = [dict(trade_date=day, trade_type='deposit', net_cash=D(100)) for day in (first,receipt)]
                assert load_contribution_ratios(cur,cfg,events,[first,receipt]) == {first:D(0), receipt:D('.25')}
                cur.execute("UPDATE pams_cashflow_fact SET amount=-55 WHERE source_item_id=%s", (str(item),))
                assert load_contribution_ratios(cur,cfg,events,[first,receipt]) == {}
        finally:
            conn.rollback()
