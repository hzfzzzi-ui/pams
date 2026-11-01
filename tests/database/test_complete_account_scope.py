"""Explicit account scope attestation and member evidence."""
# ruff: noqa: F811
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from tests.database.test_native_holding_versions import isolated  # noqa: F401
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.imports.test_account_import_publish import DAY, client, draft, holding, preview, publish, sql  # noqa: F401
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


def confirm(web,batch,count=2,cash='50',**options):
    payload={'revision':batch['revision'],'as_of':DAY,'member_count':count,'cash_amount':cash,
             'note':'已逐项核对完整账户持仓及现金','confirmed':True}
    payload.update(options)
    return web.post('/api/account-import/batches/'+batch['batch_id']+'/confirm-scope',json=payload)


def test_explicit_complete_scope_binds_all_members_and_cash(isolated):
    web,params=isolated
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert preview(web,batch)['can_publish']
    assert confirm(web,batch,confirmed=False).status_code >= 400
    assert confirm(web,batch,cash='51').status_code >= 400
    assert confirm(web,batch,count=1).status_code >= 400
    scope=confirm(web,batch)
    assert scope.status_code==200, scope.text
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    check=preview(web,batch)
    assert check['can_publish'], check
    response=publish(web,batch,check)
    assert response.status_code==200,response.text
    assert publish(web,batch,check).json()['replayed']
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM account_snapshot_version WHERE account_code='tonghuashun'")
        version=cur.fetchone()
        assert version['coverage_status']=='complete' and version['cash_status']=='known' and version['cash_amount']==Decimal(50)
        assert str(version['source_batch_id'])==batch['batch_id']
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',version['as_of'])})
        assert coverage[('tonghuashun',version['as_of'])]['status']=='complete'
        assert coverage[('tonghuashun',version['as_of'])]['cash_status']=='known'
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            coverage=load_account_snapshot_coverage(cur,{('tonghuashun',version['as_of'])})
            assert coverage[('tonghuashun',version['as_of'])]['status']=='complete'
    # Any fact mutation invalidates the scope instead of silently keeping complete.
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE symbol='000001'")
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',version['as_of'])})
        assert coverage[('tonghuashun',version['as_of'])]['status']=='unknown'


