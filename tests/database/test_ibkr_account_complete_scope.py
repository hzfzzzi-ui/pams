"""Complete IBKR scope requires explicit securities and cash currency evidence."""
# ruff: noqa: F811
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.testclient import TestClient
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.database.test_ibkr_holding_review_http import setup, HEADERS
from tests.database.test_ibkr_holding_identity_review import XML, DAY
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


def scope_report():
    from domain.brokers.ibkr.ibkr_daily_close import COMPONENT_FIELDS
    components={field:'0' for field in COMPONENT_FIELDS}
    components.update(cash='110',stock='123')
    attributes=' '.join(f'{key}="{value}"' for key,value in components.items())
    attributes+=' '+ ' '.join(f'{field}Long="0" {field}Short="0"' for field in COMPONENT_FIELDS if field not in {'cash','stock'})
    attributes+=' cashLong="110" cashShort="0" stockLong="123" stockShort="0" totalLong="233" totalShort="0"'
    nav=(f'<AccountInformation accountId="U_TEST" currency="USD" />'
         f'<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="U_TEST" currency="USD" '
         f'reportDate="20261022" total="233" {attributes} /></EquitySummaryInBase>').encode()
    report=XML.replace(b'toDate="20261022"',b'fromDate="20261022" toDate="20261022"',1)
    report=report.replace(b'<OpenPositions>',nav+b'<OpenPositions>')
    report=report.replace(b'conid="1001"',b'conid="1001" assetCategory="STK" fxRateToBase="1"')
    report=report.replace(b'</FlexStatement>',b'<CashReport>'
        b'<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022" endingCash="50" fxRateToBase="1" />'
        b'<CashReportCurrency accountId="U_TEST" currency="EUR" toDate="20261022" endingCash="30" fxRateToBase="2" />'
        b'</CashReport></FlexStatement>')
    return report


