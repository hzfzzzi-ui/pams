"""Account-level estimates must keep every holding, its quantity and its own currency."""
import os
from datetime import date, timedelta
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from domain.accounts.account_history import read_account_history
from domain.accounts.position_valuation import _position_estimates
from domain.investments.valuation.valuation_nav import _add_dated_account_observations
from decimal import Decimal
from domain.imports import source_records_v2 as sources
from domain.imports.isolation import source_enabled
from domain.investments.valuation.account_total_evidence import publish as publish_total
from domain.market.quote_valuation import GOLD_BENCHMARK_ASSET, GOLD_CLOSE_SOURCE


def test_verified_trades_reprice_all_members_without_inventing_cash_or_zero():
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    code = 'test_position_history'
    days = [date(2025, 1, 6) + timedelta(days=n) for n in range(5)]
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert source_enabled(cur)
                cur.execute('INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,%s)', (code, code))
                for asset, symbol, kind in [('hist_f', '001999', 'fund'), ('hist_g', 'GOLD', 'precious_metal')]:
                    cur.execute("""INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency)
                        VALUES (%s,%s,%s,%s,'CNY')""", (asset, asset, symbol, kind))
                for day, asset, symbol, action, count, cash in [
                    (days[1], 'hist_f', '001999', 'buy', 10, -20),
                    (days[2], 'hist_g', 'GOLD', 'buy', 1, -5),
                    (days[4], 'hist_f', '001999', 'sell', 10, 40),
                ]:
                    src, item = uuid4(), uuid4()
                    fields = dict(business_date=day.isoformat(), trade_type=action, asset_id=asset, symbol=symbol,
                                  currency='CNY', net_cash=str(cash), quantity=str(count), execution_status='confirmed')
                    sources.create_source(cur, source_id=src, account_code=code, source_name='order',
                                          acquisition_method='manual', document_type='trade')
                    sources.add_manual_item(cur, source_id=src, item_id=item, expected_revision=1, parsed_fields=fields)
                    sources.review_item(cur, source_id=src, item_id=item, expected_revision=2, parsed_fields=fields,
                                        review_status='confirmed')
                    cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(src),))
                    cur.execute("""INSERT INTO pams_investment_trade_fact
                        (account_code,trade_date,trade_type,side,currency,asset_id,symbol,net_cash,quantity,execution_status,source_item_id,fact_role)
                        VALUES (%s,%s,%s,%s,'CNY',%s,%s,%s,%s,'confirmed',%s,'primary')""",
                        (code, day, action, action, asset, symbol, cash, count, str(item)))
                for asset, symbol, category, units in [('hist_f','001999','A股',10), ('hist_g','GOLD','贵金属',1)]:
                    cur.execute("""INSERT INTO pams_holdings_snapshot
                        (account_code,as_of,asset_id,symbol,name,currency,asset_liability,major_category,minor_category,quantity,market_value)
                        VALUES(%s,%s,%s,%s,%s,'CNY','资产',%s,%s,%s,%s) RETURNING id""",
                        (code, days[3], asset, symbol, symbol, category, category, units, units))
                    if symbol == 'GOLD':
                        ident, version = cur.fetchone()['id'], str(uuid4())
                        cur.execute("""INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash)
                            SELECT %s,id,md5(to_jsonb(h)::text) FROM pams_holdings_snapshot h WHERE id=%s""", (version, ident))
                        cur.execute("""INSERT INTO account_snapshot_version
                            (version_id,account_code,as_of,coverage_status,cash_status,calculation_version,reason)
                            VALUES (%s,%s,%s,'partial','unknown','test-history','synthetic')""", (version, code, days[3]))
                for day, price in [(days[1],2), (days[2],3), (days[4],4)]:
                    cur.execute("""INSERT INTO market_observations
                        (observation_type,as_of,asset_id,symbol,market,currency,unit_nav,source,source_key)
                        VALUES ('daily',%s,'hist_f','001999','基金','CNY',%s,'eastmoney_f10',%s)""",
                        (day, price, str(uuid4())))
                for day in days[2:]:
                    cur.execute("""INSERT INTO market_observations
                        (observation_type,as_of,asset_id,symbol,market,currency,close_price,source,source_key)
                        VALUES ('daily',%s,%s,'Au99.99','SGE','CNY',5,%s,%s)""",
                        (day, GOLD_BENCHMARK_ASSET, GOLD_CLOSE_SOURCE, str(uuid4())))
                cfg = {'account_type':'fund_platform', 'base_currency':'CNY'}
                anchors = {days[0]: {'net_value': 0, 'net_value_status':'confirmed'}}
                _position_estimates(cur, code, cfg, days, anchors)
                # A missing fund quote cannot produce a gold-only total. The later
                # sale has no settlement evidence and cannot erase the earlier marks.
                assert {d:p['net_value'] for d,p in anchors.items()} == {days[0]:0, days[1]:20, days[2]:35}
                source, item = uuid4(), uuid4()
                fields = {'kind':'account_portfolio_value', 'value_scope':'account_total', 'currency':'CNY',
                          'business_date':'2024-12-31', 'net_asset':'0', 'covered_account_codes':[code],
                          'attestation':'account_empty'}
                sources.create_source(cur, source_id=source, account_code=code, source_name='opening',
                                      acquisition_method='manual', document_type='statement')
                sources.add_manual_item(cur, source_id=source, item_id=item, expected_revision=1, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields,
                                    review_status='confirmed', note='Confirmed synthetic opening balance')
                publish_total(cur, source_id=source, revision=3)
                window = {}
                _position_estimates(cur, code, cfg, days[1:], window)
                assert {d:p['net_value'] for d,p in window.items()} == {days[1]:20, days[2]:35}
                cfg['reconcile_method'] = 'position_nav'
                detail = read_account_history(cur, account_code=code, account_config={'accounts': {code: cfg}},
                                              start_date=days[1], end_date=days[-1])
                detail_values = {row['date']: row['net_value'] for row in detail['rows']}
                nav = [{'date': day, 'net_worth': None, 'unavailable_accounts': [code]} for day in days[1:]]
                _add_dated_account_observations(cur, nav, {code: cfg}, {code}, lambda _: Decimal('.5'),
                                                lambda *_: Decimal(1))
                assert [row.get('available_net_worth') for row in nav] == [10, Decimal('17.5'), None, None]
                assert detail_values[days[2]] == 2 * nav[1]['available_net_worth']
                cfg['latest_position_evidence'] = 'fund_snapshot_without_cash'
                points = {days[2]: {'net_value': 36, 'net_value_status':'confirmed'}}
                _position_estimates(cur, code, cfg, days, points)
                assert days[0] not in points and days[3] not in points
                assert points[days[2]] == {'net_value':36, 'net_value_status':'confirmed',
                                           'position_value':35, 'position_value_status':'partial',
                                           'position_symbols':['001999', 'GOLD']}
                assert points[days[4]] == {'position_value':5, 'position_value_status':'partial', 'position_symbols':['GOLD']}
        finally:
            conn.rollback()


