"""Historical position FX stays unknown without same-day evidence."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.investments import holdings_positions as positions

D1=date(2026,10,20)
D2=date(2026,10,21)
D3=date(2026,10,22)

def trade(cur, *, ident, day, symbol, kind, gross, qty, currency='USD', net=None, realized=None, category='美股'):
    cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,symbol,name,currency,market,
       asset_major_category,asset_minor_category,trade_type,side,quantity,gross_amount,fee_amount,tax_amount,
       net_cash,realized_pnl,fx_rate_to_cny,source_table,source_id,source_hash)
       VALUES (%s,'ibkr',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s,1,'synthetic',%s,%s)""",
       (day,symbol,symbol,currency,category,category,category,kind,kind,qty,gross,net,realized,ident,ident.ljust(64,'x')))

def rate(cur,day,ccy,value,source='synthetic'):
    cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY',%s,%s,%s)",(day,ccy,value,source))

def holding(symbol='SYN',currency='USD',cost=100,market=110):
    return {'account':'ibkr','symbol':symbol,'name':symbol,'currency':currency,'as_of':D2,
       'major_category':'美股','minor_category':'美股','market_value':Decimal(market),'market_value_cny':None,
       'cost':Decimal(cost),'cost_cny':None,'holding_pnl':Decimal(market-cost),'holding_pnl_native':Decimal(market-cost),
       'holding_pnl_cny':None}