def test_full_empty_database_complete_scope_enables_account_nav(empty_source_db, monkeypatch):
    from datetime import date
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scripts import bootstrap_isolated_sources as bootstrap
    from domain.imports import account_import_store as store
    from domain.investments import portfolio_state
    from domain.investments.valuation_nav import current_nav_bucket_from_parts
    from endpoints import account_import
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency)
                VALUES ('TEST_STOCK','000001','Synthetic stock','stock','CNY')""")
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','CNY',1,'synthetic')",(DAY,))
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    monkeypatch.setattr(store,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    app=FastAPI()
    app.include_router(account_import.router,prefix='/api')
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
        assert confirm(web,batch).status_code==200
        batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
        check=preview(web,batch)
        response=publish(web,batch,check)
        assert response.status_code==200,response.text
    cfg={'accounts':{'tonghuashun':{'account_type':'brokerage','truth_source':'platform_snapshot',
                                    'base_currency':'CNY','owner_ratio':'1','include_in_nav':True},
                     'ibkr':{'account_type':'brokerage','truth_source':'ibkr_flex','base_currency':'USD','include_in_nav':False}}}
    monkeypatch.setattr(portfolio_state,'_view_as_of',lambda:date.fromisoformat(DAY))
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        balances=portfolio_state.load_account_balances(cur,'CNY',cfg)
        positions=portfolio_state.load_investment_positions(cur,'CNY',cfg,account_balances=balances)
        from domain.accounts.account_balance import load_account_balance_basis
        basis=load_account_balance_basis(cur,account_code='tonghuashun',owner_ratio=Decimal(1))
        assert basis is not None and basis['net_gross']==Decimal('1050')
        from domain.investments.valuation_nav import _investment_snapshot_series
        proven_day=date.fromisoformat(DAY)
        historical=_investment_snapshot_series(cur,account_codes=['tonghuashun'],dates=[proven_day],
            base_currency='CNY',owner_ratio_case_sql=lambda alias:('1',[]))
        assert historical[proven_day]==Decimal('1050') and not historical.unknown_accounts_by_date
        from domain.investments.holdings_view import build_holdings_history_payload
        history=build_holdings_history_payload(cur,category='A股',days=1,interval='daily',minor_category=None,
            view_as_of=date.fromisoformat(DAY),tonghuashun_owner_ratio=Decimal(1),
            owner_ratio_sql='1',owner_ratio_params=[],excluded_accounts=[])
        assert history['rows'][-1]['value']==1000
        assert history['rows'][-1]['scope_status']=='complete_for_observed_accounts'
    account=next(row for row in balances if row['account_code']=='tonghuashun')
    assert account['snapshot_scope_status']=='complete'
    assert account['amount_cny']==Decimal('1050')
    bucket=current_nav_bucket_from_parts(account_balances=balances,investment_positions=positions)
    assert bucket['net_worth']==Decimal('1050')
    import main
    from domain.brokers.ibkr import ibkr_daily_close_read
    from endpoints import accounts
    day=date.fromisoformat(DAY)
    monkeypatch.setattr(ibkr_daily_close_read,'configured',lambda:False)
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main,'_holdings_market_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_excluded_holding_accounts',lambda:set())
    monkeypatch.setattr(main,'_account_owner_ratios',lambda:{})
    monkeypatch.setattr(main,'_default_owner_ratio',lambda:Decimal('1'))
    monkeypatch.setattr(main,'_view_as_of',lambda:day)
    monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:day)
    monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:cfg)
    monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda cur,**kwargs:None)
    monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal('1'))
    monkeypatch.setattr(accounts,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(accounts,'_load_accounts_config',lambda:cfg)
    monkeypatch.setattr(accounts,'_load_bank_config',lambda:{})
    monkeypatch.setattr(accounts,'_view_as_of',lambda:day)
    account_app=FastAPI()
    account_app.include_router(accounts.router,prefix='/api')
    with TestClient(account_app) as account_web:
        response=account_web.get('/api/accounts/overview')
        assert response.status_code==200,response.text
        account=next(r for r in response.json()['accounts'] if r['account_code']=='tonghuashun')
        assert Decimal(account['primary_balance']['amount'])==Decimal('1050')
    actual_overview=accounts._overview_payload
    monkeypatch.setattr(accounts,'_overview_payload',lambda cur=None:{'accounts':[]})
    with TestClient(main.app) as web:
        response=web.get('/holdings/latest')
        assert response.status_code==200,response.text
        rows={row['symbol']:row for row in response.json()['rows']}
        assert float(rows['000001']['market_value'])==1000
        assert rows['000001']['snapshot_scope_status']=='complete'
        nav=web.get('/nav/weekly',params={'start':DAY,'baseCurrency':'CNY','latest':'true','interval':'daily'})
        assert nav.status_code==200,nav.text
        assert Decimal(nav.json()['rows'][-1]['netWorth'])==Decimal('1050')
        monkeypatch.setattr(accounts,'_overview_payload',lambda cur=None:{'accounts':[{
            'account_code':'tonghuashun','account_type':'brokerage','include_in_nav':True,
            'primary_balance':{'amount':'1050','currency':'CNY','as_of':DAY}}]})
        from datetime import timedelta
        scoped=web.get('/nav/weekly',params={'start':(day-timedelta(days=1)).isoformat(),
            'baseCurrency':'CNY','latest':'true','interval':'daily'})
        assert scoped.status_code==200,scoped.text
        assert scoped.json()['rows'][0]['netWorth'] is None
        assert Decimal(scoped.json()['rows'][-1]['netWorth'])==Decimal('1050')
        monkeypatch.setattr(accounts,'_overview_payload',lambda cur=None:{'accounts':[]})
        series=web.get('/nav/series?limit=1&baseCurrency=CNY')
        assert series.status_code==200,series.text
        assert Decimal(str(series.json()['rows'][-1]['netWorth']))==Decimal('1050')
        drawdown=web.get('/nav/drawdown?limit=2')
        assert drawdown.status_code==200,drawdown.text
        assert Decimal(drawdown.json()['rows'][-1]['total_market_value'])==Decimal('1050')
        overview=web.get('/summary2/latest')
        assert overview.status_code==200,overview.text
        assert Decimal(overview.json()['totals']['market_value'])==Decimal('1050')
        accounts_response=web.get('/api/holdings/by-account')
        assert accounts_response.status_code==200,accounts_response.text
        assert Decimal(accounts_response.json()['by_account'][0]['total_value_base'])==Decimal('1050')
        cfg['accounts']['ibkr']['include_in_nav']=True
        missing_ibkr=web.get('/nav/weekly',params={'start':DAY,'baseCurrency':'CNY','latest':'true','interval':'daily'})
        assert missing_ibkr.status_code==200,missing_ibkr.text
        assert missing_ibkr.json()['rows'][-1]['netWorth'] is None
        assert 'ibkr' in missing_ibkr.json()['rows'][-1]['unavailable_accounts']
        cfg['accounts']['ibkr']['include_in_nav']=False
        # Changing a member revokes the complete scope on every consumer.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE symbol='000001'")
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_balance_basis(cur,account_code='tonghuashun',owner_ratio=Decimal(1)) is None
        response=web.get('/holdings/latest')
        assert response.status_code==200,response.text
        rows={row['symbol']:row for row in response.json()['rows']}
        assert rows['000001']['snapshot_scope_status']=='unknown'
        monkeypatch.setattr(accounts,'_overview_payload',actual_overview)
        with TestClient(account_app) as account_web:
            response=account_web.get('/api/accounts/overview')
            assert response.status_code==200,response.text
            account=next(r for r in response.json()['accounts'] if r['account_code']=='tonghuashun')
            assert account['primary_balance'] is None
        monkeypatch.setattr(accounts,'_overview_payload',lambda cur=None:{'accounts':[]})
        nav=web.get('/nav/weekly',params={'start':DAY,'baseCurrency':'CNY','latest':'true','interval':'daily'})
        assert nav.status_code==200,nav.text
        assert nav.json()['rows'][-1]['netWorth'] is None
        series=web.get('/nav/series?limit=1&baseCurrency=CNY')
        assert series.status_code==200,series.text
        assert series.json()['rows'][-1]['netWorth'] is None
        drawdown=web.get('/nav/drawdown?limit=2')
        assert drawdown.status_code==200,drawdown.text
        assert drawdown.json()['rows'][-1]['total_market_value'] is None
        assert drawdown.json()['max_drawdown'] is None
        overview=web.get('/summary2/latest')
        assert overview.status_code==200,overview.text
        assert overview.json()['totals']['market_value'] is None
        accounts_response=web.get('/api/holdings/by-account')
        assert accounts_response.status_code==200,accounts_response.text
        assert accounts_response.json()['by_account'][0]['total_value_base'] is None
        # Explicitly including an unproven IBKR account still blocks aggregate NAV.
        cfg['accounts']['ibkr']['include_in_nav']=True
        nav=web.get('/nav/weekly',params={'start':DAY,'baseCurrency':'CNY','latest':'true','interval':'daily'})
        assert nav.status_code==200,nav.text
        assert nav.json()['rows'][-1]['netWorth'] is None
        assert 'ibkr' in nav.json()['rows'][-1]['unavailable_accounts']
        # The native holding remains visible when no USD/CNY rate exists.
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (symbol,name,asset_liability,currency,account_code,major_category,minor_category,market_value,cost,as_of,quantity,current_price)
                VALUES ('SYNTH_US','Synthetic USD','资产','USD','tonghuashun','美股','股票',100,80,%s,1,100)""", (DAY,))
        response=web.get('/holdings/latest')
        assert response.status_code==200,response.text
        rows={row['symbol']:row for row in response.json()['rows']}
        assert 'SYNTH_US' in rows, response.text
        usd=rows['SYNTH_US']
        assert Decimal(usd['market_value'])==100
        assert usd['market_value_cny'] is None and usd['cost_cny'] is None and usd['holding_pnl_cny'] is None
        overview=web.get('/summary2/latest')
        assert overview.status_code==200,overview.text
        assert overview.json()['totals']['market_value'] is None
        assert 'tonghuashun' in overview.json()['fx_unavailable_accounts']
        assert next(row for row in overview.json()['children'] if row['symbol']=='SYNTH_US')['market_value'] is None
        # Neither an older nor a future rate proves the historical day.
        from datetime import timedelta
        from domain.investments.valuation_nav import _investment_snapshot_series, build_weekly_nav_rows
        from domain.investments.holdings_view import build_holdings_history_payload, build_holdings_latest_payload
        with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            for fx_day in (day-timedelta(days=1),day+timedelta(days=1)):
                cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')", (fx_day,))
            args=dict(account_codes=['tonghuashun'],dates=[day],base_currency='CNY',owner_ratio_case_sql=lambda alias:('1',[]))
            series=_investment_snapshot_series(cur,**args)
            assert series.unknown_accounts_by_date[day]==['tonghuashun']
            latest_args=dict(limit=100,include_closed=False,view_as_of=day,excluded_accounts=set(),
                             owner_ratios={},default_owner_ratio=Decimal(1))
            current=build_holdings_latest_payload(cur,**latest_args)
            usd=next(row for row in current['rows'] if row['symbol']=='SYNTH_US')
            assert usd['fx_precision']=='estimated' and usd['fx_rate_as_of']==(day-timedelta(days=1)).isoformat()
            values=build_weekly_nav_rows(dates=[day],bank_series={},snapshot_series=series,a_share_series={},ibkr_values={})
            assert values[0]['net_worth'] is None and values[0]['unavailable_accounts']==['tonghuashun']
            history_args=dict(category='美股',days=1,interval='daily',minor_category=None,view_as_of=day,
                              tonghuashun_owner_ratio=Decimal(1),owner_ratio_sql='1',owner_ratio_params=[],excluded_accounts=[])
            history=build_holdings_history_payload(cur,**history_args)
            assert history['rows'][-1]['value'] is None
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic')",(day,))
            series=_investment_snapshot_series(cur,**args)
            # An FX rate cannot repair a tampered account scope.
            assert series.unknown_accounts_by_date[day]==['tonghuashun']
            current=build_holdings_latest_payload(cur,**latest_args)
            usd=next(row for row in current['rows'] if row['symbol']=='SYNTH_US')
            assert usd['fx_precision']=='daily_exact' and usd['fx_rate_as_of']==day.isoformat()
            history=build_holdings_history_payload(cur,**history_args)
            assert history['rows'][-1]['value'] is None
            assert history['rows'][-1]['known_value']==700.0


