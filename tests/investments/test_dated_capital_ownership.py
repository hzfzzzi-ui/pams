from datetime import date
from decimal import Decimal as D
from uuid import uuid4
import os
import pytest

from domain.investments.valuation.capital_ownership import contribution_ratios, load_contribution_ratios


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


def test_later_trades_preserve_ownership_until_first_unreviewed_funding(monkeypatch):
    from domain.investments.trades import nav_trade_source_v2 as nav
    from domain.investments.valuation import capital_ownership
    from domain.investments.valuation.valuation_nav import ValuationEvidenceSeries
    from unittest.mock import Mock

    days = [date(2026, 1, day) for day in range(1, 6)]
    first, deposit, trade, unknown_flow, after = days
    events = [
        dict(trade_date=first, trade_type='deposit', net_cash=D(100), broker_cash=D('100.69')),
        dict(trade_date=deposit, trade_type='deposit', net_cash=D(100)),
        dict(trade_date=trade, trade_type='interest', net_cash=D(10)),
        dict(trade_date=unknown_flow, trade_type='withdraw', net_cash=D(-10)),
        dict(trade_date=after, trade_type='interest', net_cash=D(1)),
    ]
    legs = {(first, 'deposit'): D(50)}
    ratios = contribution_ratios(events, legs, days, deposit, D(50), D(200))
    assert ratios == {first: D('.5'), deposit: D('.25'), trade: D('.25')}
    monkeypatch.setattr(nav, '_read_event_prefix', lambda cur, account_code: (events, None))
    monkeypatch.setattr(nav, 'daily_estimates', lambda *args, **kwargs: dict(zip(days, map(D, [110,210,220,210,211]))))
    monkeypatch.setattr(capital_ownership, 'load_contribution_ratios', lambda *args, **kwargs: ratios)
    cur = Mock()
    cur.fetchall.return_value = []
    series = nav.project(cur, dates=days, owner_ratio=D('.9'), series_type=ValuationEvidenceSeries, ownership_config={})
    assert series.preview_by_date == {first:D(55), deposit:D('52.5'), trade:D(55)}
    assert series.preview_performance_by_date == {
        first: {'total_pnl':D(5), 'display_return_rate':D('.1')},
        deposit: {'total_pnl':D('2.5'), 'display_return_rate':D('.05')},
        trade: {'total_pnl':D(5), 'display_return_rate':D('.1')}}


def test_only_original_backed_cash_contributes_on_receipt():
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
    from domain.imports.sources import source_records_v2 as sources
    from domain.imports import isolation
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
                assert load_contribution_ratios(cur,cfg,events,[receipt],unverified_from=receipt) == {}
                cur.execute("UPDATE pams_cashflow_fact SET amount=-55 WHERE source_item_id=%s", (str(item),))
                assert load_contribution_ratios(cur,cfg,events,[first,receipt]) == {}
        finally:
            conn.rollback()


def test_personal_positions_use_dated_shares_without_counting_ownership_changes_as_market_gains(monkeypatch):
    from collections import defaultdict
    from domain.investments.holdings import position_performance as perf
    from domain.investments.valuation.capital_ownership import monthly_profit_reallocations
    from unittest.mock import Mock

    days = [date(2030, 1, day) for day in (1, 2, 3)]
    key = ('pooled', 'CNY', 'X')
    anchors = {day: {'quantity': D(10), 'current_price': D(price), 'source_item_id': 'proof'}
               for day, price in zip(days, (12, 12, 14))}
    monkeypatch.setattr(perf, '_load_positions', lambda *a: ({key: anchors}, {key: 'reference'}, {}))
    monkeypatch.setattr(perf, '_load_events', lambda *a: ({}, {}))
    monkeypatch.setattr(perf, '_load_prices', lambda *a: {})
    monkeypatch.setattr(perf, 'load_fx_timelines', lambda *a: {})
    ratios = dict(zip(days, (D('.5'), D('.25'), D('.25'))))
    monkeypatch.setattr(perf, 'position_ownership', lambda *a: {'pooled': ratios})
    changes = defaultdict(lambda: defaultdict(D))
    series = perf.build_position_return_series(Mock(), start=days[0], end=days[-1], interval='daily',
        base_currency='CNY', owner_ratio=lambda _: D('.9'), excluded_accounts=set(), monthly_changes=changes)
    assert [D(str(r['market_value'])) for r in series['rows']] == [60, 30, 35]
    assert [D(str(r['total_pnl'])) for r in series['rows']] == [0, 0, 5]
    assert changes[days[0]]['reference', 'CNY', 'X'] == 5

    history = [dict(date=days[0].isoformat(), personal_balance=50, account_balance=100),
               dict(date=days[1].isoformat(), personal_balance=50, account_balance=200)]
    nav = {d.isoformat(): {'latest_known_components': {'pooled': {'native_balance': str(v),
            'currency': 'CNY', 'owner_ratio': str(ratios[d])}}} for d, v in zip(days, (120, 220, 240))}
    assert monthly_profit_reallocations(history, nav, 'pooled') == {days[0]: D(-5)}
    del nav[days[0].isoformat()]
    assert monthly_profit_reallocations(history, nav, 'pooled') == {days[0]: None}
