"""FX trade identity is distinct from its raw buy/sell direction."""
# ruff: noqa: F811
from datetime import date,datetime,timezone
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML


def test_fx_conversion_verifies_raw_direction_without_inventing_cash_legs(empty_source_db,monkeypatch,tmp_path):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(ibkr,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    fx='<Trade accountId="U_TEST" symbol="USD.MXN" assetCategory="CASH" currency="MXN" tradeID="FX1" tradeDate="20261022" buySell="BUY" quantity="100" tradePrice="20" multiplier="1" tradeMoney="2000" ibCommission="-2" ibCommissionCurrency="USD" taxAmount="0" netCash="0" />'
    original=XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    raw={'value':original.replace('</FlexStatement>',fx+'</FlexStatement>')}
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw['value'])
    with TestClient(main.app) as web:
        saved=raw['value']
        raw['value']=saved.replace('symbol="USD.MXN" assetCategory="CASH"','symbol="USD.MXN" assetCategory="STK"')
        blocked=web.post('/api/ibkr/flex/sync',json={})
        assert blocked.status_code==500 and 'currency-pair trade requires explicit CASH' in blocked.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_investment_trade_fact'); assert cur.fetchone()[0]==0
            cur.execute('SELECT count(*) FROM pams_holdings_snapshot'); assert cur.fetchone()[0]==0
        raw['value']=saved
        response=web.post('/api/ibkr/flex/sync',json={})
        assert response.status_code==200,response.text
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE symbol='USD.MXN'")
            fact=cur.fetchone()
            assert fact['side']==fact['trade_type']=='fx_conversion'
            assert fact['quantity']==100 and fact['gross_amount']==2000 and fact['net_cash']==0
            assert fact['fee_amount']==Decimal('2') and fact['fee_currency']=='USD'
            assert fact['fx_rate_to_cny'] is None and fact['net_cash_cny'] is None
            proof=verify_ibkr_fact(cur,trade_id=fact['trade_id'])
            assert proof['status']=='partial',proof
            assert {'fx_conversion_legs','net_reconciliation'} <= set(proof['unknown_fields'])
            assert not {'side','trade_type','quantity','fee_currency'} & set(proof['unknown_fields'])
            cur.execute("SELECT count(*) AS n FROM pams_cashflow_fact")
            assert cur.fetchone()['n']==0
