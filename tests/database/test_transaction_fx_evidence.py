"""Trade list FX evidence, using only a disposable bootstrapped database."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
import pytest
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from endpoints import investment_transactions as endpoint


def _publish_synthetic_trades(conn, rows):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.imports import source_records_v2 as source
    source_id = uuid4()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                             acquisition_method='manual', document_type='trade')
        for index, fields in enumerate(rows):
            source.add_item(cur, source_id=source_id, item_id=uuid4(), parse_method='manual',
                            raw_fields={'ordinal': index}, parsed_fields=fields)
        cur.execute('SELECT source_item_id,parsed_fields FROM source_items WHERE source_id=%s ORDER BY source_item_id', (str(source_id),))
        items = cur.fetchall()
        revision = len(items) + 1
        for item in items:
            revision = source.review_item(cur, source_id=source_id, item_id=item['source_item_id'],
                                          expected_revision=revision, parsed_fields=item['parsed_fields'],
                                          review_status='confirmed')
        source.publish_trade_source(cur, source_id=source_id, expected_revision=revision)



def _publish_synthetic_cashflow(conn, *, account, fields, classification):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.imports import source_records_v2 as source
    source_id, item_id = uuid4(), uuid4()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        source.create_source(cur, source_id=source_id, account_code=account, source_name='synthetic',
                             acquisition_method='manual', document_type='cash')
        source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                        raw_fields={'row': 'synthetic'}, parsed_fields=fields)
        revision = source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                                      parsed_fields=fields, review_status='confirmed')
        source.publish_cash_source(cur, source_id=source_id, expected_revision=revision)
        cur.execute("""UPDATE cashflow_classification SET cashflow_type=%s,status='confirmed',
                    classification_source='manual' WHERE cashflow_id=(SELECT cashflow_id
                    FROM pams_cashflow_fact WHERE source_item_id=%s)""", (classification, str(item_id)))


def test_investment_cashflow_keeps_native_amount_when_fx_missing(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_account_rows, _account_summary, _serialize_row
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _publish_synthetic_trades(conn, [{'business_date': str(day), 'trade_type': 'deposit',
                'currency': 'USD', 'gross_amount': '100', 'net_cash': '100'}])
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')", (day+timedelta(days=1),))
            rows = _load_investment_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount']==100 and rows[0]['amount_cny'] is None
            assert _serialize_row(rows[0])['amount_cny'] is None
            summary = _account_summary(rows, {'tonghuashun'})[0]
            assert summary['fx_status']=='incomplete' and summary['investment_inflow'] is None and summary['net'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (day,))
            rows = _load_investment_account_rows(cur,start=day,end=day,base_currency='CNY')
            assert rows[0]['amount_cny']==700 and _account_summary(rows, {'tonghuashun'})[0]['investment_inflow']==700


def test_transfer_summary_never_uses_a_future_fx_rate(empty_source_db, monkeypatch):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_transfer_matches, _load_pending_transfer_cases, _transfer_summary, summarize_period
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            from domain.ledger import confirmed_transfers_v2
            common = {'from_account_code':'bank_a', 'to_account_code':'ibkr', 'amount':Decimal('100'),
                      'currency':'USD', 'from_evidence':{'kind':'cashflow','id':1,'day':str(day)},
                      'to_evidence':{'kind':'trade','id':2,'day':str(day)}}
            records = [{**common, 'transfer_id':'valid','evidence_status':'confirmed'},
                       {**common, 'transfer_id':'invalid','evidence_status':'invalidated'}]
            monkeypatch.setattr(confirmed_transfers_v2, 'read', lambda cur, limit=None: records)
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')",(day+timedelta(days=1),))
            rows=_load_transfer_matches(cur,start=day,end=day,base_currency='CNY')
            assert len(rows)==1 and rows[0]['amount_cny'] is None
            pending=_load_pending_transfer_cases(cur,start=day,end=day)
            assert len(pending)==1 and pending[0]['status']=='invalidated' and pending[0]['amount'] is None
            rows[0]['match_date_obj']=day
            summary=summarize_period([],rows,day,day)
            assert summary['internal_transfer'] is None and summary['investment_transfer'] is None
            assert _transfer_summary(rows)[0]['amount'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            rows=_load_transfer_matches(cur,start=day,end=day,base_currency='CNY')
            assert rows[0]['amount_cny']==700 and _transfer_summary(rows)[0]['amount']==700


def test_only_verified_fact_link_deduplicates_investment_funding(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_account_rows
    from domain.ledger.confirmed_transfers_v2 import confirm
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank')")
        _publish_synthetic_cashflow(conn, account='test_bank', fields={'business_date': str(day),
            'net_cash': '-100', 'currency': 'USD'}, classification='investment_funding')
        _publish_synthetic_trades(conn, [
            {'business_date': str(day), 'trade_type': 'deposit', 'currency': 'USD', 'net_cash': '100'},
            {'business_date': str(day), 'trade_type': 'deposit', 'currency': 'USD', 'net_cash': '101'},
        ])
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code='test_bank'")
            cash_id = cur.fetchone()['cashflow_id']
            cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE net_cash=100")
            trade_id = cur.fetchone()['trade_id']
            confirm(cur, from_kind='cashflow', from_id=cash_id, to_kind='trade', to_id=trade_id,
                    note='synthetic reviewed transfer', confirmed=True)
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rows = _load_investment_account_rows(cur, start=day, end=day, base_currency='CNY')
            assert len(rows) == 2 and all(row['amount_cny'] is None for row in rows)
            assert next(row for row in rows if row['fact_id'] == trade_id)['linked_cashflow_id'] == cash_id
            assert next(row for row in rows if row['fact_id'] != trade_id)['linked_cashflow_id'] is None
            cur.execute('UPDATE pams_cashflow_fact SET amount=-99 WHERE cashflow_id=%s', (cash_id,))
            rows = _load_investment_account_rows(cur, start=day, end=day, base_currency='CNY')
            assert all(row['linked_cashflow_id'] is None for row in rows)


def test_investment_funding_requires_published_matching_source(empty_source_db):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.imports import source_records_v2 as source
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            source_id, item_id = uuid4(), uuid4()
            source.create_source(cur, source_id=source_id, account_code='tonghuashun', source_name='synthetic',
                                 acquisition_method='manual', document_type='trade')
            fields = {'business_date': str(day), 'trade_type': 'deposit', 'currency': 'CNY', 'net_cash': '100'}
            source.add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'row': 'synthetic'}, parsed_fields=fields)
            cur.execute('SAVEPOINT pending_item')
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,
                    net_cash,source_item_id,fact_role) VALUES (%s,'tonghuashun','CNY','deposit',100,%s,'execution')""",
                    (day, str(item_id)))
            cur.execute('ROLLBACK TO SAVEPOINT pending_item')
            source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                               parsed_fields=fields, review_status='confirmed')
            cur.execute('SAVEPOINT wrong_account')
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,
                    net_cash,source_item_id,fact_role) VALUES (%s,'ibkr','CNY','deposit',100,%s,'execution')""",
                    (day, str(item_id)))
            cur.execute('ROLLBACK TO SAVEPOINT wrong_account')
            cur.execute('SAVEPOINT draft_source')
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,
                net_cash,source_item_id,fact_role) VALUES (%s,'tonghuashun','CNY','deposit',100,%s,'execution')""",
                (day, str(item_id)))
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cur.execute('ROLLBACK TO SAVEPOINT draft_source')
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 0


