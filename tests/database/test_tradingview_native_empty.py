"""New TradingView facts and holdings against a truly bootstrapped empty DB."""
# ruff: noqa: F811
from datetime import date, timedelta
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts import bootstrap_isolated_sources as bootstrap
from domain.imports.tradingview_source import stage_csvs
from domain.imports.tradingview_source_publish import publish
from tests.database.test_tradingview_source_publish import review_all
from collectors import tradingview_portfolio as tv
from endpoints import investment_transactions

HEADER = 'Symbol,Side,Qty,Fill Price,Commission,Closing Time,Tax\n'


def stage_publish(params, content):
    batch_id = uuid4()
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        stage_csvs(cur, batch_id=batch_id, account_code='tonghuashun', files=[('trades.csv', content)])
        review_all(cur,batch_id)
        publish(cur, batch_id=batch_id, confirm=True)
    return batch_id


def test_tax_unknown_zero_positive_and_http_remain_distinct(empty_source_db, monkeypatch):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    content = (HEADER + 'SSE:600000,Buy,10,8,1,2026-10-01 12:00:00,\n'
               + 'SSE:600001,Buy,10,8,1,2026-10-01 12:00:01,0\n'
               + 'SSE:600002,Buy,10,8,1,2026-10-01 12:00:02,0.1234567891\n').encode()
    batch = stage_publish(empty_source_db, content)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert publish(cur, batch_id=batch, confirm=True)['replayed']
        cur.execute("SELECT symbol,tax_amount,net_cash,source_item_id FROM pams_investment_trade_fact WHERE source_table='tradingview_csv' ORDER BY symbol")
        rows = cur.fetchall()
        assert [(r['symbol'], r['tax_amount'], r['net_cash']) for r in rows] == [
            ('600000', None, None), ('600001', Decimal('0'), Decimal('-81')),
            ('600002', Decimal('0.1234567891'), Decimal('-81.1234567891'))]
        assert all(r['source_item_id'] is not None for r in rows)
        cur.execute("SELECT to_regclass('pams_tradingview_transactions')")
        assert cur.fetchone()['to_regclass'] is None
    monkeypatch.setattr(investment_transactions, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(investment_transactions, '_load_accounts_config', lambda: {})
    app = FastAPI()
    app.include_router(investment_transactions.router, prefix='/api')
    with TestClient(app) as web:
        response = web.get('/api/investment-transactions?account_code=tonghuashun')
        assert response.status_code == 200, response.text
        output = response.json()
        row = next(r for r in output['rows'] if r['symbol'] == '600000')
        assert row['tax_amount'] is None and row['fees'] is None and row['fees_status'] == 'unknown'
        assert output['summary']['fees_cny'] is None and output['summary']['fees_personal_cny'] is None


def test_native_holding_calculation_requires_tax_and_verifies_contributions(empty_source_db, monkeypatch, tmp_path):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    content = (HEADER + '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
               + 'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,0.25\n').encode()
    stage_publish(empty_source_db, content)
    path = tmp_path / 'trades.csv'
    path.write_bytes(content)
    transactions = tv.parse_transactions_csv(path)
    marks = {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')}
    old_result = tv.calculate_portfolio(transactions, marks, as_of=date(2026,10,22))
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(tv.TradingViewImportError, match='include confirmed taxes'):
            tv.sync_portfolio_to_db(conn, old_result, transactions)
    result = tv.calculate_portfolio(transactions, marks, as_of=date(2026,10,22), require_explicit_tax=True)
    assert result.cash == Decimal('918.75')
    with psycopg2.connect(**empty_source_db) as conn:
        tv.sync_portfolio_to_db(conn, result, transactions)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE account_code='tonghuashun' AND symbol='600000'")
        assert cur.fetchone()['market_value'] == Decimal('90')
        cur.execute("SELECT count(*) AS n FROM valuation_evidence")
        assert cur.fetchone()['n'] >= 1
        cur.execute("SELECT to_regclass('pams_tradingview_transactions') AS legacy")
        assert cur.fetchone()['legacy'] is None


def test_missing_tax_preserves_known_security_and_records_partial_account(empty_source_db, monkeypatch, tmp_path):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','prior cash','$CASH','CNY','现金','现金/活期','资产',500,500,500,1,'2026-10-22')""")
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    content = (HEADER + '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
               + 'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,\n').encode()
    stage_publish(empty_source_db, content)
    path = tmp_path / 'trades.csv'
    path.write_bytes(content)
    transactions = tv.parse_transactions_csv(path)
    marks = {'600000': tv.MarketMark(price=Decimal('9'), name='Synthetic stock')}
    result = tv.calculate_portfolio(transactions, marks, as_of=date(2026,10,22), require_explicit_tax=True)
    assert result.cash is None and result.total_nav is None and result.realized_pnl is None
    assert result.positions[0].quantity == 10 and result.positions[0].market_value == 90
    assert result.positions[0].cost is None and result.positions[0].holding_pnl is None
    summary = tv.summarize_result(result)
    assert summary['cash'] is None and summary['total_nav'] is None
    assert summary['positions'][0]['cost'] is None and summary['positions'][0]['buy_price'] is None
    guessed = tv.calculate_portfolio(transactions, marks, as_of=date(2026,10,22))
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(ValueError, match='holding omission|holding cost requires confirmed taxes'):
            tv.sync_portfolio_to_db(conn, guessed, transactions)
    with psycopg2.connect(**empty_source_db) as conn:
        tv.sync_portfolio_to_db(conn, result, transactions)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id,quantity,market_value,cost,buy_price FROM pams_holdings_snapshot WHERE account_code='tonghuashun' AND symbol='600000'")
        holding = cur.fetchone()
        assert (holding['quantity'],holding['market_value'],holding['cost'],holding['buy_price']) == (10,90,None,None)
        cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE name='prior cash'")
        assert cur.fetchone()['market_value'] == 500
        cur.execute("SELECT version_id,coverage_status,cash_status,cash_amount FROM account_snapshot_version WHERE account_code='tonghuashun' ORDER BY version_number DESC LIMIT 1")
        version = cur.fetchone()
        assert (version['coverage_status'],version['cash_status'],version['cash_amount']) == ('partial','unknown',None)
        cur.execute("""SELECT holding_id,holding_row_hash FROM account_snapshot_member
            WHERE version_id=%s""", (version['version_id'],))
        members = cur.fetchall()
        assert len(members) == 1 and members[0]['holding_id'] == holding['id']
        cur.execute("SELECT md5(to_jsonb(h)::text) AS hash FROM pams_holdings_snapshot h WHERE id=%s", (holding['id'],))
        assert cur.fetchone()['hash'] == members[0]['holding_row_hash']
        cur.execute("SELECT tax_amount, net_cash FROM pams_investment_trade_fact WHERE symbol='600000'")
        assert cur.fetchone()['tax_amount'] is None
    forged = replace(result, positions=[replace(result.positions[0], quantity=Decimal('11'))])
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(tv.TradingViewImportError, match='quantity or valuation differs'):
            tv.sync_portfolio_to_db(conn, forged, transactions)
    forged_value = replace(result, positions=[replace(result.positions[0], market_value=Decimal('999'))])
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(tv.TradingViewImportError, match='quantity or valuation differs'):
            tv.sync_portfolio_to_db(conn, forged_value, transactions)
    with psycopg2.connect(**empty_source_db) as conn:
        tv.sync_portfolio_to_db(conn, result, transactions)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM account_snapshot_version WHERE account_code='tonghuashun'")
        assert cur.fetchone()[0] == 1
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("UPDATE account_snapshot_version SET coverage_status='complete' WHERE version_id=%s", (version['version_id'],))
        conn.rollback()


def test_partial_snapshot_visible_without_false_account_nav(empty_source_db, monkeypatch, tmp_path):
    import main
    from domain.investments import portfolio_state
    from domain.investments.valuation_nav import current_nav_bucket_from_parts
    from domain.brokers.ibkr import ibkr_daily_close_read
    from endpoints import accounts

    day = date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','old cash','$CASH','CNY','现金','现金/活期','资产',500,500,500,1,%s)""", (day,))
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,name,symbol,currency,major_category,
                minor_category,asset_liability,market_value,cost,quantity,current_price,as_of)
                VALUES ('tonghuashun','old stock','600001','CNY','A股','A股','资产',100,80,10,10,%s)""", (day - timedelta(days=1),))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    content = (HEADER + '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
               + 'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,\n').encode()
    stage_publish(empty_source_db, content)
    path = tmp_path / 'trades.csv'
    path.write_bytes(content)
    transactions = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(transactions, {'600000':tv.MarketMark(price=Decimal('9'), name='Synthetic')},
                                    as_of=day, require_explicit_tax=True)
    with psycopg2.connect(**empty_source_db) as conn:
        tv.sync_portfolio_to_db(conn, result, transactions)

    cfg = {'accounts': {'tonghuashun': {'account_type':'brokerage','truth_source':'platform_snapshot',
        'base_currency':'CNY','owner_ratio':'1','include_in_nav':True}}}
    monkeypatch.setattr(portfolio_state, '_view_as_of', lambda: day)
    monkeypatch.setattr(ibkr_daily_close_read, 'configured', lambda: False)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        balances = portfolio_state.load_account_balances(cur, 'CNY', cfg)
        positions = portfolio_state.load_investment_positions(cur, 'CNY', cfg, account_balances=balances)
    position = next(row for row in positions if row['symbol'] == '600000')
    assert position['quantity'] == 10 and position['market_value_cny'] == 90
    assert position['snapshot_scope_status'] == 'partial' and position['snapshot_scope_member'] == 'recorded'
    assert position['snapshot_market_evidence'] == 'unknown' and position['cost_cny'] is None
    older_stock = next(row for row in positions if row['symbol'] == '600001')
    assert older_stock['market_value_cny'] == 100 and older_stock['snapshot_scope_status'] == 'unknown'
    old_cash = next(row for row in positions if row['symbol'] == '$CASH')
    assert old_cash['snapshot_scope_member'] == 'unverified'
    assert balances[0]['amount_cny'] is None
    bucket = current_nav_bucket_from_parts(account_balances=balances, investment_positions=positions)
    assert bucket['scope_status'] == 'incomplete' and bucket['net_worth'] is None
    state = portfolio_state.build_portfolio_state_from_parts(as_of=day, base_currency='CNY',
        account_balances=balances, investment_positions=positions)
    assert state['snapshot_scope_status'] == 'incomplete' and state['risk_snapshot']['total_nav'] is None

    monkeypatch.setattr(main, 'get_conn', lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, '_holdings_market_view_as_of', lambda cur: day)
    monkeypatch.setattr(main, '_daily_pnl_view_as_of', lambda: day)
    monkeypatch.setattr(main, '_excluded_holding_accounts', lambda: set())
    monkeypatch.setattr(main, '_account_owner_ratios', lambda: {})
    monkeypatch.setattr(main, '_default_owner_ratio', lambda: Decimal('1'))
    monkeypatch.setattr(main, '_view_as_of', lambda: day)
    monkeypatch.setattr(main, '_investment_return_view_as_of', lambda cur: day)
    monkeypatch.setattr(main, '_load_portfolio_accounts_config', lambda: cfg)
    monkeypatch.setattr(main, '_latest_published_portfolio_valuation_state', lambda cur, **kwargs: None)
    monkeypatch.setattr(main, '_account_owner_ratio', lambda account: Decimal('1'))
    monkeypatch.setattr(accounts, '_overview_payload', lambda cur=None: {'accounts': []})
    with TestClient(main.app) as web:
        response = web.get('/holdings/latest')
        assert response.status_code == 200, response.text
        rows = {row['symbol']: row for row in response.json()['rows']}
        assert float(rows['600000']['market_value']) == 90
        assert rows['600000']['snapshot_scope_status'] == 'partial'
        assert rows['600000']['snapshot_scope_member'] == 'recorded'
        assert rows['600000']['cost'] is None and rows['600000']['holding_pnl'] is None
        assert rows['$CASH']['snapshot_scope_member'] == 'unverified'
        assert float(rows['600001']['market_value']) == 100 and rows['600001']['snapshot_scope_status'] == 'unknown'
        nav = web.get('/nav/weekly?start=2026-10-22&baseCurrency=CNY&latest=true&interval=daily')
        assert nav.status_code == 200, nav.text
        assert nav.json()['rows'][-1]['netWorth'] is None
        assert 'tonghuashun' in nav.json()['rows'][-1]['unavailable_accounts']


def test_unrepresentable_tax_rolls_back_publication(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    batch = uuid4()
    content = (HEADER + 'SSE:600000,Buy,10,8,1,2026-10-01 12:00:00,0.12345678911\n').encode()
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        stage_csvs(cur, batch_id=batch, account_code='tonghuashun', files=[('trades.csv', content)])
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ValueError, match='tax is invalid or unrepresentable'):
            review_all(cur,batch)
        with pytest.raises(ValueError, match='individual review'):
            publish(cur, batch_id=batch, confirm=True)
        cur.execute("SELECT count(*) AS n FROM pams_investment_trade_fact")
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT status FROM source_batches WHERE batch_id=%s", (str(batch),))
        assert cur.fetchone()['status'] == 'draft'


def test_partial_account_scope_failure_rolls_back_holdings(empty_source_db, monkeypatch, tmp_path):
    from domain.investments import account_snapshot_evidence
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    content = (HEADER + '$CASH,Deposit,1000,,0,2026-10-01 00:00:00,0\n'
               + 'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,\n').encode()
    stage_publish(empty_source_db, content)
    path = tmp_path / 'trades.csv'
    path.write_bytes(content)
    transactions = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(transactions, {'600000':tv.MarketMark(price=Decimal('9'), name='Synthetic')},
                                    as_of=date(2026,10,22), require_explicit_tax=True)
    monkeypatch.setattr(account_snapshot_evidence, 'record_partial', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('synthetic scope failure')))
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(ValueError, match='scope failure'):
            tv.sync_portfolio_to_db(conn, result, transactions)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pams_holdings_snapshot WHERE account_code='tonghuashun'")
        assert cur.fetchone()[0] == 0
        cur.execute('SELECT count(*) FROM account_snapshot_version')
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM pams_investment_trade_fact WHERE source_table='tradingview_csv'")
        assert cur.fetchone()[0] == 2


def test_partial_position_rejects_omitted_known_transactions(empty_source_db, monkeypatch, tmp_path):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    first = (HEADER + 'SSE:600000,Buy,10,8,1,2026-10-02 00:00:00,\n').encode()
    second = (HEADER + 'SSE:600000,Buy,5,7,1,2026-10-01 00:00:00,0\n').encode()
    stage_publish(empty_source_db, first)
    stage_publish(empty_source_db, second)
    path = tmp_path / 'trades.csv'
    path.write_bytes(first)
    transactions = tv.parse_transactions_csv(path)
    result = tv.calculate_portfolio(transactions, {'600000':tv.MarketMark(price=Decimal('9'), name='Synthetic')},
                                    as_of=date(2026,10,22), require_explicit_tax=True)
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(tv.TradingViewImportError, match='other recorded trade inputs'):
            tv.sync_portfolio_to_db(conn, result, transactions)
    with psycopg2.connect(**empty_source_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pams_holdings_snapshot WHERE account_code='tonghuashun'")
        assert cur.fetchone()[0] == 0


def test_tradingview_writer_isolation_gate_fails_before_any_write(empty_source_db, monkeypatch):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    monkeypatch.delenv('PAMS_HOLDING_EVIDENCE_ISOLATED', raising=False)
    result = tv.PortfolioCalcResult(as_of=date(2026,10,22), account_code='tonghuashun',
        positions=[], cash=None, holdings_market_value=Decimal('0'), holdings_cost=Decimal('0'),
        holding_pnl=Decimal('0'), total_nav=None, realized_pnl=None, total_deposits=Decimal('0'),
        total_dividends=Decimal('0'), raw_transaction_count=0, deduped_transaction_count=0)
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(tv.TradingViewImportError,match='isolated source evidence'):
            tv.sync_portfolio_to_db(conn, result, [])
        with pytest.raises(tv.TradingViewImportError,match='isolated source evidence'):
            tv._write_portfolio_holdings(conn, result, before_commit=lambda: None)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pams_holdings_snapshot")
            assert cur.fetchone()[0] == 0