def test_published_account_scope_covers_each_security_and_cash_currency(empty_source_db,monkeypatch):
    import main
    report=scope_report()
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                ('ibkr','Cash USD','CASH_USD','USD','资产','现金','现金/活期',%s,50,50,50),
                ('ibkr','Cash EUR','CASH_EUR','EUR','资产','现金','现金/活期',%s,30,30,30)""",(DAY,DAY))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',14,'synthetic')",(DAY,DAY))
        conn.commit()
        with TestClient(main.app,base_url='http://pams.local') as web:
            source=web.post('/api/ibkr/holding-review/stage',headers=HEADERS,json={
                'source_account_id':'U_TEST','business_date':DAY.isoformat(),'xml_text':report.decode()}).json()
            base='/api/ibkr/holding-review/'+source['batch_id']
            rev=web.get(base,headers=HEADERS).json()['revision']
            row=web.post(base+'/review',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'expected_revision':rev,'note':'row'}); assert row.status_code==200,row.text
            identity=web.post(base+'/identity',headers=HEADERS,json={'item_id':source['position_item_ids'][0],
                'holding_id':holding,'expected_revision':row.json()['revision'],'note':'contract'}); assert identity.status_code==200,identity.text
            scope=web.post(base+'/review',headers=HEADERS,json={'item_id':source['scope_item_id'],
                'expected_revision':identity.json()['revision'],'note':'positions','scope_complete':True}); assert scope.status_code==200,scope.text
            binding=web.post(base+'/account-binding',headers=HEADERS,json={'source_account_id':'U_TEST',
                'expected_revision':scope.json()['revision'],'note':'account mapping'}); assert binding.status_code==200,binding.text
            payload={'expected_revision':binding.json()['revision'],'note':'positions and currencies reviewed','confirmed':True}
            assert web.post(base+'/account-scope',headers=HEADERS,json={**payload,'confirmed':False}).status_code==409
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE pams_holdings_snapshot ALTER COLUMN market_value DROP NOT NULL")
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=NULL WHERE symbol='CASH_USD'")
            conn.commit()
            assert web.post(base+'/account-scope',headers=HEADERS,json=payload).status_code==409
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=50,asset_liability='负债' WHERE symbol='CASH_USD'")
                cur.execute("ALTER TABLE pams_holdings_snapshot ALTER COLUMN market_value SET NOT NULL")
            conn.commit()
            assert web.post(base+'/account-scope',headers=HEADERS,json=payload).status_code==409
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE pams_holdings_snapshot DROP CONSTRAINT pams_holdings_snapshot_asset_liability_check")
                cur.execute("UPDATE pams_holdings_snapshot SET asset_liability='unknown' WHERE symbol='CASH_USD'")
            conn.commit()
            assert web.post(base+'/account-scope',headers=HEADERS,json=payload).status_code==409
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET asset_liability='资产' WHERE symbol='CASH_USD'")
                cur.execute("ALTER TABLE pams_holdings_snapshot ADD CONSTRAINT pams_holdings_snapshot_asset_liability_check CHECK(asset_liability IN ('资产','负债'))")
            conn.commit()
            with conn.cursor() as cur: cur.execute("UPDATE pams_holdings_snapshot SET market_value=49 WHERE symbol='CASH_USD'")
            conn.commit()
            assert web.post(base+'/account-scope',headers=HEADERS,json=payload).status_code==409
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=50 WHERE symbol='CASH_USD'")
                cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                    major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                    ('ibkr','Cash GBP','CASH_GBP','GBP','资产','现金','现金/活期',%s,10,10,10)""",(DAY,))
            conn.commit()
            assert web.post(base+'/account-scope',headers=HEADERS,json=payload).status_code==409
            with conn.cursor() as cur: cur.execute("DELETE FROM pams_holdings_snapshot WHERE symbol='CASH_GBP'")
            conn.commit()
            reviewed=web.post(base+'/account-scope',headers=HEADERS,json=payload)
            assert reviewed.status_code==200,reviewed.text
            assert reviewed.json()['cash_currencies']==['EUR','USD']
            from domain.brokers.ibkr import ibkr_account_scope
            original_record=ibkr_account_scope.record_complete
            def fail_record(*args,**kwargs):
                raise psycopg2.errors.DeadlockDetected('synthetic failure after source publication')
            monkeypatch.setattr(ibkr_account_scope,'record_complete',fail_record)
            failed=web.post(base+'/publish',headers=HEADERS,json={'expected_revision':reviewed.json()['revision']})
            assert failed.status_code==409 and 'synthetic' not in failed.text
            state=web.get(base,headers=HEADERS).json()
            assert state['status']!='published' and state['revision']==reviewed.json()['revision']
            with conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM account_snapshot_version')
                assert cur.fetchone()[0]==0
            conn.rollback()
            from concurrent.futures import ThreadPoolExecutor
            from threading import Event
            entered=Event(); release=Event(); insert_started=Event(); writer_pid=[]
            def pause_record(*args,**kwargs):
                entered.set()
                if not release.wait(10): raise ValueError('synthetic publish timeout')
                return original_record(*args,**kwargs)
            monkeypatch.setattr(ibkr_account_scope,'record_complete',pause_record)
            def publish_once():
                return web.post(base+'/publish',headers=HEADERS,json={'expected_revision':reviewed.json()['revision']})
            def insert_member():
                with psycopg2.connect(**empty_source_db) as writer,writer.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='8s'")
                    writer_pid.append(writer.get_backend_pid())
                    insert_started.set()
                    cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                        major_category,minor_category,as_of,quantity,cost,market_value) VALUES
                        ('ibkr','Concurrent cash','CASH_GBP','GBP','资产','现金','现金/活期',%s,10,10,10)""",(DAY,))
            with ThreadPoolExecutor(max_workers=3) as executor:
                publishing=executor.submit(publish_once)
                assert entered.wait(8)
                insertion=executor.submit(insert_member)
                assert insert_started.wait(8)
                from time import monotonic, sleep
                deadline=monotonic()+5
                blocked=False
                with psycopg2.connect(**empty_source_db) as observer,observer.cursor() as cur:
                    while monotonic()<deadline:
                        cur.execute("SELECT wait_event_type,wait_event FROM pg_stat_activity WHERE pid=%s",(writer_pid[0],))
                        state=cur.fetchone()
                        if state and state[0]=='Lock' and state[1]=='advisory':
                            blocked=True
                            break
                        observer.rollback()
                        sleep(0.05)
                assert blocked and not insertion.done()  # Server shows the trigger waiting for the account lock.
                competitor=executor.submit(publish_once)
                release.set()
                attempts=[publishing.result(timeout=10),competitor.result(timeout=10)]
                insertion.result(timeout=10)
            assert sorted(r.status_code for r in attempts)==[200,409],[(r.status_code,r.text) for r in attempts]
            published=next(r for r in attempts if r.status_code==200)
            assert published.json()['scope']=='account_scope'
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='unknown'
            conn.rollback()
            with conn.cursor() as cur: cur.execute("DELETE FROM pams_holdings_snapshot WHERE symbol='CASH_GBP'")
            conn.commit()
            assert web.post(base+'/publish',headers=HEADERS,json={'expected_revision':reviewed.json()['revision']}).status_code==409
        with psycopg2.connect(**empty_source_db) as read:
            read.set_session(readonly=True,isolation_level='REPEATABLE READ')
            with read.cursor(cursor_factory=RealDictCursor) as cur:
                coverage=load_account_snapshot_coverage(cur,{('ibkr',DAY)})
                assert coverage[('ibkr',DAY)]['status']=='complete',coverage
                cur.execute('SELECT cash_amount,scope_attestation FROM account_snapshot_version WHERE version_id=%s',
                    (published.json()['account_scope_version_id'],))
                version=cur.fetchone()
                assert version['cash_amount'] is None
                assert version['scope_attestation']['cash_amounts']=={'EUR':'30','USD':'50'}
                from domain.investments.valuation_nav import _investment_snapshot_series
                history=_investment_snapshot_series(cur,account_codes=['ibkr'],dates=[DAY],base_currency='USD',
                    owner_ratio_case_sql=lambda alias:('1',[]))
                assert history[DAY]==Decimal('233') and not history.unknown_accounts_by_date
        from domain.investments import portfolio_state
        from endpoints import accounts
        from fastapi import FastAPI
        cfg={'accounts':{'ibkr':{'account_type':'brokerage','truth_source':'ibkr_flex','base_currency':'USD',
                                'owner_ratio':'1','include_in_nav':True}}}
        monkeypatch.setattr(portfolio_state,'_view_as_of',lambda:DAY)
        with psycopg2.connect(**empty_source_db) as read,read.cursor(cursor_factory=RealDictCursor) as cur:
            balances=portfolio_state.load_account_balances(cur,'CNY',cfg)
            balance=next(r for r in balances if r['account_code']=='ibkr')
            assert balance['snapshot_scope_status']=='complete'
            assert balance['amount_cny']==Decimal('1631'),balance
            from domain.investments.valuation_nav import current_nav_bucket_from_parts
            positions=portfolio_state.load_investment_positions(cur,'CNY',cfg,account_balances=balances)
            bucket=current_nav_bucket_from_parts(account_balances=balances,investment_positions=positions)
            assert bucket['net_worth']==Decimal('1631'),bucket
        with conn.cursor() as cur: cur.execute("DELETE FROM pams_fx_rates_daily WHERE quote_currency='EUR' AND as_of=%s",(DAY,))
        conn.commit()
        with psycopg2.connect(**empty_source_db) as read,read.cursor(cursor_factory=RealDictCursor) as cur:
            balance=next(r for r in portfolio_state.load_account_balances(cur,'CNY',cfg) if r['account_code']=='ibkr')
            assert balance['snapshot_scope_status']=='complete' and balance['amount_cny'] is None
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','EUR',14,'synthetic')",(DAY,))
        conn.commit()
        monkeypatch.setattr(accounts,'get_conn',lambda:psycopg2.connect(**empty_source_db))
        monkeypatch.setattr(accounts,'_load_accounts_config',lambda:cfg)
        monkeypatch.setattr(accounts,'_load_bank_config',lambda:{})
        monkeypatch.setattr(accounts,'_view_as_of',lambda:DAY)
        app=FastAPI(); app.include_router(accounts.router,prefix='/api')
        with TestClient(app) as web:
            response=web.get('/api/accounts/overview')
            assert response.status_code==200,response.text
            account=next(r for r in response.json()['accounts'] if r['account_code']=='ibkr')
            assert 'amount_cny' in account['primary_balance'], account['primary_balance']
            assert Decimal(str(account['primary_balance']['amount_cny']))==Decimal('1631')
            assert Decimal(str(account['primary_balance']['amount']))==Decimal('1631')/Decimal('7')
            # Without a documented quote the EUR component must not disappear.
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='EUR' AND as_of=%s",(DAY,))
            conn.commit()
            missing=web.get('/api/accounts/overview')
            assert missing.status_code==200,missing.text
            missing_account=next(r for r in missing.json()['accounts'] if r['account_code']=='ibkr')
            assert missing_account['primary_balance'] is None,missing_account
            with psycopg2.connect(**empty_source_db) as read,read.cursor(cursor_factory=RealDictCursor) as cur:
                missing_balance=next(r for r in portfolio_state.load_account_balances(cur,'CNY',cfg) if r['account_code']=='ibkr')
                assert missing_balance['amount_cny'] is None and missing_balance['fx_status']=='unavailable',missing_balance
                from domain.investments.valuation_nav import _investment_snapshot_series
                history=_investment_snapshot_series(cur,account_codes=['ibkr'],dates=[DAY],base_currency='USD',
                    owner_ratio_case_sql=lambda alias:('1',[]))
                assert history.unknown_accounts_by_date[DAY]==['ibkr']
            from endpoints import portfolio_state as portfolio_endpoint
            monkeypatch.setattr(portfolio_state,'_load_accounts_config',lambda:cfg)
            monkeypatch.setattr(portfolio_endpoint,'build_portfolio_state',lambda base_currency:portfolio_state.build_portfolio_state(
                base_currency=base_currency,conn_factory=lambda:psycopg2.connect(**empty_source_db)))
            portfolio_app=FastAPI(); portfolio_app.include_router(portfolio_endpoint.router,prefix='/api')
            with TestClient(portfolio_app) as portfolio_web:
                state=portfolio_web.get('/api/portfolio/state')
                assert state.status_code==200,state.text
                item=next(r for r in state.json()['account_balances'] if r['account_code']=='ibkr')
                assert item['amount_cny'] is None and item['fx_status']=='unavailable',item
                assert state.json()['risk_snapshot']['total_nav'] is None
            # Previous and future dated quotes must not become today's verified balance.
            with conn.cursor() as cur:
                cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s - 1,'CNY','EUR',8,'old'),(%s + 1,'CNY','EUR',8,'future')",(DAY,DAY))
            conn.commit()
            with TestClient(portfolio_app) as portfolio_web:
                state=portfolio_web.get('/api/portfolio/state')
                assert state.status_code==200,state.text
                item=next(r for r in state.json()['account_balances'] if r['account_code']=='ibkr')
                assert item['amount_cny'] is None and item['fx_status']=='unavailable',item
            from domain.investments.valuation_nav import current_nav_bucket_from_parts
            with psycopg2.connect(**empty_source_db) as read,read.cursor(cursor_factory=RealDictCursor) as cur:
                balances=portfolio_state.load_account_balances(cur,'CNY',cfg)
                positions=portfolio_state.load_investment_positions(cur,'CNY',cfg,account_balances=balances)
                bucket=current_nav_bucket_from_parts(account_balances=balances,investment_positions=positions)
                assert bucket['net_worth'] is None and 'ibkr' in bucket['unavailable_accounts'],bucket
            monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**empty_source_db))
            monkeypatch.setattr(main,'_view_as_of',lambda:DAY)
            monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:cfg)
            monkeypatch.setattr(main,'_latest_published_portfolio_valuation_state',lambda cur,**kwargs:None)
            monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal('1'))
            monkeypatch.setattr(main,'_investment_return_view_as_of',lambda cur:DAY)
            real_overview=accounts._overview_payload
            def configured_overview(cur=None):
                payload=real_overview(cur=cur)
                payload['accounts']=[row for row in payload['accounts'] if row['account_code']=='ibkr']
                return payload
            monkeypatch.setattr(accounts,'_overview_payload',configured_overview)
            with TestClient(main.app) as nav_web:
                nav=nav_web.get('/nav/weekly',params={'start':DAY.isoformat(),'baseCurrency':'CNY','latest':'true','interval':'daily'})
                assert nav.status_code==200,nav.text
                assert nav.json()['rows'][-1]['netWorth'] is None,nav.text
                series=nav_web.get('/nav/series?limit=1')
                assert series.status_code==200 and series.json()['rows'][-1]['netWorth'] is None,series.text
                drawdown=nav_web.get('/nav/drawdown?limit=2')
                assert drawdown.status_code==200 and drawdown.json()['rows'][-1]['total_market_value'] is None,drawdown.text
                assert drawdown.json()['max_drawdown'] is None
                performance=nav_web.get('/api/account-performance/ibkr')
                assert performance.status_code==200,performance.text
                assert performance.json()['status']=='unavailable',performance.text
                assert performance.json()['net_liquidation_cny'] is None
                assert {v['currency'] for v in performance.json()['currency_values']}=={'USD','EUR'}
                assert nav_web.get('/api/account-performance/not-an-account').status_code==404
            with conn.cursor() as cur:
                cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='EUR' AND as_of=%s",(DAY,))
            conn.commit()
            with TestClient(main.app) as nav_web:
                series=nav_web.get('/nav/series?limit=1')
                assert series.status_code==200,series.text
                assert series.json()['rows'][-1]['netWorth'] is not None,series.text
                assert Decimal(str(series.json()['rows'][-1]['netWorth']))==Decimal('1631')
        with conn.cursor() as cur: cur.execute("UPDATE pams_holdings_snapshot SET market_value=31 WHERE symbol='CASH_EUR'")
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='unknown'
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_holdings_snapshot SET market_value=30 WHERE symbol='CASH_EUR'")
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='complete'
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE source_files DISABLE TRIGGER USER')
            cur.execute("UPDATE source_files SET content=%s WHERE batch_id=%s",(b'changed',source['batch_id']))
            cur.execute('ALTER TABLE source_files ENABLE TRIGGER USER')
        conn.commit()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='unknown'


def test_cash_scope_requires_explicit_section_values_and_exact_account_date():
    import pytest
    from domain.brokers.ibkr.ibkr_account_scope import _cash_amounts
    base=XML.replace(b'</FlexStatement>',b'<CashReport><CashReportCurrency accountId="U_TEST" '
        b'currency="USD" toDate="20261022" endingCash="0" /></CashReport></FlexStatement>')
    assert _cash_amounts(base,source_account_id='U_TEST',day=DAY)=={'USD':Decimal('0')}
    for invalid in (XML, base.replace(b'endingCash="0"',b''), base.replace(b'toDate="20261022" endingCash',b'toDate="20261021" endingCash'),
        base.replace(b'accountId="U_TEST" currency="USD"',b'accountId="OTHER" currency="USD"'),
        base.replace(b'</CashReport>',b'<CashReportCurrency currency="USD" toDate="20261022" endingCash="0" /></CashReport>'),
        base.replace(b'</CashReport>',b'<CashReportCurrency currency="BASE_SUMMARY" toDate="20261022" endingCash="0" />'
                     b'<CashReportCurrency currency="BASE_SUMMARY" toDate="20261022" endingCash="0" /></CashReport>')):
        with pytest.raises(ValueError): _cash_amounts(invalid,source_account_id='U_TEST',day=DAY)


def test_account_balances_keep_liability_sign_in_real_pg(empty_source_db, monkeypatch):
    from datetime import date
    from endpoints import accounts
    from domain.investments import snapshot_coverage
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    import psycopg2
    from psycopg2.extras import RealDictCursor
    day=date(2026,10,22)
    monkeypatch.setattr(accounts,'_view_as_of',lambda:day)
    monkeypatch.setattr(snapshot_coverage,'load_account_snapshot_coverage',lambda cur,keys:{key:{'status':'complete'} for key in keys})
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('mix','mixed','USD')")
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,currency,as_of,asset_liability,major_category,minor_category,market_value)
                VALUES ('mix','stock','USD',%s,'资产','股票','股票',100),('mix','debt','USD',%s,'负债','借款','借款',20),
                       ('mix','negative cash','EUR',%s,'资产','现金','现金',-30)""",(day,day,day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',8,'synthetic')""",(day,day))
            balances=accounts._fetch_holding_balances(cur)
            rates=accounts._fetch_daily_fx_rates_by_date(cur,balances)
            result=accounts._primary_platform_balance(balances['mix'],'USD',rates)
            assert result['amount_cny']==Decimal('320')
            assert result['amount']==Decimal('320')/Decimal('7')
            cur.execute("INSERT INTO pams_holdings_snapshot(account_code,name,currency,as_of,asset_liability,major_category,minor_category,market_value) VALUES ('mix','zero cash','GBP',%s,'资产','现金','现金',0)",(day,))
            from domain.investments import portfolio_state
            from domain.investments.valuation_nav import current_nav_bucket_from_parts
            monkeypatch.setattr(portfolio_state,'_view_as_of',lambda:day)
            monkeypatch.setattr(portfolio_state,'load_account_snapshot_coverage',lambda cur,keys:{key:{'status':'complete'} for key in keys})
            monkeypatch.setattr(portfolio_state,'annotate_snapshot_coverage',lambda cur,rows:[{**r,'snapshot_scope_status':'complete'} for r in rows])
            cfg={'accounts':{'mix':{'account_type':'brokerage','truth_source':'platform_snapshot','base_currency':'USD','include_in_nav':True}}}
            balance=portfolio_state.load_account_balances(cur,'CNY',cfg)
            assert next(r for r in balance if r['account_code']=='mix')['amount_cny']==Decimal('320')
            positions=portfolio_state.load_investment_positions(cur,'CNY',cfg,account_balances=balance)
            zero=next(r for r in positions if r['asset_name']=='zero cash')
            assert zero['market_value_cny']==0 and zero['fx_status']=='not_required'
            bucket=current_nav_bucket_from_parts(account_balances=balance,investment_positions=positions)
            assert bucket['net_worth']==Decimal('320'),bucket


def test_full_nav_requires_explicit_all_components_and_consistent_original_fx():
    import pytest
    from domain.brokers.ibkr.ibkr_account_scope import _cash_amounts, _complete_nav
    report=scope_report()
    cash=_cash_amounts(report,source_account_id='U_TEST',day=DAY)
    evidence=_complete_nav(report,source_account_id='U_TEST',day=DAY,cash_amounts=cash)
    assert Decimal(evidence['nav_base'])==Decimal(233)
    negative=report.replace(b'currency="USD" toDate="20261022" endingCash="50"',
                            b'currency="USD" toDate="20261022" endingCash="-50"')
    negative=negative.replace(b' cash="110"',b' cash="10"').replace(b'total="233"',b'total="133"')
    negative=negative.replace(b'cashLong="110"',b'cashLong="60"').replace(b'cashShort="0"',b'cashShort="-50"')
    negative=negative.replace(b'totalLong="233"',b'totalLong="183"').replace(b'totalShort="0"',b'totalShort="-50"')
    signed=_complete_nav(negative,source_account_id='U_TEST',day=DAY,
        cash_amounts=_cash_amounts(negative,source_account_id='U_TEST',day=DAY))
    assert Decimal(signed['cash_base'])==Decimal(10) and Decimal(signed['nav_base'])==Decimal(133)
    variants=[report.replace(b' interestAccruals="0"',b''),
        report.replace(b' optionsLong="0"',b''),
        report.replace(b' totalLong="233"',b''),
        report.replace(b' interestAccruals="0"',b' interestAccruals="1"').replace(b'total="233"',b'total="234"'),
        report.replace(b' stock="123"',b' stock="124"').replace(b'total="233"',b'total="234"'),
        report.replace(b'currency="EUR" toDate="20261022" endingCash="30" fxRateToBase="2"',
                       b'currency="EUR" toDate="20261022" endingCash="30" fxRateToBase="3"'),
        report.replace(b'assetCategory="STK"',b'assetCategory="FUT"'),
        report.replace(b'</CashReport>',b'<CashReportCurrency accountId="U_TEST" currency="BASE_SUMMARY" toDate="20261022" endingCash="111" /></CashReport>')]
    for changed in variants:
        with pytest.raises(ValueError):
            _complete_nav(changed,source_account_id='U_TEST',day=DAY,
                cash_amounts=_cash_amounts(changed,source_account_id='U_TEST',day=DAY))


def test_reviewed_negative_cash_keeps_signed_liability_in_full_scope(empty_source_db,monkeypatch):
    from uuid import uuid4
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review,review_identity,review_account_binding,review_account_scope,publish
    from domain.investments import portfolio_state
    report=scope_report().replace(b'symbol="TEST"',b'symbol="TEST" description="Test"').replace(b'currency="USD" toDate="20261022" endingCash="50"',
                                  b'currency="USD" toDate="20261022" endingCash="-50"')
    report=report.replace(b' cash="110"',b' cash="10"').replace(b'total="233"',b'total="133"')
    report=report.replace(b'cashLong="110"',b'cashLong="60"').replace(b'cashShort="0"',b'cashShort="-50"')
    report=report.replace(b'totalLong="233"',b'totalLong="183"').replace(b'totalShort="0"',b'totalShort="-50"')
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        from collectors import ibkr_flex
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
        parsed=ibkr_flex.parse_flex_report(report.decode())
        usd=next(row for row in parsed.rows if row['symbol']=='CASH_USD')
        assert usd['market_value']==-50 and usd['major_category']=='借款' and usd['asset_liability']=='负债'
        assert ibkr_flex._write_holdings(conn,parsed.rows,account_code='ibkr',as_of=DAY)==len(parsed.rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',14,'synthetic')",(DAY,DAY))
            source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report)
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],expected_revision=rev,note='row')['revision']
            rev=review_identity(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],holding_id=holding,expected_revision=rev,note='contract')['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=rev,note='positions',scope_complete=True)['revision']
            rev=review_account_binding(cur,batch_id=source['batch_id'],source_account_id='U_TEST',expected_revision=rev,note='account')['revision']
            rev=review_account_scope(cur,batch_id=source['batch_id'],expected_revision=rev,note='signed cash',confirmed=True)['revision']
            result=publish(cur,batch_id=source['batch_id'],expected_revision=rev)
        conn.commit()
        assert result['account_scope_version_id']
        cfg={'accounts':{'ibkr':{'account_type':'brokerage','truth_source':'ibkr_flex','base_currency':'USD','owner_ratio':'1','include_in_nav':True}}}
        monkeypatch.setattr(portfolio_state,'_view_as_of',lambda:DAY)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            proof=load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]
            assert proof['status']=='complete'
            balance=next(r for r in portfolio_state.load_account_balances(cur,'CNY',cfg) if r['account_code']=='ibkr')
            assert balance['amount_cny']==Decimal('931')