def test_published_investment_source_keeps_verified_counter_account_leg(empty_source_db, monkeypatch):
    from datetime import date
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scripts import bootstrap_isolated_sources as bootstrap
    from domain.imports import account_import_store as store
    from domain.ledger.ledger_view import _load_cash_rows, _load_investment_account_rows
    from endpoints import account_import
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('bank_test','Synthetic bank') ON CONFLICT DO NOTHING")
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','a1'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    monkeypatch.setattr(store,'get_conn',lambda:psycopg2.connect(**empty_source_db))
    app=FastAPI()
    app.include_router(account_import.router,prefix='/api')
    with TestClient(app) as web:
        web.headers['Origin']='http://testserver'
        fields={'kind':'cash','business_date':DAY,'currency':'CNY','action':'deposit','net_cash':'500','counter_account_code':'bank_test'}
        batch=draft(web,[fields])
        check=preview(web,batch)
        assert check['can_publish'],check
        result=publish(web,batch,check)
        assert result.status_code==200,result.text
    with psycopg2.connect(**empty_source_db) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        day=date.fromisoformat(DAY)
        cash=_load_cash_rows(cur,start=day,end=day,base_currency='CNY')
        assert len(cash)==1 and cash[0]['account_code']=='bank_test' and cash[0]['amount']==-500
        funding=_load_investment_account_rows(cur,start=day,end=day,base_currency='CNY')
        assert len(funding)==1 and funding[0]['amount']==500


