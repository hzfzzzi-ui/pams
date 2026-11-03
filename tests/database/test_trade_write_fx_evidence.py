"""Investment trade write FX and direction against a disposable empty database."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_transaction_fx_evidence import _publish_synthetic_trades
from domain.investments.transaction_list_v2 import list_rows

DAY = date(2026, 10, 22)


def deposit(**changes):
    return {'business_date': str(DAY), 'trade_type': 'deposit', 'currency': 'USD',
            'gross_amount': '100', 'net_cash': '100', **changes}


def test_unverified_fx_stays_unknown_and_forgery_is_rejected(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')", (DAY + timedelta(days=1),))
            _publish_synthetic_trades(conn, [deposit(fx_rate_to_cny='7', net_cash_cny='1'),
                                            deposit(currency='CNY')])
            rows = {row['currency']: row for row in list_rows(cur, start=DAY, end=DAY)}
            assert rows['USD']['net_cash'] == 100 and rows['USD']['net_cash_cny'] is None
            assert rows['CNY']['net_cash_cny'] == 100
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (DAY,))
            rows = {row['currency']: row for row in list_rows(cur, start=DAY, end=DAY)}
            assert rows['USD']['net_cash_cny'] == 700
            for fields in (deposit(net_cash='-100'), deposit(trade_type='withdraw')):
                with pytest.raises(ValueError, match='trade required fields invalid') as exc:
                    _publish_synthetic_trades(conn, [fields])
                assert 'cash direction' in str(exc.value.__cause__)
            _publish_synthetic_trades(conn, [deposit(trade_type='withdraw', net_cash='-100')])
            amounts = {row['trade_type']: row['net_cash_cny'] for row in
                       list_rows(cur, start=DAY, end=DAY) if row['currency'] == 'USD'}
            assert amounts == {'deposit': 700, 'withdraw': -700}


def test_equity_bridge_marks_unverified_funding_and_cashflow_unknown(empty_source_db):
    from domain.investments.portfolio_equity_bridge import _cashflow_breakdown, _funding_closure_checks
    from domain.ledger.confirmed_transfers_v2 import confirm
    from tests.database.test_transaction_fx_evidence import _publish_synthetic_cashflow, _publish_synthetic_trades

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('bank_test','Synthetic bank')")
            _publish_synthetic_cashflow(conn, account='bank_test', classification='investment_funding',
                fields={'business_date': str(DAY), 'currency': 'USD', 'net_cash': '-100'})
            _publish_synthetic_trades(conn, [{'business_date': str(DAY), 'trade_type': 'deposit',
                'currency': 'USD', 'gross_amount': '100', 'net_cash': '100'}], account_code='ibkr')
            cur.execute("SELECT cashflow_id FROM pams_cashflow_fact")
            cashflow_id = cur.fetchone()['cashflow_id']
            cur.execute("SELECT trade_id FROM pams_investment_trade_fact")
            trade_id = cur.fetchone()['trade_id']
            period = dict(start_date=DAY, end_date=DAY)
            assert _cashflow_breakdown(cur, **period)['external_net_cny'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')", (DAY,))
            assert _funding_closure_checks(cur, **period, account_cfg={})[0]['gap_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s", (DAY,))
            assert _cashflow_breakdown(cur, **period)['external_net_cny'] is None
            assert _funding_closure_checks(cur, **period, account_cfg={})[0]['status'] == 'unknown'
            confirm(cur, from_kind='cashflow', from_id=cashflow_id, to_kind='trade', to_id=trade_id,
                    note='synthetic independently reviewed pair', confirmed=True)
            assert _cashflow_breakdown(cur, **period)['external_net_cny'] == 0
            check = _funding_closure_checks(cur, **period, account_cfg={})[0]
            assert check['status'] == 'ok' and check['gap_cny'] == 0
            assert check['cashflow_side_cny'] == check['investment_trade_side_cny'] == 700
            cur.execute("UPDATE pams_cashflow_fact SET amount=-99 WHERE cashflow_id=%s", (cashflow_id,))
            assert _cashflow_breakdown(cur, **period)['external_net_cny'] is None
            assert _funding_closure_checks(cur, **period, account_cfg={})[0]['status'] == 'unknown'


def test_trade_attribution_keeps_known_percent_but_not_unverified_cny(empty_source_db):
    from tests.database.test_transaction_fx_evidence import _publish_synthetic_trades
    from domain.investments.trade_attribution import (_fetch_review_trades, _event_review_record,
        _summary_bucket, _add_metric_to_bucket, _finalize_bucket, _build_trade_asset_heatmap)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        _publish_synthetic_trades(conn, [{'business_date': str(DAY), 'trade_type': 'buy',
            'symbol': 'SYN', 'currency': 'USD', 'quantity': '10', 'price': '10',
            'gross_amount': '100', 'fee_amount': '2', 'tax_amount': '0', 'net_cash': '-102'}])
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade = _fetch_review_trades(cur, start=DAY, end=DAY, limit=20)[0]
            assert trade['fx_rate_to_cny'] is None and trade['net_cash_cny'] is None
            payload = {'side': 'buy', 'event_metrics': {'coverage': 'ok', 'current_return_pct': Decimal('10')}}
            record = _event_review_record(trade, payload)
            assert record['estimated_decision_effect_cny'] is None and record['fee_drag_cny'] is None
            bucket = _summary_bucket('synthetic')
            _add_metric_to_bucket(bucket, trade, payload)
            result = _finalize_bucket(bucket)
            assert result['avg_return_pct'] is not None and result['gross_exposure_cny'] is None
            assert result['estimated_decision_effect_cny'] is None and result['win_rate'] is None
            heatmap = _build_trade_asset_heatmap([record])
            assert heatmap['coverage_status'] == 'incomplete' and heatmap['estimated_decision_effect_cny'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')", (DAY,))
            assert _fetch_review_trades(cur, start=DAY, end=DAY, limit=20)[0]['fx_rate_to_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s", (DAY,))
            trade = _fetch_review_trades(cur, start=DAY, end=DAY, limit=20)[0]
            record = _event_review_record(trade, payload)
            assert record['estimated_decision_effect_cny'] == Decimal('71.4') and record['fee_drag_cny'] == Decimal('-14')


def test_exact_fx_precision_preserves_native_amount_and_rejects_duplicate(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',7.12345678,'synthetic')", (DAY,))
            amount = '0.1234567891'
            fields = deposit(currency='EUR', net_cash=amount, gross_amount=amount)
            _publish_synthetic_trades(conn, [fields])
            with pytest.raises(ValueError, match='potential duplicate trade'):
                _publish_synthetic_trades(conn, [fields])
            result, = list_rows(cur, start=DAY, end=DAY)
            assert result['net_cash'] == Decimal(amount)
            assert result['net_cash_cny'] == Decimal(amount) * Decimal('7.12345678')


def test_rollforward_cash_requires_published_reviewed_source(empty_source_db):
    from domain.accounts.account_balance import _cash_delta_cny, _cash_source_schema_available

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _publish_synthetic_trades(conn, [deposit(currency='CNY')], account_code='ibkr')
            assert _cash_source_schema_available(cur)
            assert _cash_delta_cny(cur, 'ibkr', DAY - timedelta(days=1), DAY) == 100
            cur.execute("UPDATE pams_investment_trade_fact SET net_cash=99 WHERE account_code='ibkr'")
            assert _cash_delta_cny(cur, 'ibkr', DAY - timedelta(days=1), DAY) is None
