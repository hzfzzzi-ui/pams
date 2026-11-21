"""Historical evidence is partial, dated, and never invents holding cost."""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from domain.imports import holding_publication_v2 as publication, source_records_v2 as sources
from domain.investments.position_nav_projection import load_position_nav_estimates
from domain.investments.holding_evidence_writer import verify_historical_holding_source
from domain.investments.holdings_view import LATEST_KNOWN_HOLDINGS_CTE
from domain.accounts.sync_status import _latest_cash_snapshots, _manual_position_value
from domain.accounts.accounts_view import build_account_detail_payload


def test_reviewed_historical_holding_publish_and_nav(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    day, later = date(2025, 12, 31), date(2026, 1, 1)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_history','Test')")
                cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency) VALUES ('test_fund','Test fund','TESTFUND','fund','CNY')")
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,symbol,name,currency,asset_liability,major_category,minor_category,market_value)
                    VALUES ('test_history','2026-03-01','OTHER','Other','CNY','资产','其他','其他',25)""")
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_history', source_name='reviewed example',
                    acquisition_method='manual', document_type='holding')
                fields = {'business_date': day.isoformat(), 'currency':'CNY', 'asset_type':'fund', 'symbol':'TESTFUND',
                          'quantity':'100', 'price':'1.2', 'market_value':'120', 'cost_basis_type':'unknown'}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                with pytest.raises(ValueError):
                    publication.preview(cur, source_id=source, revision=3)
                with pytest.raises(ValueError, match='完整范围'):
                    publication.preview(cur, source_id=source, revision=3, historical=True, scope={})
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                assert plan['holdings'][0]['cost'] is None
                result = publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                assert result['coverage_status'] == 'partial'
                cur.execute("SELECT id,cost,buy_price FROM pams_holdings_snapshot WHERE source_item_id=%s", (str(item),))
                holding = cur.fetchone()
                assert holding['cost'] is None and holding['buy_price'] is None
                assert verify_historical_holding_source(cur, holding_id=holding['id'], source_item_id=str(item))
                from domain.market.fund_nav_estimates import load_latest_active_fund_holdings
                assert load_latest_active_fund_holdings(cur) == []
                from domain.investments.position_performance import _load_positions
                assert _load_positions(cur, later, set())[0] == {}
                for cutoff, expected in ((day, ['TESTFUND']), (later, []), (date(2026, 3, 1), ['OTHER'])):
                    cur.execute(f'WITH {LATEST_KNOWN_HOLDINGS_CTE} SELECT symbol FROM latest_known_holding ORDER BY symbol', (cutoff,))
                    assert [row['symbol'] for row in cur.fetchall()] == expected
                with pytest.raises(ValueError):
                    publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                assert _latest_cash_snapshots(cur, ['test_history']) == []
                assert _manual_position_value(cur, 'test_history', later) is None
                assert build_account_detail_payload(cur, account_code='test_history')['holdings_count'] == 1
                estimates = load_position_nav_estimates(cur, accounts={'test_history': {}}, dates=[day, later],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal('1'),
                    rate_at=lambda currency, _: Decimal('1') if currency == 'CNY' else None)
                assert estimates[day]['test_history'][0] == Decimal('120')
                assert 'test_history' not in estimates.get(later, {})
                cur.execute('UPDATE pams_holdings_snapshot SET market_value=999 WHERE id=%s', (holding['id'],))
                cur.execute(f'WITH {LATEST_KNOWN_HOLDINGS_CTE} SELECT symbol FROM latest_known_holding', (day,))
                assert cur.fetchall() == []
        finally:
            conn.rollback()


def test_historical_fund_reconstruction_binds_existing_evidence(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    from domain.investments.valuation_input_evidence import bound_holding_freshness
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_reconstruction','Test')")
                cur.execute("INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency) VALUES ('test_fund','Test fund','123456','fund','CNY')")
                trade_source, trade_item = uuid4(), uuid4()
                sources.create_source(cur, source_id=trade_source, account_code='test_reconstruction', source_name='Trade', acquisition_method='manual', document_type='trade')
                trade = {'business_date':'2025-12-15', 'trade_type':'fund_subscription', 'currency':'CNY', 'asset_id':'test_fund', 'symbol':'123456', 'quantity':'100', 'price':'1', 'execution_status':'confirmed'}
                sources.add_item(cur, source_id=trade_source, item_id=trade_item, parse_method='manual', raw_fields={'entered_fields':trade}, parsed_fields=trade)
                sources.review_item(cur, source_id=trade_source, item_id=trade_item, expected_revision=2, parsed_fields=trade, review_status='confirmed')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(trade_source),))
                cur.execute("""INSERT INTO pams_investment_trade_fact(account_code,trade_date,trade_type,currency,asset_id,symbol,quantity,price,execution_status,source_item_id,fact_role)
                    VALUES ('test_reconstruction','2025-12-15','fund_subscription','CNY','test_fund','123456',100,1,'confirmed',%s,'primary') RETURNING trade_id""", (str(trade_item),))
                trade_id = cur.fetchone()['trade_id']
                cur.execute("""INSERT INTO market_observations(observation_type,as_of,asset_id,symbol,market,currency,unit_nav,source,source_key)
                    VALUES ('daily','2025-12-31','test_fund','123456','基金','CNY',1.2,'tushare_fund_nav','test') RETURNING quote_id""")
                quote_id = cur.fetchone()['quote_id']
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_reconstruction', source_name='Historical decision', acquisition_method='manual', document_type='holding')
                fields = {'business_date':'2025-12-31','currency':'CNY','asset_type':'fund','symbol':'123456','quantity':'100','price':'1.2','market_value':'120','cost_basis_type':'unknown', 'reconstruction_evidence':{'trade_ids':[trade_id],'quote_id':quote_id}}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                invalid = {**fields, 'reconstruction_evidence': {'trade_ids': [trade_id + 10000], 'quote_id': quote_id}}
                with pytest.raises(ValueError, match='交易证据'):
                    publication._historical_fund_inputs(cur, invalid, publication._holding(cur, fields, 'test_reconstruction', date(2025, 12, 31), item, complete=False, historical=True))
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                assert len(plan['input_refs'][str(item)]) == 3
                publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                cur.execute("SELECT version_key FROM valuation_evidence WHERE account_code='test_reconstruction'")
                version = cur.fetchone()['version_key']
                assert bound_holding_freshness(cur, version) in {'unknown','unchanged_for_recorded_inputs'}
                nav_args = dict(accounts={'test_reconstruction': {}}, dates=[date(2025, 12, 31)],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal('1'),
                    rate_at=lambda currency, _: Decimal('1') if currency == 'CNY' else None)
                assert load_position_nav_estimates(cur, **nav_args)[date(2025, 12, 31)]['test_reconstruction'][0] == Decimal('120')
                cur.execute("""INSERT INTO market_observations(observation_type,as_of,asset_id,symbol,market,currency,unit_nav,source,source_key)
                    VALUES ('daily','2025-12-31','test_fund','123456','基金','CNY',1.25,'eastmoney_f10','other') RETURNING quote_id""")
                conflicting = cur.fetchone()['quote_id']
                assert not load_position_nav_estimates(cur, **nav_args).get(date(2025, 12, 31))
                cur.execute('DELETE FROM market_observations WHERE quote_id=%s', (conflicting,))
                cur.execute("UPDATE market_observations SET unit_nav=1.3 WHERE quote_id=%s", (quote_id,))
                assert bound_holding_freshness(cur, version) == 'changed'
                assert not load_position_nav_estimates(cur, **nav_args).get(date(2025, 12, 31))
        finally:
            conn.rollback()


def test_historical_foreign_statement_balance_stays_on_its_day(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('test_foreign','Test','MXN')")
                original, original_item = uuid4(), uuid4()
                sources.create_source(cur, source_id=original, account_code='test_foreign', source_name='Statement', acquisition_method='file', document_type='cash', filename='statement.pdf', file_format='pdf', content=b'%PDF synthetic')
                balance = {'kind':'cash', 'asset_type':'cash', 'action':'balance', 'currency':'MXN', 'business_date':'2025-12-31', 'net_cash':'0'}
                sources.add_item(cur, source_id=original, item_id=original_item, parse_method='template', raw_fields={'source':{'balance_candidate':True}, 'initial_fields':balance}, parsed_fields=balance, source_position={'row':'statement_balance'})
                sources.review_item(cur, source_id=original, item_id=original_item, expected_revision=2, parsed_fields=balance, review_status='excluded')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(original),))
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_foreign', source_name='Historical balance', acquisition_method='manual', document_type='holding')
                fields = {**balance, 'reconstruction_evidence': {'statement_source_item_id':str(original_item)}}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                assert plan['holdings'][0]['currency'] == 'MXN'
                publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                estimates = load_position_nav_estimates(cur, accounts={'test_foreign': {}}, dates=[date(2025,12,31),date(2026,1,1)],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal('1'), rate_at=lambda *_: None)
                assert estimates[date(2025,12,31)]['test_foreign'][0] == 0
                assert 'test_foreign' not in estimates.get(date(2026,1,1), {})
        finally:
            conn.rollback()


def test_historical_statement_running_balance_requires_last_verified_row(monkeypatch):
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    from domain.imports import account_import_statement
    day = date(2025, 12, 31)
    from pathlib import Path
    content = (Path(__file__).parent / 'fixtures' / 'synthetic_statement.xls').read_bytes()
    monkeypatch.setattr(account_import_statement, 'profiles', lambda: {'test_1234':[{'statement_template':'ccb_debit_pdf'}]})
    document = account_import_statement.parse(content, 'statement.xls', 'test_1234', 'cash')
    source_row = document['rows'][0]['source']['statement_row']
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_1234','Test')")
                original, original_item = uuid4(), uuid4()
                sources.create_source(cur, source_id=original, account_code='test_1234', source_name='Statement', acquisition_method='file', document_type='cash', filename='statement.xls', file_format='xls', content=content)
                parsed = {'kind':'cash','asset_type':'cash','action':'expense','currency':'CNY','business_date':day.isoformat(),'net_cash':'-1'}
                raw = {'source':{'row':5,'statement_row':source_row},'initial_fields':parsed}
                sources.add_item(cur, source_id=original, item_id=original_item, parse_method='template', raw_fields=raw, parsed_fields=parsed)
                sources.review_item(cur, source_id=original, item_id=original_item, expected_revision=2, parsed_fields=parsed, review_status='confirmed')
                cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(original),))
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_1234', source_name='Historical balance', acquisition_method='manual', document_type='holding')
                fields = {'kind':'holding','asset_type':'cash','currency':'CNY','business_date':day.isoformat(),'net_cash':'123.45','reconstruction_evidence':{'statement_source_item_id':str(original_item)}}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
                values = load_position_nav_estimates(cur, accounts={'test_1234':{}}, dates=[day,date(2026,1,1)],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal('1'), rate_at=lambda *_: Decimal('1'))
                assert values[day]['test_1234'][0] == Decimal('123.45')
                assert 'test_1234' not in values.get(date(2026,1,1), {})
                from domain.investments.valuation_nav import _apply_historical_cash_points
                bucket = {day: {'assets':Decimal('200'), 'liabilities':Decimal('0'), 'net_worth':Decimal('200'),
                                'account_values':{'test_1234':Decimal('200')}, 'available_accounts':['test_1234']}}
                _apply_historical_cash_points(cur, bank_series=bucket, accounts=[{'account_code':'test_1234', 'base_currency':'CNY', 'owner_ratio':Decimal('1')}],
                                              dates=[day], rate_at=lambda *_:Decimal('1'))
                assert bucket[day]['assets'] == bucket[day]['net_worth'] == Decimal('123.45')
                assert bucket[day]['available_accounts'] == ['test_1234']
        finally:
            conn.rollback()