def test_scope_confirmation_expires_on_draft_change_and_missing_cash_is_blocked(isolated):
    web,params=isolated
    no_cash=draft(web,[holding()],completeness='complete')
    assert confirm(web,no_cash,count=1,cash='0').status_code>=400
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,batch).status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    changed=web.put('/api/account-import/batches/'+batch['batch_id'],json={'revision':batch['revision'],'rows':batch['rows'],'completeness':'complete'})
    assert changed.status_code==200,changed.text
    batch=changed.json()
    check=preview(web,batch)
    assert check['can_publish'],check
    response=publish(web,batch,check)
    assert response.status_code==200,response.text
    assert sql(params,"SELECT count(*) AS n FROM account_snapshot_version WHERE coverage_status='complete'")[0]['n']==0


def test_complete_reconfirmation_retains_same_day_versions(isolated):
    web,params=isolated
    initial=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,initial).status_code==200
    initial=web.get('/api/account-import/batches/'+initial['batch_id']).json()
    assert publish(web,initial,preview(web,initial)).status_code==200
    again=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='60')],completeness='complete')
    assert confirm(web,again,cash='60').status_code==200
    again=web.get('/api/account-import/batches/'+again['batch_id']).json()
    check=preview(web,again)
    response=publish(web,again,check)
    assert response.status_code==200,response.text
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM account_snapshot_version WHERE account_code='tonghuashun' ORDER BY version_number DESC")
        versions=cur.fetchall()
        assert len(versions)==2 and versions[0]['cash_amount']==Decimal('60') and versions[1]['cash_amount']==Decimal('50')
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',versions[0]['as_of'])})
        assert coverage[('tonghuashun',versions[0]['as_of'])]['status']=='complete'


