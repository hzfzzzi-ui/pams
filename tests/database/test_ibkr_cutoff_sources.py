"""Cutoff preserves original report publication without historical fact backfill."""
# ruff: noqa: F811
from datetime import date,datetime,timezone
import psycopg2
import pytest
from fastapi.testclient import TestClient
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.brokers.ibkr.test_ibkr_sync_transaction import XML


@pytest.mark.parametrize('first_cutoff,expected_count',[('2026-10-22',0),(None,1)])
def test_report_cutoff_preserves_holdings_and_frozen_trade_selection(empty_source_db,monkeypatch,tmp_path,first_cutoff,expected_count):
    import main
    from collectors import ibkr_flex
    from domain.brokers.ibkr import ibkr_report_state
    from endpoints import ibkr
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
    if first_cutoff is None: monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
    else: monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER',first_cutoff)
    monkeypatch.setenv('PAMS_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(ibkr,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(ibkr,'_credentials',lambda:('token','query'))
    monkeypatch.setattr(ibkr_flex,'request_flex_reference',lambda *a,**k:{'reference_code':'TEST'})
    monkeypatch.setattr(ibkr_report_state,'utc_now',lambda:datetime(2026,10,22,22,tzinfo=timezone.utc))
    raw={'value':XML.replace('ibCommission="-1.25"','ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')}
    monkeypatch.setattr(ibkr_flex,'fetch_flex_statement',lambda *a,**k:raw['value'])
    with TestClient(main.app) as web:
        response=web.post('/api/ibkr/flex/sync',json={})
        assert response.status_code==200,response.text
        assert response.json()['trade_count']==expected_count
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT quantity,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
            assert cur.fetchone()==(2,381)
            cur.execute("SELECT trade_id,source_item_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            initial=cur.fetchall(); assert len(initial)==expected_count
        # A changed configuration must not add to or rewrite a published batch.
        if first_cutoff is None: monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-22')
        else: monkeypatch.delenv('PAMS_IBKR_CASH_EVENTS_AFTER',raising=False)
        replay=web.post('/api/ibkr/flex/sync',json={})
        assert replay.status_code==200,replay.text
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("SELECT trade_id,source_item_id FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
            assert cur.fetchall()==initial
        if first_cutoff is not None:
            monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER',first_cutoff)
            raw['value']=raw['value'].replace('tradeID="T1"','tradeID="T2"')
            changed=web.post('/api/ibkr/flex/sync',json={})
            assert changed.status_code==200,changed.text
            with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade'")
                assert cur.fetchone()[0]==0
                cur.execute("SELECT quantity,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr' AND symbol='TEST'")
                assert cur.fetchone()==(2,381)
