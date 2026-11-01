"""Terminal NAV requires complete numeric evidence before overriding a history row."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


@pytest.mark.parametrize('bucket,expected',[
    ({'assets':None,'liabilities':None,'net_worth':None,'scope_status':'incomplete','unavailable_accounts':['demo']},None),
    ({'assets':Decimal(100),'liabilities':Decimal(0),'net_worth':None},None),
    ({'assets':Decimal(100),'liabilities':Decimal(0),'net_worth':Decimal(100),'fx_status':'unavailable'},None),
    ({'assets':Decimal(100),'liabilities':Decimal(0),'net_worth':Decimal(100),'scope_status':'unknown'},None),
    ({'assets':Decimal('NaN'),'liabilities':Decimal(0),'net_worth':Decimal(0)},None),
    ({'assets':Decimal(0),'liabilities':Decimal(0),'net_worth':Decimal(0)},Decimal(0)),
    ({'assets':Decimal(250),'liabilities':Decimal(40),'net_worth':Decimal(210)},Decimal(210)),
])
def test_actual_postgres_nav_terminal_evidence(empty_source_db,bucket,expected):
    from domain.investments import valuation_nav
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            payload=valuation_nav.build_nav_weekly_payload(cur,start_date=day,terminal_date=day,
                base_currency='CNY',overview_payload={'accounts':[]},
                owner_ratio_case_sql=lambda alias:('%s',[Decimal(1)]),account_owner_ratio=lambda account:Decimal(1),
                include_ibkr=False,interval='daily',terminal_nav_bucket=bucket)
            row=payload['rows'][-1]
            assert (Decimal(row['netWorth']) if row['netWorth'] is not None else None)==expected,row
            if expected is None:
                assert row['assets'] is None and row['liabilities'] is None
                assert row['scope_status']=='incomplete'


def test_nav_http_does_not_display_zero_when_terminal_fx_unknown(empty_source_db,monkeypatch):
    import main
    from endpoints import accounts
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:{'accounts':{'ibkr':{'include_in_nav':False}}})
    monkeypatch.setattr(main,'_load_account_balances',lambda *a:[])
    monkeypatch.setattr(main,'_load_investment_positions',lambda *a,**kw:[])
    monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda *a,**kw:None)
    monkeypatch.setattr(main,'_current_nav_bucket_from_parts',lambda **kw:{'assets':None,'liabilities':None,
        'net_worth':None,'scope_status':'incomplete','unavailable_accounts':['demo']})
    monkeypatch.setattr(main,'_current_nav_account_values_from_parts',lambda **kw:{})
    monkeypatch.setattr(accounts,'_overview_payload',lambda **kwargs:{'accounts':[]})
    with TestClient(main.app) as web:
        response=web.get('/nav/weekly',params={'start':day.isoformat(),'latest':'true','interval':'daily'})
        assert response.status_code==200,response.text
        row=response.json()['rows'][-1]
        assert row['netWorth'] is None and row['assets'] is None and row['liabilities'] is None,row
        assert row['scope_status']=='incomplete' and 'demo' in row['unavailable_accounts']
