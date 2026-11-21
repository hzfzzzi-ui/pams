from datetime import date
from decimal import Decimal
import os
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from domain.imports import holding_publication_v2 as publication, source_records_v2 as sources
from domain.imports.isolation import source_enabled
from domain.investments.personal_loan_nav import annotate_personal_loans, apply_personal_loan_coverage
from domain.investments.valuation_nav import format_weekly_nav_payload


def test_borrowing_does_not_become_net_worth_growth_or_an_opening_balance():
    first, last = date(2025, 12, 31), date(2026, 1, 1)
    def point(day, amount):
        return dict(date=day, net_worth=Decimal(amount), available_accounts=['cash'],
                    _account_contributions={'cash': Decimal(amount)}, _comparison_basis={'cash': 'cash_balance'},
                    _dated_cash_accounts={'cash'})
    fact = dict(account_code='cash', transaction_date=last, currency='CNY', amount=Decimal(10),
                classification_status='confirmed', classification_source='manual', source_evidence_status='verified',
                cashflow_type='debt_drawdown', category='个人借款', subcategory='个人借入本金')
    for override in ({'classification_status':'pending'}, {'source_evidence_status':'unknown'},
                     {'subcategory':'收回借出款'}, {'category':'应收款'}):
        rows = [point(last, 0)]
        annotate_personal_loans(rows, [{**fact, **override}], {}, lambda _: None)
        assert rows[0]['net_worth'] == 0 and 'personal_loan_coverage' not in rows[0]
    rows = [point(first, 0), point(last, 10)]
    annotate_personal_loans(rows, [fact], {}, lambda _: None)
    report = format_weekly_nav_payload(start_date=first, terminal_date=last, base_currency='CNY', rows=rows)
    short = [point(first, 0)]
    annotate_personal_loans(short, [], {}, lambda _: None)
    assert rows[0] == short[0] and report['rows'][0]['netWorth'] == '0'
    assert 'personal_loan_coverage' not in report['rows'][0]
    assert report['rows'][1]['personal_loan_coverage'] == dict(status='principal_movements_only', balance=None,
        day_movements=[dict(currency='CNY', borrowed='10', repaid='0')])
    assert report['period_comparisons']['ytd']['status'] == 'different_coverage'
    assert report['period_comparisons']['ytd']['comparable_accounts']['account_codes'] == []
    paid = dict(account_code='debt', as_of=last, market_value=Decimal(0), currency='CNY')
    later = [point(date(2026, 1, 2), 10), point(date(2026, 1, 3), 19)]
    annotate_personal_loans(later, [fact, {**fact, 'transaction_date':later[-1]['date'], 'amount':Decimal(9)}],
                            {last:paid}, lambda r:r['market_value'])
    assert later[0]['net_worth'] == 10 and 'personal_loan_coverage' not in later[0]
    assert later[1]['net_worth'] is None and later[1]['personal_loan_coverage']['balance'] is None


@pytest.mark.parametrize('amount', ['0', '15'])
def test_principal_anchor_is_dated_and_must_still_verify(monkeypatch, amount):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    code, day = 'synthetic_payable', date(2026, 5, 2)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name,is_asset) VALUES (%s,'Synthetic',false)", (code,))
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code=code, source_name='dated principal statement',
                                      acquisition_method='manual', document_type='holding')
                fields = dict(business_date=str(day), currency='CNY', asset_type='cash', net_cash=amount)
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual',
                                 raw_fields={'entered_fields': fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields,
                                    review_status='confirmed')
                scope = dict(as_of=str(day), confirmed=True, point_in_time_only=True,
                             note='All personal borrowing principal at the close', cash_currency='CNY',
                             member_count=1, cash_amount=amount, liability_scope='all_personal_loan_principal')
                plan = publication.preview(cur, source_id=source, revision=3, scope=scope)
                publication.publish(cur, source_id=source, revision=3, preview_token=plan['preview_token'], scope=scope)
                def read():
                    rows = [dict(date=day, net_worth=-Decimal(amount), _account_contributions={code:-Decimal(amount)})]
                    apply_personal_loan_coverage(cur, rows, [code], lambda p:p['market_value'])
                    return rows[0]
                row = read()
                assert row['net_worth'] == -Decimal(amount) and row['personal_loan_coverage']['status'] == 'confirmed'
                assert Decimal(row['personal_loan_coverage']['balance']) == Decimal(amount)
                def read_without_source_account():
                    points = [dict(date=date(2026, 5, d), net_worth=Decimal(12),
                                   _account_contributions={'cash':Decimal(12)}) for d in (1, 2, 3)]
                    apply_personal_loan_coverage(cur, points, ['cash'], lambda p:p['market_value'])
                    return points
                before, opening, after = read_without_source_account()
                if amount == '0':
                    assert opening['personal_loan_coverage']['status'] == 'confirmed'
                    assert Decimal(opening['personal_loan_coverage']['balance']) == 0
                else:
                    assert 'personal_loan_coverage' not in opening
                assert opening['net_worth'] == 12 and opening['_account_contributions'] == {'cash':Decimal(12)}
                assert all('personal_loan_coverage' not in row for row in (before, after))
                cur.execute('UPDATE pams_holdings_snapshot SET market_value=20 WHERE source_item_id=%s', (str(item),))
                assert 'personal_loan_coverage' not in read()
                assert all('personal_loan_coverage' not in p for p in read_without_source_account())
        finally:
            conn.rollback()