def test_zero_cash_and_zero_exit_require_explicit_scope_evidence(isolated):
    web,params=isolated
    first=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,first).status_code==200
    first=web.get('/api/account-import/batches/'+first['batch_id']).json()
    assert publish(web,first,preview(web,first)).status_code==200
    zero=holding(quantity='0',cost='0',market_value='0')
    batch=draft(web,[zero,holding(asset_type='cash',symbol=None,action='balance',net_cash='0')],completeness='complete')
    before=preview(web,batch)
    assert not before['can_publish'] and any(i['code']=='exit_scope_unverified' for row in before['rows'] for i in row['issues'])
    assert confirm(web,batch,cash='0').status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    after=preview(web,batch)
    assert after['can_publish'],after
    result=publish(web,batch,after)
    assert result.status_code==200,result.text
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM account_snapshot_version ORDER BY version_number DESC LIMIT 1")
        version=cur.fetchone()
        assert version['cash_amount']==0
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',version['as_of'])})
        assert coverage[('tonghuashun',version['as_of'])]['status']=='complete'
        cur.execute("SELECT symbol,quantity,cost,market_value FROM pams_holdings_snapshot ORDER BY symbol")
        rows=cur.fetchall()
        assert len(rows)==2 and all(row['quantity']==row['cost']==row['market_value']==0 for row in rows)


