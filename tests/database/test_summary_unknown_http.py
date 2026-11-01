"""Published source and empty data remain honest on the summary HTTP endpoint."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
import psycopg2
from fastapi.testclient import TestClient

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.imports.test_account_import_publish import draft, holding, preview, publish


def test_summary_http_empty_then_published_holding_without_quote_is_unknown(empty_source_db,monkeypatch):
    import main
    from domain.imports import account_import_store as store
    day=date.today()-timedelta(days=2)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency) VALUES ('TEST_STOCK','000001','Synthetic','stock','CNY')")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(store,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:Decimal(1))
    # No configured accounts or published attribution exist at startup.
    monkeypatch.setattr(main,'_load_account_balances',lambda *args:[])
    monkeypatch.setattr(main,'_build_return_attribution_payload',lambda *args,**kwargs:None)
    monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda *args,**kwargs:None)
    with TestClient(main.app,base_url='http://localhost') as web:
        web.headers['Origin']='http://localhost'
        response=web.get('/summary2/latest')
        assert response.status_code==200,response.text
        assert all(response.json()['totals'][key] is None for key in ('market_value','holding_pnl','daily_pnl'))
        rows=[holding(business_date=day.isoformat(),quantity='100',market_value='1000',cost='900'),
              holding(business_date=day.isoformat(),asset_type='cash',symbol=None,action='balance',net_cash='0')]
        batch=draft(web,rows,completeness='complete')
        response=web.post('/api/account-import/batches/'+batch['batch_id']+'/confirm-scope',json={
            'revision':batch['revision'],'as_of':day.isoformat(),'member_count':2,
            'cash_amount':'0','note':'checked synthetic account holdings','confirmed':True})
        assert response.status_code==200,response.text
        batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
        check=preview(web,batch)
        assert check['can_publish'],check
        response=publish(web,batch,check)
        assert response.status_code==200,response.text
        response=web.get('/summary2/latest')
        assert response.status_code==200,response.text
        body=response.json()
        assert Decimal(body['totals']['market_value'])==Decimal(1000)
        assert body['totals']['daily_pnl'] is None
        assert body['daily_pnl_status']=='unknown'
        monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:day+timedelta(days=1))
        monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:day+timedelta(days=1))
        stale=web.get('/holdings/latest')
        assert stale.status_code==200,stale.text
        stock=next(row for row in stale.json()['rows'] if row.get('symbol')=='000001')
        assert stock['daily_pnl_stale'] and stock['daily_pnl_cny'] is None
        assert Decimal(stock['market_value_cny'])==Decimal(1000)
        stale_summary=web.get('/summary2/latest')
        assert stale_summary.status_code==200 and stale_summary.json()['totals']['daily_pnl'] is None
