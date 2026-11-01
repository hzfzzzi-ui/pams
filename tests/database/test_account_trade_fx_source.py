"""Account and trade facts require sourced business-day FX."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from tests.imports.test_account_import_publish import client  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_account_balance_current_and_rollforward_require_source(empty_source_db, monkeypatch):
    from domain.accounts import account_balance
    day=date(2026,10,22); previous=day-timedelta(days=1); anchor=day-timedelta(days=2)
    monkeypatch.setattr(account_balance,'load_account_snapshot_coverage',lambda cur,pairs:{pair:{'status':'complete'} for pair in pairs})
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(name,account_code,asset_liability,currency,
                major_category,minor_category,market_value,cost,as_of) VALUES
                ('stock','ibkr','资产','USD','美股','股票',100,80,%s),
                ('stock','ibkr','资产','USD','美股','股票',105,80,%s),
                ('stock','ibkr','资产','USD','美股','股票',110,80,%s)""",(anchor,previous,day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES
                (%s,'CNY','USD',7,'synthetic'),(%s,'CNY','USD',7,''),(%s,'CNY','USD',7.2,'')""",(anchor,previous,day))
            kwargs={'account_code':'ibkr','owner_ratio':Decimal(1)}
            assert account_balance.load_account_balance_basis(cur,**kwargs) is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
            basis=account_balance.load_account_balance_basis(cur,**kwargs)
            assert basis['position_value_gross']==Decimal(792)
            # Current and anchor quotes are known; an unknown prior day must
            # not erase the proven current NAV or invent a prior value.
            roll={**kwargs,'net_liquidation_gross':Decimal(700),'net_liquidation_as_of':anchor}
            prior_unknown=account_balance.load_account_balance_basis(cur,**roll)
            assert prior_unknown['net_gross']==Decimal(792) and prior_unknown['previous_net_gross'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(previous,))
            basis=account_balance.load_account_balance_basis(cur,**roll)
            assert basis['net_gross']==Decimal(792) and basis['previous_net_gross']==Decimal(735)
            # A true zero is known even without a foreign-currency quote.
            cur.execute("UPDATE pams_holdings_snapshot SET market_value=0,cost=0 WHERE account_code='ibkr' AND as_of=%s",(day,))
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='USD' AND as_of=%s",(day,))
            zero=account_balance.load_account_balance_basis(cur,**kwargs)
            assert zero['position_value_gross']==0 and zero['holding_pnl_gross']==0


def test_ibkr_fx_helper_and_trade_insert_require_sourced_quote(empty_source_db):
    from domain.brokers.ibkr.ibkr_cash_events import dated_fx_rate
    from domain.investments.investment_trade_fact import build_investment_trade_fact,insert_investment_trade_facts
    day=date(2026,10,22)
    payload={'source_table':'synthetic','source_id':'bank','source_key':'bank','trade_date':day,
        'account_code':'ibkr','currency':'USD','side':'deposit','gross_amount':'100','net_cash':'100','fx_rate_to_cny':'7'}
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(day,))
            assert dated_fx_rate(cur,'USD',day) is None
            fact=build_investment_trade_fact(payload)
            assert insert_investment_trade_facts(cur,[fact],strict_source=True)==1
            assert insert_investment_trade_facts(cur,[fact],strict_source=True)==0
            cur.execute("SELECT net_cash,fx_rate_to_cny,net_cash_cny FROM pams_investment_trade_fact WHERE source_id='bank'")
            saved=cur.fetchone()
            assert saved['net_cash']==100 and saved['fx_rate_to_cny'] is None and saved['net_cash_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
            assert dated_fx_rate(cur,'USD',day)==7
            payload.update(source_id='proven',source_key='proven')
            assert insert_investment_trade_facts(cur,[build_investment_trade_fact(payload)],strict_source=True)==1
            cur.execute("SELECT net_cash,fx_rate_to_cny,net_cash_cny FROM pams_investment_trade_fact WHERE source_id='proven'")
            saved=cur.fetchone()
            assert saved['net_cash']==100 and saved['fx_rate_to_cny']==7 and saved['net_cash_cny']==700


def test_rollforward_cash_requires_sourced_business_day_fx(empty_source_db,monkeypatch):
    from domain.accounts.account_balance import _cash_delta_cny
    from domain.imports import source_fact_evidence
    day=date(2026,10,22)
    class Verifier:
        def __init__(self,cur): self.cur=cur
        def verify(self,**kwargs): return {'status':'verified'}
    monkeypatch.setattr(source_fact_evidence,'NativeFactVerifier',Verifier)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO source_batches(batch_id,account_code,business_type,institution,acquisition_method)
                VALUES ('00000000-0000-0000-0000-000000000001','ibkr','trade','synthetic','manual')""")
            cur.execute("""INSERT INTO source_items(source_item_id,batch_id,source_uid,parse_method,record_hash,review_status)
                VALUES ('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001',
                        'synthetic','manual',%s,'confirmed')""",('a'*64,))
            cur.execute("""INSERT INTO pams_investment_trade_fact(trade_date,account_code,currency,trade_type,side,
                gross_amount,net_cash,affects_cash,source_table,source_id,source_hash,source_item_id)
                VALUES (%s,'ibkr','USD','deposit','deposit',100,100,true,'synthetic','cashflow',%s,
                        '00000000-0000-0000-0000-000000000002')""",(day,'b'*64))
            cur.execute("""UPDATE source_batches SET status='published',published_source_hash=%s
                WHERE batch_id='00000000-0000-0000-0000-000000000001'""",('c'*64,))
            cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES (%s,'CNY','USD',7,'')",(day,))
            assert _cash_delta_cny(cur,'ibkr',day-timedelta(days=1),day) is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='USD' AND as_of=%s",(day,))
            assert _cash_delta_cny(cur,'ibkr',day-timedelta(days=1),day)==Decimal(700)