def test_reverse_account_and_row_lock_order_rolls_back_and_can_retry(empty_source_db,monkeypatch):
    """A direct external row lock can deadlock, but one whole transaction rolls back."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    with psycopg2.connect(**empty_source_db) as conn:
        holding=setup(conn,monkeypatch,empty_source_db)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,asset_liability,
                major_category,minor_category,as_of,quantity,cost,market_value)
                VALUES ('ibkr','Other','OTHER','USD','资产','美股','stock',%s,0,0,0) RETURNING id""",(DAY,))
            other=cur.fetchone()[0]
        conn.commit()
    account_locked=Event(); row_locked=Event()
    def account_first():
        with psycopg2.connect(**empty_source_db) as c:
            try:
                with c.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='10s'")
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
                    cur.execute('UPDATE pams_holdings_snapshot SET quantity=quantity+1 WHERE id=%s',(other,))
                    account_locked.set()
                    assert row_locked.wait(5)
                    cur.execute('UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE id=%s',(holding,))
                c.commit(); return 'ok'
            except psycopg2.errors.DeadlockDetected:
                c.rollback(); return 'deadlock'
    def row_first():
        with psycopg2.connect(**empty_source_db) as c:
            try:
                with c.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='10s'")
                    cur.execute('SELECT id FROM pams_holdings_snapshot WHERE id=%s FOR UPDATE',(holding,))
                    row_locked.set()
                    assert account_locked.wait(5)
                    cur.execute('UPDATE pams_holdings_snapshot SET quantity=quantity+1 WHERE id=%s',(holding,))
                c.commit(); return 'ok'
            except psycopg2.errors.DeadlockDetected:
                c.rollback(); return 'deadlock'
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(account_first); b=executor.submit(row_first)
        first,second=a.result(timeout=15),b.result(timeout=15)
    assert sorted([first,second])==['deadlock','ok']
    with psycopg2.connect(**empty_source_db) as c,c.cursor() as cur:
        cur.execute('SELECT quantity,market_value FROM pams_holdings_snapshot WHERE id=%s',(holding,))
        quantity,value=cur.fetchone()
        cur.execute('SELECT quantity FROM pams_holdings_snapshot WHERE id=%s',(other,)); other_quantity=cur.fetchone()[0]
        assert (quantity,value,other_quantity)==((10,124,1) if first=='ok' else (11,123,0))
        # Retrying under the canonical account-first lock commits the entire operation.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
        if first=='deadlock':
            cur.execute('UPDATE pams_holdings_snapshot SET quantity=quantity+1 WHERE id=%s',(other,))
            cur.execute('UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE id=%s',(holding,))
        else:
            cur.execute('UPDATE pams_holdings_snapshot SET quantity=quantity+1 WHERE id=%s',(holding,))
    with psycopg2.connect(**empty_source_db) as c,c.cursor() as cur:
        cur.execute('SELECT quantity,market_value FROM pams_holdings_snapshot WHERE id=%s',(holding,))
        assert cur.fetchone()==(11,124)
        cur.execute('SELECT quantity FROM pams_holdings_snapshot WHERE id=%s',(other,))
        assert cur.fetchone()[0]==1