def test_investment_funding_direction_conflict_does_not_reverse_cash(empty_source_db):
    from psycopg2.extras import RealDictCursor
    from domain.ledger.ledger_view import _load_investment_account_rows
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        _publish_synthetic_trades(conn, [{'business_date': str(day), 'trade_type': 'deposit',
            'currency': 'CNY', 'gross_amount': '100', 'net_cash': '100'}])
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE pams_investment_trade_fact SET net_cash=-100 WHERE trade_type='deposit'")
            rows = _load_investment_account_rows(cur, start=day, end=day, base_currency='CNY')
            assert len(rows) == 1
            assert rows[0]['source_evidence_status'] == 'unknown'
            assert rows[0]['amount'] is None and rows[0]['amount_cny'] is None
            assert rows[0]['needs_review'] is True


def test_ledger_foreign_expense_keeps_native_value_and_blocks_period_totals(empty_source_db, monkeypatch):
    from psycopg2.extras import RealDictCursor
    from domain.ledger import ledger_view
    day=date(2026,10,22)
    monkeypatch.setattr(ledger_view, '_load_accounts_config', lambda: {'accounts': {'test_bank': {'account_type':'funding'}}})
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank') ON CONFLICT DO NOTHING")
            _publish_synthetic_cashflow(conn, account='test_bank', fields={'business_date': str(day),
                'net_cash': '-100', 'currency': 'USD'}, classification='operating_expense')
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')",(day+timedelta(days=1),))
            payload=ledger_view.build_ledger_workbench_payload(cur,month='2026-10')
            assert payload['periods']['month']['expense'] is None
            assert payload['periods']['month']['fx_status']=='incomplete'
            row=next(row for row in payload['recent_rows'] if row['account_code']=='test_bank')
            assert row['amount']==-100 and row['amount_cny'] is None
            assert row['recorded_amount_cny'] is None and row['fx_basis']=='unavailable'
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            payload=ledger_view.build_ledger_workbench_payload(cur,month='2026-10')
            assert payload['periods']['month']['expense']==700
            assert payload['periods']['month']['fx_status']=='estimated'