def test_complete_scope_failure_rolls_back_entire_publication(isolated,monkeypatch):
    from domain.investments import account_snapshot_evidence
    web,params=isolated
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,batch).status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    check=preview(web,batch)
    monkeypatch.setattr(account_snapshot_evidence,'record_complete',lambda *a,**k:(_ for _ in ()).throw(ValueError('synthetic complete scope failure')))
    response=publish(web,batch,check)
    assert response.status_code>=400
    assert sql(params,'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n']==0
    assert sql(params,'SELECT count(*) AS n FROM account_snapshot_version')[0]['n']==0
    assert sql(params,'SELECT status FROM source_batches WHERE batch_id=%s',(batch['batch_id'],))[0]['status']=='draft'


def test_zero_exit_after_scope_revision_change_stays_blocked(isolated):
    web,params=isolated
    first=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,first).status_code==200
    first=web.get('/api/account-import/batches/'+first['batch_id']).json()
    assert publish(web,first,preview(web,first)).status_code==200
    zero=holding(quantity='0',cost='0',market_value='0')
    batch=draft(web,[zero,holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,batch).status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    response=web.put('/api/account-import/batches/'+batch['batch_id'],json={
        'revision':batch['revision'],'rows':batch['rows'],'completeness':'complete'})
    assert response.status_code==200,response.text
    batch=response.json()
    check=preview(web,batch)
    assert not check['can_publish']
    assert any(i['code']=='exit_scope_unverified' for row in check['rows'] for i in row['issues'])
    row=sql(params,"SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE symbol='000001'")[0]
    assert row['quantity']==100 and row['cost']==900 and row['market_value']==1000


def test_earlier_known_holding_omitted_from_complete_scope_blocks_publication(isolated):
    from datetime import date,timedelta
    web,params=isolated
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
            minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
            VALUES ('tonghuashun','Earlier other stock','600001','CNY','A股','A股','资产',100,90,10,10,%s)""",
            (date.fromisoformat(DAY)-timedelta(days=1),))
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    check=preview(web,batch)
    assert not check['can_publish']
    assert any(i['code']=='incomplete_snapshot' for row in check['rows'] for i in row['issues'])
    assert confirm(web,batch).status_code>=400
    assert sql(params,'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n']==1
    assert sql(params,'SELECT count(*) AS n FROM account_snapshot_version')[0]['n']==0
    assert sql(params,'SELECT status FROM source_batches WHERE batch_id=%s',(batch['batch_id'],))[0]['status']=='draft'


def test_late_discovery_of_earlier_holding_invalidates_scope(isolated):
    from datetime import date,timedelta
    web,params=isolated
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,batch).status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    assert publish(web,batch,preview(web,batch)).status_code==200
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
            minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
            VALUES ('tonghuashun','Earlier other stock','600001','CNY','A股','A股','资产',100,90,10,10,%s)""",
            (date.fromisoformat(DAY)-timedelta(days=1),))
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',date.fromisoformat(DAY))})
        assert coverage[('tonghuashun',date.fromisoformat(DAY))]['status']=='unknown'


def test_same_name_earlier_other_currency_invalidates_scope(isolated):
    from datetime import date,timedelta
    web,params=isolated
    batch=draft(web,[holding(),holding(asset_type='cash',symbol=None,action='balance',net_cash='50')],completeness='complete')
    assert confirm(web,batch).status_code==200
    batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
    assert publish(web,batch,preview(web,batch)).status_code==200
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
            minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
            VALUES ('tonghuashun','Test stock','000001','USD','A股','A股','资产',100,90,10,10,%s)""",
            (date.fromisoformat(DAY)-timedelta(days=1),))
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        coverage=load_account_snapshot_coverage(cur,{('tonghuashun',date.fromisoformat(DAY))})
        assert coverage[('tonghuashun',date.fromisoformat(DAY))]['status']=='unknown'