def test_supported_nonstock_contracts_reconcile_to_their_own_nav_component():
    import pytest
    from domain.brokers.ibkr.ibkr_account_scope import _cash_amounts, _complete_nav
    from collectors.ibkr_flex import parse_flex_report
    for contract,component in [('OPT','options'),('BOND','bonds'),('FUND','funds'),('CMDTY','commodities')]:
        report=scope_report().replace(b'assetCategory="STK"',('assetCategory="'+contract+'"').encode())
        report=report.replace(b' stock="123"',b' stock="0"').replace(b'stockLong="123"',b'stockLong="0"')
        report=report.replace((' '+component+'="0"').encode(),(' '+component+'="123"').encode())
        report=report.replace((component+'Long="0"').encode(),(component+'Long="123"').encode())
        cash=_cash_amounts(report,source_account_id='U_TEST',day=DAY)
        position=next(row for row in parse_flex_report(report.decode()).rows if row['symbol']=='TEST')
        assert position['major_category']=='其他' and position['minor_category']=={'OPT':'期权','BOND':'债券','FUND':'基金','CMDTY':'商品'}[contract]
        assert Decimal(_complete_nav(report,source_account_id='U_TEST',day=DAY,cash_amounts=cash)['nav_base'])==Decimal(233)
        inconsistent=report.replace((component+'Long="123"').encode(),(component+'Long="0"').encode())
        with pytest.raises(ValueError):
            _complete_nav(inconsistent,source_account_id='U_TEST',day=DAY,cash_amounts=cash)


