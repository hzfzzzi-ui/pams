"""Investment trade write FX and direction against a disposable empty database."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts

DAY = date(2026,10,22)

def row(key, currency='USD', side='deposit', amount='100', rate=None):
    return {'source_table':'synthetic', 'source_id':key, 'source_key':key, 'trade_date':DAY,
            'account_code':'ibkr', 'currency':currency, 'side':side, 'gross_amount':'100',
            'net_cash':amount, 'fx_rate_to_cny':rate}


def test_unverified_fx_stays_unknown_and_forgery_is_rejected(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',8,'synthetic')",(DAY+timedelta(days=1),))
            unknown=build_investment_trade_fact(row('one'))
            assert unknown['net_cash']==Decimal(100) and unknown['fx_rate_to_cny'] is None and unknown['net_cash_cny'] is None
            assert insert_investment_trade_facts(cur,[unknown],strict_source=True)==1
            cur.execute("SELECT net_cash,fx_rate_to_cny,net_cash_cny FROM pams_investment_trade_fact WHERE source_id='one'")
            stored=cur.fetchone()
            assert stored['net_cash']==100 and stored['fx_rate_to_cny'] is None and stored['net_cash_cny'] is None
            cny=build_investment_trade_fact(row('cny',currency='CNY'))
            assert insert_investment_trade_facts(cur,[cny],strict_source=True)==1
            assert cny['fx_rate_to_cny']==1 and cny['net_cash_cny']==100
            raw_with_unproven_rate=build_investment_trade_fact(row('wrong',rate='7'))
            assert insert_investment_trade_facts(cur,[raw_with_unproven_rate],strict_source=True)==1
            assert insert_investment_trade_facts(cur,[raw_with_unproven_rate],strict_source=True)==0
            cur.execute("SELECT net_cash,fx_rate_to_cny,net_cash_cny FROM pams_investment_trade_fact WHERE source_id='wrong'")
            missing=cur.fetchone()
            assert missing['net_cash']==100 and missing['fx_rate_to_cny'] is None and missing['net_cash_cny'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(DAY,))
            exact=build_investment_trade_fact(row('exact',rate='7'))
            assert insert_investment_trade_facts(cur,[exact],strict_source=True)==1
            assert exact['net_cash_cny']==700
            forged={**build_investment_trade_fact(row('forged',rate='7')), 'net_cash_cny':Decimal(100)}
            with pytest.raises(ValueError,match='converted trade amount'):
                insert_investment_trade_facts(cur,[forged],strict_source=True)
            with pytest.raises(ValueError,match='cash direction'):
                build_investment_trade_fact(row('bad-deposit',amount='-100'))
            with pytest.raises(ValueError,match='cash direction'):
                build_investment_trade_fact(row('bad-withdraw',side='withdraw',amount='100'))
            withdrawal=build_investment_trade_fact(row('withdraw',side='withdraw',amount='-100',rate='7'))
            assert insert_investment_trade_facts(cur,[withdrawal],strict_source=True)==1
            assert withdrawal['net_cash_cny']==-700


def test_equity_bridge_marks_unverified_funding_and_cashflow_unknown(empty_source_db):
    from domain.investments.portfolio_equity_bridge import _cashflow_breakdown, _funding_closure_checks
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('bank_test','Synthetic bank')")
            cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,counter_account_code,
                amount,currency,fx_rate_to_cny,amount_cny,cashflow_type,source_table,source_id,source_hash)
                VALUES (%s,'bank_test','ibkr',-100,'USD',1,-100,'investment_funding','synthetic','b','b')""",(DAY,))
            assert insert_investment_trade_facts(cur,[build_investment_trade_fact(row('unknown'))])==1
            breakdown=_cashflow_breakdown(cur,start_date=DAY,end_date=DAY)
            assert breakdown['external_net_cny'] is None
            checks=_funding_closure_checks(cur,start_date=DAY,end_date=DAY,account_cfg={})
            assert checks[0]['status']=='unknown' and checks[0]['gap_cny'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(DAY,))
            breakdown=_cashflow_breakdown(cur,start_date=DAY,end_date=DAY)
            assert breakdown['external_net_cny'] is None
            checks=_funding_closure_checks(cur,start_date=DAY,end_date=DAY,account_cfg={})
            assert checks[0]['status']=='unknown' and checks[0]['gap_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(DAY,))
            breakdown=_cashflow_breakdown(cur,start_date=DAY,end_date=DAY)
            assert breakdown['external_net_cny']==-700
            checks=_funding_closure_checks(cur,start_date=DAY,end_date=DAY,account_cfg={})
            assert checks[0]['status']=='ok' and checks[0]['gap_cny']==0


def test_trade_attribution_keeps_known_percent_but_not_unverified_cny(empty_source_db):
    from domain.investments.trade_attribution import (_trade_query, _event_review_record,
        _summary_bucket, _add_metric_to_bucket, _finalize_bucket, _build_trade_asset_heatmap)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Simulate legacy rows that contain a fabricated 1:1 CNY value.
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,symbol,currency,
              trade_type,side,gross_amount,net_cash,fee_amount,tax_amount,fx_rate_to_cny,net_cash_cny,
              source_table,source_id,source_hash) VALUES (%s,'ibkr','SYN','USD','buy','buy',100,-102,2,0,1,-102,'synthetic','attr','attr')""", (DAY,))
            cur.execute(_trade_query("WHERE f.source_id='attr'"))
            trade=dict(cur.fetchone())
            assert trade['fx_rate_to_cny'] is None and trade['net_cash_cny'] is None
            payload={'side':'buy','event_metrics':{'coverage':'ok','current_return_pct':Decimal('10')}}
            record=_event_review_record(trade,payload)
            assert record['estimated_decision_effect_cny'] is None and record['fee_drag_cny'] is None
            bucket=_summary_bucket('synthetic')
            _add_metric_to_bucket(bucket,trade,payload)
            result=_finalize_bucket(bucket)
            assert result['avg_return_pct'] is not None and result['gross_exposure_cny'] is None and result['estimated_decision_effect_cny'] is None
            assert result['win_rate'] is None
            heatmap=_build_trade_asset_heatmap([record])
            assert heatmap['coverage_status']=='incomplete' and heatmap['estimated_decision_effect_cny'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(DAY,))
            cur.execute(_trade_query("WHERE f.source_id='attr'"))
            assert cur.fetchone()['fx_rate_to_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(DAY,))
            cur.execute(_trade_query("WHERE f.source_id='attr'"))
            record=_event_review_record(dict(cur.fetchone()),payload)
            assert record['estimated_decision_effect_cny']==Decimal('71.4') and record['fee_drag_cny']==Decimal('-14')


def test_exact_fx_precision_rounds_and_replays_idempotently(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',7.12345678,'synthetic')", (DAY,))
            raw = {**row('precise',currency='EUR',amount='0.1234567891',rate='7.12345678'), 'gross_amount':'0.1234567891'}
            fact = build_investment_trade_fact(raw)
            assert insert_investment_trade_facts(cur,[fact],strict_source=True)==1
            assert insert_investment_trade_facts(cur,[fact],strict_source=True)==0
            cur.execute("SELECT net_cash_cny FROM pams_investment_trade_fact WHERE source_id='precise'")
            assert cur.fetchone()['net_cash_cny']==fact['net_cash_cny']


def test_rollforward_cash_requires_published_reviewed_source(empty_source_db):
    from uuid import uuid4
    from domain.accounts.account_balance import _cash_delta_cny, _cash_source_schema_available
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            batch, item = str(uuid4()), str(uuid4())
            cur.execute("INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method) VALUES (%s,'ibkr','trade','synthetic','manual')",(batch,))
            cur.execute("INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status) VALUES (%s,%s,'1','manual',%s,'confirmed')",(item,batch,'a'*64))
            fact=build_investment_trade_fact(row('source',currency='CNY'))
            assert insert_investment_trade_facts(cur,[fact])==1
            cur.execute("UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE source_id='source'",(item,))
            assert _cash_source_schema_available(cur)
            assert _cash_delta_cny(cur,'ibkr',DAY-timedelta(days=1),DAY) is None
            cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",('b'*64,batch))
            # A claimed publication without a matching immutable snapshot and
            # publication record is still not source-to-fact evidence.
            assert _cash_delta_cny(cur,'ibkr',DAY-timedelta(days=1),DAY) is None