def test_empty_and_fact_rows_keep_amounts_and_require_trade_day_fx(empty_source_db, monkeypatch):
    day = date(2026, 10, 22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(endpoint, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(endpoint, '_load_accounts_config', lambda: {})
    app = FastAPI()
    app.include_router(endpoint.router, prefix='/api')
    with TestClient(app) as web:
        empty = web.get('/api/investment-transactions')
        assert empty.status_code == 200, empty.text
        assert empty.json()['rows'] == []
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            _publish_synthetic_trades(conn, [
                {'business_date': str(day), 'trade_type': 'buy', 'symbol': 'USD1', 'currency': 'USD',
                 'quantity': '10', 'price': '10', 'gross_amount': '100', 'fee_amount': '2', 'tax_amount': '0', 'net_cash': '-102'},
                {'business_date': str(day), 'trade_type': 'sell', 'symbol': 'CNY1', 'currency': 'CNY',
                 'quantity': '1', 'price': '50', 'gross_amount': '50', 'fee_amount': '1', 'tax_amount': '0', 'net_cash': '49'},
            ])
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate)
                           VALUES (%s,'CNY','USD',6),(%s,'CNY','USD',8)""", (day-timedelta(days=1), day+timedelta(days=1)))
        response = web.get('/api/investment-transactions')
        assert response.status_code == 200, response.text
        data = response.json()
        rows = {row['symbol']: row for row in data['rows']}
        usd = rows['USD1']
        assert float(usd['gross_amount']) == 100 and float(usd['net_cash']) == -102
        assert usd['fx_rate'] is None and usd['gross_amount_cny'] is None and usd['net_cash_cny'] is None and usd['fees_cny'] is None
        assert data['summary']['buy_amount_cny'] is None and data['summary']['buy_amount_personal_cny'] is None
        assert float(data['summary']['sell_amount_cny']) == 50 and data['summary']['fees_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(day,))
        data = web.get('/api/investment-transactions').json()
        usd = next(row for row in data['rows'] if row['symbol'] == 'USD1')
        assert usd['fx_rate'] is None and usd['gross_amount_cny'] is None
        with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
        data = web.get('/api/investment-transactions').json()
        usd = next(row for row in data['rows'] if row['symbol'] == 'USD1')
        assert float(usd['gross_amount_cny']) == 700 and float(usd['net_cash_cny']) == -714 and float(usd['fees_cny']) == 14
        assert float(data['summary']['buy_amount_cny']) == 700 and float(data['summary']['fees_cny']) == 15


def test_zero_trade_amounts_remain_zero_without_fx(empty_source_db,monkeypatch):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        _publish_synthetic_trades(conn, [{'business_date': '2026-10-22', 'trade_type': 'dividend',
            'symbol': 'ZERO', 'currency': 'JPY', 'gross_amount': '0', 'fee_amount': '0',
            'tax_amount': '0', 'net_cash': '0'}])
    monkeypatch.setattr(endpoint,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(endpoint,'_load_accounts_config',lambda:{})
    app=FastAPI(); app.include_router(endpoint.router,prefix='/api')
    with TestClient(app) as web:
        response=web.get('/api/investment-transactions')
        assert response.status_code==200,response.text
        row=response.json()['rows'][0]
        assert row['fx_rate'] is None
        assert Decimal(str(row['gross_amount_cny']))==0
        assert Decimal(str(row['net_cash_cny']))==0
        assert Decimal(str(row['fees_cny']))==0