def test_replay_keeps_native_and_blocks_fabricated_cny(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='buy',day=D1,symbol='SYN',kind='buy',gross=100,qty=10,net=-100)
            trade(cur,ident='sell',day=D2,symbol='SYN',kind='sell',gross=60,qty=5,net=60)
            rate(cur,D3,'USD',9)
            row=holding(cost=50,market=70)
            out=positions._enrich_realized_trade_pnl(cur,[row])[0]
            assert out['realized_trade_pnl_native']==Decimal('10')
            assert out['realized_trade_pnl_cny'] is None and out['holding_pnl_cny'] is None
            rate(cur,D1,'USD',7)
            rate(cur,D2,'USD',8,source='')
            row=holding(cost=50,market=70)
            out=positions._enrich_realized_trade_pnl(cur,[row])[0]
            assert out['realized_trade_pnl_native']==Decimal('10')
            assert out['realized_trade_pnl_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE as_of=%s AND quote_currency='USD'",(D2,))
            out=positions._enrich_realized_trade_pnl(cur,[holding(cost=50,market=70)])[0]
            assert out['realized_trade_pnl_cny']==Decimal('130')

def test_explicit_realized_and_esop_income_preserve_unknown_fx(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='realized',day=D2,symbol='SYN',kind='sell',gross=60,qty=5,net=60,realized=10)
            trade(cur,ident='dividend',day=D2,symbol='ESOP1',kind='dividend',gross=5,qty=None,net=5,category='ESOP')
            rate(cur,D1,'USD',6)
            rate(cur,D3,'USD',9)
            row=holding(cost=50,market=70)
            out=positions._enrich_realized_trade_pnl(cur,[row])[0]
            assert out['realized_trade_pnl_native']==Decimal('10')
            assert out['realized_trade_pnl_cny'] is None
            esop=holding(symbol='ESOP1')
            esop['major_category']=esop['minor_category']='ESOP'
            out=positions._enrich_esop_realized_income(cur,[esop])[0]
            assert out['realized_income_native']==Decimal('5') and out['realized_income_cny'] is None
            rate(cur,D2,'USD',8)
            out=positions._enrich_realized_trade_pnl(cur,[holding(cost=50,market=70)])[0]
            assert out['realized_trade_pnl_cny']==Decimal('80')
            esop=holding(symbol='ESOP1'); esop['major_category']=esop['minor_category']='ESOP'
            out=positions._enrich_esop_realized_income(cur,[esop])[0]
            assert out['realized_income_cny']==Decimal('40')

def test_buy_and_sell_facts_do_not_establish_a_closed_position(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='cbuy',day=D1,symbol='CLOSED',kind='buy',gross=100,qty=10,net=-100)
            trade(cur,ident='csell',day=D2,symbol='CLOSED',kind='sell',gross=120,qty=10,net=120)
            rate(cur,D1,'USD',7); rate(cur,D2,'USD',8)
            assert positions._closed_position_rows(cur)==[]


def test_unverified_zero_holding_does_not_establish_an_ibkr_exit(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,market_value,cost,quantity)
                VALUES ('ibkr','Synthetic','SYN','USD',%s,%s,%s,%s,0,0,0)""",
                ('\u8d44\u4ea7','\u7f8e\u80a1','\u7f8e\u80a1',D2))
        conn.commit()
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert positions._ibkr_closed_position_rows(cur)==[]


def test_mixed_currency_rows_do_not_create_partial_aggregate(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='usd',day=D2,symbol='SYN',kind='sell',gross=60,qty=5,net=60,realized=10)
            trade(cur,ident='eur',day=D2,symbol='SYN',kind='sell',gross=50,qty=5,net=50,realized=7,currency='EUR')
            rate(cur,D2,'USD',8)
            row=holding(cost=50,market=70)
            out=positions._enrich_realized_trade_pnl(cur,[row])[0]
            assert out['realized_trade_pnl_native'] is None
            assert out['realized_trade_pnl_cny'] is None
            assert out['realized_trade_pnl_source']=='unavailable_currency_identity'


def test_base_currency_is_identity_and_invalid_rate_is_unknown(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='cny',day=D2,symbol='CNY',kind='sell',gross=60,qty=5,net=60,realized=10,currency='CNY')
            trade(cur,ident='bad',day=D2,symbol='BAD',kind='sell',gross=60,qty=5,net=60,realized=10)
            rate(cur,D2,'USD','NaN')
            cny=holding(symbol='CNY',currency='CNY',cost=50,market=70)
            bad=holding(symbol='BAD',cost=50,market=70)
            rows=positions._enrich_realized_trade_pnl(cur,[cny,bad])
            assert rows[0]['realized_trade_pnl_cny']==10
            assert rows[1]['realized_trade_pnl_native']==10 and rows[1]['realized_trade_pnl_cny'] is None


def test_esop_observation_after_snapshot_is_not_included(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='early',day=D1,symbol='E',kind='dividend',gross=5,qty=None,net=5,category='ESOP')
            trade(cur,ident='future',day=D3,symbol='E',kind='dividend',gross=100,qty=None,net=100,category='ESOP')
            rate(cur,D1,'USD',7)
            rate(cur,D3,'USD',9)
            row=holding(symbol='E'); row['major_category']=row['minor_category']='ESOP'
            result=positions._enrich_esop_realized_income(cur,[row])[0]
            assert result['realized_income_native']==5 and result['realized_income_cny']==35


def test_mixed_known_and_unknown_fx_do_not_expose_partial_sum(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            trade(cur,ident='one',day=D1,symbol='SYN',kind='sell',gross=60,qty=5,net=60,realized=10)
            trade(cur,ident='two',day=D2,symbol='SYN',kind='sell',gross=50,qty=5,net=50,realized=7)
            rate(cur,D1,'USD',7)
            out=positions._enrich_realized_trade_pnl(cur,[holding(cost=50,market=70)])[0]
            assert out['realized_trade_pnl_native']==17
            assert out['realized_trade_pnl_cny'] is None


def test_unlocked_holding_proof_requires_readonly_repeatable_read(empty_source_db):
    import pytest
    from domain.investments.valuation_input_evidence import _bound_input
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError,match='read-only repeatable-read'):
                _bound_input(cur,'holding',{'id':1},locking=False)
        conn.rollback()
        conn.set_session(readonly=True,isolation_level='READ COMMITTED')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError,match='read-only repeatable-read'):
                _bound_input(cur,'holding',{'id':1},locking=False)