def test_month_end_prices_use_current_units_and_keep_each_mark_date(monkeypatch):
    from domain.investments.holdings import account_position_history as history
    friday, saturday, monday = date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 2)
    days = [saturday, monday]
    trades = [{'asset_id': code, 'asset_type': 'fund', 'symbol': code} for code in ('A', 'B')]
    prices = {('A', friday): Decimal(2), ('B', saturday): Decimal(3), ('A', monday): Decimal(100)}
    def quotes(cur, *, assets, start, end):
        assert start == friday and end == monday
        return prices
    monkeypatch.setattr(history, 'load_fund_nav_marks', quotes)
    monkeypatch.setattr(history, '_position_history', lambda *args: ((dict.fromkeys(days, 5), {}), None))
    marks = history._quantity_marks(None, days, trades, {}, set())
    assert marks[saturday] == 25 and marks[monday] is None
    assert marks.price_dates[saturday] == {'A': friday, 'B': saturday}
    prices['A', saturday] = None  # conflicting exact quotes cannot be bypassed
    assert history._quantity_marks(None, days, trades, {}, set())[saturday] is None


def test_month_end_direct_account_amount_precedes_prior_session_price(monkeypatch):
    from domain.investments.holdings import account_position_history as history
    friday, sunday = date(2026, 5, 29), date(2026, 5, 31)
    marks = history.PositionMarks({friday: Decimal(12), sunday: Decimal(12)})
    marks.price_dates[sunday]['GOLD'] = friday
    monkeypatch.setattr(history, 'load_account_trade_marks', lambda *a, **kw: marks)
    cfg = {'account_type':'fund_platform', 'truth_source':'manual', 'reconcile_method':'position_nav',
           'manual_balance':{'source':'statement', 'amount':'13', 'as_of':sunday.isoformat()}}
    points = {friday: {'net_value':Decimal(12), 'net_value_status':'confirmed'}}
    _position_estimates(None, 'test', cfg, [friday, sunday], points)
    assert points[sunday]['net_value'] == 13 and not points[sunday].get('price_dates')