def test_real_option_and_stock_source_is_classified_and_proves_full_nav(empty_source_db,monkeypatch):
    from uuid import uuid4
    from collectors import ibkr_flex
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review,review_identity,review_account_binding,review_account_scope,publish
    from domain.investments import portfolio_state
    report=scope_report().replace(b'symbol="TEST"',b'symbol="TEST" description="Test"')
    report=report.replace(b'</OpenPositions>',b'<OpenPosition accountId="U_TEST" conid="1002" symbol="OPT_TEST" '
        b'description="Synthetic option" assetCategory="OPT" currency="USD" reportDate="20261022" '
        b'position="1" costBasisMoney="10" marketValue="15" fxRateToBase="1" /></OpenPositions>')
    report=report.replace(b' options="0"',b' options="15"').replace(b'optionsLong="0"',b'optionsLong="15"')
    report=report.replace(b'total="233"',b'total="248"').replace(b'totalLong="233"',b'totalLong="248"')
    with psycopg2.connect(**empty_source_db) as conn:
        setup(conn,monkeypatch,empty_source_db)
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED','true')
        parsed=ibkr_flex.parse_flex_report(report.decode())
        option=next(r for r in parsed.rows if r['symbol']=='OPT_TEST')
        assert (option['major_category'],option['minor_category'],option['market_value'])==('其他','期权',15)
        unknown_report=report.replace(b'assetCategory="OPT"',b'assetCategory="FUT"')
        future=next(r for r in ibkr_flex.parse_flex_report(unknown_report.decode()).rows if r['symbol']=='OPT_TEST')
        assert (future['major_category'],future['minor_category'])==('其他','待分类')
        missing_type=report.replace(b' assetCategory="OPT"',b'')
        unknown=next(r for r in ibkr_flex.parse_flex_report(missing_type.decode()).rows if r['symbol']=='OPT_TEST')
        assert (unknown['major_category'],unknown['minor_category'],unknown['market_value'])==('其他','待分类',15)
        assert ibkr_flex._write_holdings(conn,parsed.rows,account_code='ibkr',as_of=DAY)==len(parsed.rows)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','EUR',14,'synthetic')",(DAY,DAY))
            source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id='U_TEST',business_date=DAY,content=report)
            cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],)); rev=cur.fetchone()['revision']
            for item in source['position_item_ids']:
                cur.execute('SELECT raw_fields FROM source_items WHERE source_item_id=%s',(item,)); raw=cur.fetchone()['raw_fields']
                cur.execute('SELECT id FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s AND symbol=%s',('ibkr',DAY,raw['symbol']))
                hid=cur.fetchone()['id']
                rev=review(cur,batch_id=source['batch_id'],item_id=item,expected_revision=rev,note='source row')['revision']
                rev=review_identity(cur,batch_id=source['batch_id'],item_id=item,holding_id=hid,expected_revision=rev,note='contract identity')['revision']
            rev=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=rev,note='positions',scope_complete=True)['revision']
            rev=review_account_binding(cur,batch_id=source['batch_id'],source_account_id='U_TEST',expected_revision=rev,note='account')['revision']
            rev=review_account_scope(cur,batch_id=source['batch_id'],expected_revision=rev,note='complete scope',confirmed=True)['revision']
            result=publish(cur,batch_id=source['batch_id'],expected_revision=rev)
        conn.commit()
        assert result['account_scope_version_id']
        cfg={'accounts':{'ibkr':{'account_type':'brokerage','truth_source':'ibkr_flex','base_currency':'USD','owner_ratio':'1','include_in_nav':True}}}
        monkeypatch.setattr(portfolio_state,'_view_as_of',lambda:DAY)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert load_account_snapshot_coverage(cur,{('ibkr',DAY)})[('ibkr',DAY)]['status']=='complete'
            balance=next(r for r in portfolio_state.load_account_balances(cur,'CNY',cfg) if r['account_code']=='ibkr')
            assert balance['amount_cny']==Decimal('1736')