def test_confirmed_full_account_snapshots_do_not_double_count_cash_flows(client, monkeypatch):
    """Actual published facts and complete cash+security snapshots share one net value."""
    from domain.accounts.account_balance import load_account_balance_basis
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    from tests.imports.test_account_import_publish import draft, holding, preview, publish, trade

    web,params=client
    from pathlib import Path
    fx_schema=Path(__file__).resolve().parents[2]/'database/postgres/schema/004_fx_rates.sql'
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute(fx_schema.read_text(encoding='utf-8'))
    anchor=date.today()-timedelta(days=4)
    middle=anchor+timedelta(days=1)
    end=middle+timedelta(days=1)

    def source(fields, completeness='partial'):
        batch=draft(web, fields, completeness=completeness)
        check=preview(web,batch)
        assert check['can_publish'],check
        response=publish(web,batch,check)
        assert response.status_code==200,response.text
        return response.json()

    def full_snapshot(day, shares, cost, cash):
        fields=[holding(business_date=day.isoformat(),quantity=str(shares),
                        market_value=str(shares*10),cost=str(cost)),
                holding(business_date=day.isoformat(),asset_type='cash',symbol=None,
                        action='balance',net_cash=str(cash))]
        batch=draft(web,fields,completeness='complete')
        response=web.post('/api/account-import/batches/'+batch['batch_id']+'/confirm-scope',
            json={'revision':batch['revision'],'as_of':day.isoformat(),'member_count':2,
                  'cash_amount':str(cash),'note':'confirmed synthetic account scope','confirmed':True})
        assert response.status_code==200,response.text
        batch=web.get('/api/account-import/batches/'+batch['batch_id']).json()
        check=preview(web,batch)
        assert check['can_publish'],check
        response=publish(web,batch,check)
        assert response.status_code==200,response.text

    full_snapshot(anchor,100,900,100)
    source([{'kind':'cash','business_date':middle.isoformat(),'currency':'CNY',
             'action':'deposit','net_cash':'20','counter_account_code':'bank_test'}])
    source([trade(business_date=middle.isoformat(),quantity='10',gross_amount='100',
                  fee_amount='0',net_cash='-100')])
    full_snapshot(middle,110,1000,20)
    source([{'kind':'cash','business_date':end.isoformat(),'currency':'CNY',
             'action':'withdraw','net_cash':'-10','counter_account_code':'bank_test'}])
    source([trade(business_date=end.isoformat(),action='sell',quantity='5',gross_amount='50',
                  fee_amount='0',net_cash='50')])
    full_snapshot(end,105,950,60)

    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        scopes=load_account_snapshot_coverage(cur,{('tonghuashun',day) for day in (anchor,middle,end)})
        assert all(value['status']=='complete' for value in scopes.values()),scopes
        result=load_account_balance_basis(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
            net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
        assert result['net_gross']==Decimal(1110)
        assert result['previous_net_gross']==Decimal(1120)
        assert Decimal(result['net_liquidation_rollforward']['cash_delta_gross_cny'])==Decimal(-40)
        assert result['net_liquidation_rollforward']['cash_delta_in_snapshot'] is True
        from domain.investments.investment_returns import build_account_performance_payload
        performance=build_account_performance_payload(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
            initial_capital=Decimal(1100),net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
        assert Decimal(performance['net_liquidation_cny'])==Decimal(1110)
        assert Decimal(performance['position_market_value_cny'])==Decimal(1050)
        assert Decimal(performance['cash_or_uninvested_cny'])==Decimal(60)
        assert Decimal(performance['observed_external_net_flow_cny'])==Decimal(-10)
        assert performance['external_flow_coverage']=='unknown'
        assert performance['daily_pnl_cny'] is None and performance['raw_daily_pnl_cny'] is None
        assert performance['total_pnl_cny'] is None and performance['return_ratio'] is None

    # Published original coverage reports explicitly cover each period and
    # bind each original row to the independently verified account fact.
    import csv
    import io
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT trade_id,trade_date,trade_type,currency,net_cash,source_key
            FROM pams_investment_trade_fact WHERE account_code='tonghuashun'
              AND trade_type IN ('deposit','withdraw') ORDER BY trade_date""")
        flow_facts=[dict(row) for row in cur.fetchall()]
    assert len(flow_facts)==2
    # A bank-to-investment transfer is an external flow for this account;
    # the derived bank leg has the opposite sign and matching provenance.
    with psycopg2.connect(**params) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT f.trade_date,f.net_cash,f.currency,f.source_item_id,
            c.event_date,c.amount,c.currency AS bank_currency,c.source_item_id AS bank_item,
            c.account_code AS bank_account,c.counter_account_code
            FROM pams_investment_trade_fact f JOIN pams_cashflow_fact c ON c.cashflow_id=f.cashflow_id
            WHERE f.account_code='tonghuashun' AND f.trade_type IN ('deposit','withdraw') ORDER BY f.trade_date""")
        legs=[dict(row) for row in cur.fetchall()]
    assert len(legs)==2
    for leg in legs:
        assert leg['trade_date']==leg['event_date'] and leg['net_cash']==-leg['amount']
        assert leg['currency']==leg['bank_currency']=='CNY' and leg['source_item_id']==leg['bank_item']
        assert leg['bank_account']=='bank_test' and leg['counter_account_code']=='tonghuashun'

    def publish_coverage(day, fact):
        buffer=io.StringIO(newline='')
        writer=csv.writer(buffer,lineterminator='\n')
        writer.writerow(['account_code','period_start','period_end','event_date','side','currency','net_amount','external_id'])
        writer.writerow(['tonghuashun',day.isoformat(),day.isoformat(),day.isoformat(),
                         fact['trade_type'],fact['currency'],str(fact['net_cash']),fact['source_key']])
        content=buffer.getvalue().encode()
        stage=web.post('/api/account-import/flow-coverage/stage?account_code=tonghuashun&filename=scope.csv',
                       content=content,headers={'Content-Type':'text/csv'})
        assert stage.status_code==200,stage.text
        batch=stage.json()
        path='/api/account-import/flow-coverage/'+batch['batch_id']+'/publish'
        body={'revision':batch['revision'],'mappings':{fact['source_key']:fact['trade_id']},
              'note':'reviewed original and all external account flows','confirmed':True}
        assert web.post(path,json={**body,'confirmed':False}).status_code==400
        assert web.post(path,json={**body,'mappings':{fact['source_key']:fact['trade_id']+999}}).status_code==400
        response=web.post(path,json=body)
        assert response.status_code==200,response.text
        approved=response.json()
        assert approved['status']=='published' and not approved['replayed']
        replay=web.post(path,json={**body,'revision':approved['revision']})
        assert replay.status_code==200 and replay.json()['replayed'],replay.text
        return approved

    publish_coverage(middle,flow_facts[0])
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            partial=build_account_performance_payload(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
                initial_capital=Decimal(1100),initial_capital_as_of=anchor,
                net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
            assert partial['total_pnl_cny'] is None and partial['daily_pnl_cny'] is None
    publish_coverage(end,flow_facts[1])
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            proven=build_account_performance_payload(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
                initial_capital=Decimal(1100),initial_capital_as_of=anchor,
                net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
            assert Decimal(proven['daily_pnl_cny'])==0 and Decimal(proven['total_pnl_cny'])==0
            assert proven['external_flow_coverage']=='verified'
            assert proven['daily_pnl_source']=='verified_reviewed_source_period'
            assert len(proven['total_pnl_evidence']['source_sha256'])==2
            assert Decimal(proven['observed_external_net_flow_cny'])==Decimal(-10)

    # Exercise the actual compatibility HTTP route with a stale configured
    # report: it may be shown as context but must never replace proven values.
    import main
    from endpoints import accounts
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main,'get_conn',lambda:psycopg2.connect(**params))
    monkeypatch.setattr(main,'_account_owner_ratio',lambda account:Decimal(1))
    monkeypatch.setattr(main,'_account_initial_capital_cny',lambda account:Decimal(1100))
    monkeypatch.setattr(main,'_daily_pnl_view_as_of',lambda:end)
    monkeypatch.setattr(main,'_load_portfolio_accounts_config',lambda:{'accounts':{
        'tonghuashun':{'initial_capital_as_of':anchor.isoformat()}}})
    monkeypatch.setattr(main,'_reported_returns_cfg',lambda:{'account_performance':{
        'tonghuashun':{'use_reported_anchor':True,'as_of':anchor.isoformat(),
                       'cash_balance_gross':'99999','total_return_gross':'99999'}}})
    monkeypatch.setattr(accounts,'_overview_payload',lambda **kwargs:{'accounts':[{
        'account_code':'tonghuashun','primary_balance':{'gross_amount':'1100',
            'amount':'1100','as_of':anchor.isoformat()}}]})
    with TestClient(main.app) as app:
        response=app.get('/api/account-performance/tonghuashun')
        assert response.status_code==200,response.text
        payload=response.json()
        assert Decimal(payload['net_liquidation_cny'])==Decimal(1110)
        assert Decimal(payload['cash_or_uninvested_cny'])==Decimal(60)
        assert Decimal(payload['observed_external_net_flow_cny'])==Decimal(-10)
        assert Decimal(payload['daily_pnl_cny'])==0 and Decimal(payload['total_pnl_cny'])==0
        assert payload['external_flow_coverage']=='verified'
        assert payload['reported_performance']['status']=='unverified_configuration'

    # An asset transfer is not income. Even if cash reports are complete,
    # an unvalued in-kind movement prevents using NAV delta as profit.
    with psycopg2.connect(**params) as conn:
        conn.set_session(isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_investment_trade_fact
                (trade_date,account_code,currency,trade_type,affects_cash,source_table,source_id,source_hash)
                VALUES (%s,'tonghuashun','CNY','transfer_in_kind',false,'synthetic','in-kind-test',%s)""",
                (end,'a'*64))
            transfer=build_account_performance_payload(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
                initial_capital=Decimal(1100),initial_capital_as_of=anchor,
                net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
            assert transfer['daily_pnl_cny'] is None and transfer['total_pnl_cny'] is None
        conn.rollback()

    # A newly published external fact not present in the reviewed report
    # invalidates coverage instead of being silently treated as zero.
    source([{'kind':'cash','business_date':end.isoformat(),'currency':'CNY',
             'action':'deposit','net_cash':'1','counter_account_code':'bank_test'}])
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            invalidated=build_account_performance_payload(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
                initial_capital=Decimal(1100),initial_capital_as_of=anchor,
                net_liquidation_gross=Decimal(1100),net_liquidation_as_of=anchor)
            assert invalidated['daily_pnl_cny'] is None and invalidated['total_pnl_cny'] is None


def test_signed_foreign_cash_and_missing_scope_or_fx(empty_source_db, monkeypatch):
    from domain.accounts import account_balance
    from domain.investments.investment_returns import build_account_performance_payload

    day=date.today()-timedelta(days=2)
    coverage=lambda cur,pairs:{pair:{'status':'complete'} for pair in pairs}
    monkeypatch.setattr(account_balance,'load_account_snapshot_coverage',coverage)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(name,account_code,asset_liability,currency,
                major_category,minor_category,market_value,cost,as_of) VALUES
                ('stock','ibkr',%s,'USD',%s,'stock',100,80,%s),
                ('financed cash','ibkr',%s,'USD',%s,'cash',20,20,%s),
                ('other loan','ibkr',%s,'USD',%s,'loan',5,5,%s)""",
                ('资产','美股',day,'负债','现金',day,'负债','借款',day))
            cur.execute("""INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source)
                VALUES (%s,'CNY','USD',7,'synthetic')""",(day,))
            result=build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal(1),initial_capital=Decimal(500))
            assert Decimal(result['net_liquidation_cny'])==Decimal(525)
            assert Decimal(result['position_market_value_cny'])==Decimal(700)
            assert Decimal(result['cash_or_uninvested_cny'])==Decimal(-140)
            assert Decimal(result['other_liabilities_cny'])==Decimal(-35)
            assert result['total_pnl_cny'] is None and result['daily_pnl_cny'] is None
            cur.execute("UPDATE pams_holdings_snapshot SET cost=NULL WHERE name='stock'")
            missing_cost=build_account_performance_payload(cur,account_code='ibkr',owner_ratio=Decimal(1),initial_capital=Decimal(500))
            assert Decimal(missing_cost['net_liquidation_cny'])==Decimal(525)
            assert Decimal(missing_cost['cash_or_uninvested_cny'])==Decimal(-140)
            assert missing_cost['holding_pnl_cny'] is None
            cur.execute("UPDATE pams_fx_rates_daily SET source='' WHERE quote_currency='USD' AND as_of=%s",(day,))
            assert account_balance.load_account_balance_basis(cur,account_code='ibkr',owner_ratio=Decimal(1)) is None
            monkeypatch.setattr(account_balance,'load_account_snapshot_coverage',
                lambda cur,pairs:{pair:{'status':'unknown'} for pair in pairs})
            assert account_balance.load_account_balance_basis(cur,account_code='ibkr',owner_ratio=Decimal(1)) is None


def test_ordinary_account_foreign_funding_is_rejected_without_facts(client):
    """Existing manual import contract rejects unsupported foreign investment cash."""
    from tests.imports.test_account_import_publish import draft, preview, publish

    web,params=client
    day=date.today()-timedelta(days=2)
    for action,amount in (('deposit','10'),('withdraw','-2')):
        batch=draft(web,[{'kind':'cash','business_date':day.isoformat(),'currency':'USD',
                          'action':action,'net_cash':amount,'counter_account_code':'bank_test'}])
        result=preview(web,batch)
        assert not result['can_publish']
        assert 'currency_unverified' in {issue['code'] for row in result['rows'] for issue in row['issues']}
        assert publish(web,batch,result).status_code == 409
    with psycopg2.connect(**params) as conn,conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM pams_investment_trade_fact')
        assert cur.fetchone()[0]==0
        cur.execute('SELECT count(*) FROM pams_cashflow_fact')
        assert cur.fetchone()[0]==0
