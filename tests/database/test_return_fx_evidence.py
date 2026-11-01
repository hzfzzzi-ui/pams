"""Historical returns require explicit same-day FX in an isolated database."""
# ruff: noqa: F811
from datetime import date,timedelta
from decimal import Decimal
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.investments import investment_return_data as data
from domain.investments import investment_returns as returns


def setup(conn,day):
    initialize_isolated_sources(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
            minor_category,asset_liability,market_value,cost,quantity,current_price,as_of) VALUES
            ('jianhang','CNY asset','CNY1','CNY',%s,%s,%s,100,90,1,100,%s),
            ('jianhang','USD asset','USD1','USD',%s,%s,%s,100,90,1,100,%s)""",
            ('\u8d35\u91d1\u5c5e','\u8d35\u91d1\u5c5e','\u8d44\u4ea7',day,
             '\u8d35\u91d1\u5c5e','\u8d35\u91d1\u5c5e','\u8d44\u4ea7',day))
        cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',6,'synthetic'),(%s,'CNY','USD',8,'synthetic')",
                    (day-timedelta(days=1),day+timedelta(days=1)))


def owner(alias): return ('1',[])


def test_category_snapshot_does_not_use_prior_or_future_fx(empty_source_db):
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,day)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            kw=dict(dates=[day],base_currency='CNY',major_category='\u8d35\u91d1\u5c5e',
                    owner_ratio_case_sql=owner,excluded_holding_accounts=set())
            assert data._snapshot_category_value_series(cur,**kw)[day] is None
            assert data._exact_snapshot_category_value(cur,as_of=day,base_currency='CNY',
                major_category='\u8d35\u91d1\u5c5e',owner_ratio_case_sql=owner,excluded_holding_accounts=set()) is data.FX_UNAVAILABLE
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'",(day,))
            assert data._snapshot_category_value_series(cur,**kw)[day] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'",(day,))
            assert data._snapshot_category_value_series(cur,**kw)[day]==Decimal('800')
            assert data._exact_snapshot_category_value(cur,as_of=day,base_currency='CNY',
                major_category='\u8d35\u91d1\u5c5e',owner_ratio_case_sql=owner,excluded_holding_accounts=set())==Decimal('800')
            cur.execute("UPDATE pams_fx_rates_daily SET rate='NaN' WHERE as_of=%s AND base_currency='CNY' AND quote_currency='USD'",(day,))
            assert data._snapshot_category_value_series(cur,**kw)[day] is None


def test_exact_fx_identity_and_unknown_are_separate():
    day=date(2026,10,22)
    rates={'USD':[(day-timedelta(days=1),Decimal(6)),(day+timedelta(days=1),Decimal(8))]}
    assert data.exact_fx_rate_at(rates,'CNY',day,'CNY')==1
    assert data.exact_fx_rate_at(rates,'USD',day,'CNY') is None
    assert data.exact_fx_rate_at(rates,None,day,'CNY') is None
    rates['USD'].append((day,Decimal('7.12345678')))
    assert data.exact_fx_rate_at(rates,'USD',day,'CNY')==Decimal('7.12345678')


def test_real_return_payloads_propagate_missing_fx_and_keep_known_values(empty_source_db):
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,day)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            args=dict(start_date=day,interval='daily',base_currency='CNY',view_as_of=day,
                reported_returns_cfg={},overview_payload={'accounts':[]},owner_ratio_case_sql=owner,
                account_owner_ratio=lambda account:Decimal(1),excluded_holding_accounts=set())
            section=returns.build_section_return_series_payload(cur,section='precious_metal',**args)
            assert section['status']=='unavailable' and section['reason']=='fx_rate_unknown'
            assert section['rows'][0]['market_value'] is None
            total=returns.build_investment_return_series_payload(cur,**args,account_initial_capital_cny=lambda account:None)
            assert total['status']=='unavailable' and total['reason']=='fx_rate_unknown'
            assert total['rows'][0]['market_value'] is None and total['rows'][0]['total_pnl'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            section=returns.build_section_return_series_payload(cur,section='precious_metal',**args)
            assert section.get('status')!='unavailable'
            assert Decimal(section['rows'][0]['market_value'])==Decimal('800')


@pytest.mark.parametrize("offset",[1,3])
def test_ibkr_today_window_requires_exact_current_and_previous_fx(empty_source_db,offset):
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of) VALUES
                ('ibkr','Synthetic','SYN','USD',%s,%s,%s,90,80,10,9,%s),
                ('ibkr','Synthetic','SYN','USD',%s,%s,%s,100,80,10,10,%s)""",
                ('\u7f8e\u80a1','\u7f8e\u80a1','\u8d44\u4ea7',day-timedelta(days=offset),
                 '\u7f8e\u80a1','\u7f8e\u80a1','\u8d44\u4ea7',day))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result=returns._ibkr_holdings_today_window_payload(cur,base_currency='CNY',label_date=day,view_as_of=day)
            assert result['status']=='unavailable' and result['total_return'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',6,'synthetic')",(day-timedelta(days=offset),))
            result=returns._ibkr_holdings_today_window_payload(cur,base_currency='CNY',label_date=day,view_as_of=day)
            assert Decimal(result['total_return'])==Decimal('160')
            assert Decimal(result['return_rate'])==Decimal('160')/Decimal('540')


def test_real_trade_flow_does_not_borrow_other_day_fx(empty_source_db):
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,day)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,symbol,currency,market,
                trade_type,side,gross_amount,fee_amount,tax_amount,net_cash,source_table,source_id,source_hash)
                VALUES (%s,'jianhang','USD1','USD',%s,'buy','buy',100,0,0,-100,'synthetic','fx-test',%s)""",
                (day,'\u8d35\u91d1\u5c5e','a'*64))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            rates={'USD':[(day-timedelta(days=1),Decimal(6)),(day+timedelta(days=1),Decimal(8))]}
            kw=dict(start_date=day,end_date=day,base_currency='CNY',fx_timelines=rates,
                    account_owner_ratio=lambda account:Decimal(1),major_category='\u8d35\u91d1\u5c5e')
            events=data._investment_transaction_flow_events(cur,**kw)
            assert events==[(day,None)] and events.missing_fx
            rates['USD'].append((day,Decimal(7)))
            events=data._investment_transaction_flow_events(cur,**kw)
            assert events==[(day,Decimal(700))] and not events.missing_fx
