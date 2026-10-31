"""Synthetic IBKR source linkage, on a marked disposable PostgreSQL only."""
# ruff: noqa: F811
from datetime import date
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from domain.brokers.ibkr import ibkr_cash_events as cash
from domain.brokers.ibkr import ibkr_cash_event_sync as sync
from domain.imports import isolation

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 22)
CUTOFF = date(2026, 10, 1)


@pytest.fixture
def isolated_ibkr(client, monkeypatch):
    _, params = client
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Synthetic IBKR')")
        cur.execute('''CREATE TABLE pams_ibkr_trades (
            id bigserial PRIMARY KEY, account_code text, source_account_id text, source_trade_key text,
            trade_date date, symbol text, name text, asset_category text, currency text, side text,
            quantity numeric, price numeric, gross_amount numeric, commission numeric, net_cash numeric,
            realized_pnl numeric, raw_json jsonb, updated_at timestamptz DEFAULT now())''')
        for name in ('074_unified_source.sql', '075_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    return params


def envelope(transaction='DIV1', **overrides):
    raw = {'accountId': 'U_TEST', 'transactionID': transaction, 'dateTime': '20261022',
           'type': 'Dividends', 'currency': 'CNY', 'amount': '2'}
    raw.update(overrides)
    return {'raw': raw, 'statement_account_id': 'U_TEST',
            'source_evidence': {'broker_source': 'ibkr_flex', 'source_sha256': 'a' * 64, 'confirmed_day': DAY.isoformat()}}


def import_cash(params, event):
    with psycopg2.connect(**params) as conn:
        return cash.import_cash_events(conn, [event], account_code='ibkr', cutoff=CUTOFF, statement_as_of=DAY)


def test_cash_event_links_new_fact_and_retry_is_idempotent(isolated_ibkr):
    params = isolated_ibkr
    assert import_cash(params, envelope())['inserted_count'] == 1
    assert import_cash(params, envelope())['duplicate_count'] == 1
    row = sql(params, '''SELECT b.status, i.raw_fields, f.source_item_id, i.source_item_id AS item_id
        FROM source_batches b JOIN source_items i USING(batch_id)
        JOIN pams_investment_trade_fact f ON f.source_item_id=i.source_item_id''')[0]
    assert row['status'] == 'published' and row['source_item_id'] == row['item_id']
    assert row['raw_fields']['official_attributes'] == envelope()['raw']
    assert row['raw_fields']['source_account_id'] == 'U_TEST'
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 1


def test_existing_cash_fact_is_not_backfilled(isolated_ibkr, monkeypatch):
    params = isolated_ibkr
    monkeypatch.delenv('PAMS_UNIFIED_SOURCE_ISOLATED')
    assert import_cash(params, envelope('HIST'))['inserted_count'] == 1
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    assert import_cash(params, envelope('HIST'))['duplicate_count'] == 1
    assert sql(params, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]['source_item_id'] is None
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0


def test_cash_event_same_identity_with_changed_evidence_is_blocked(isolated_ibkr):
    params = isolated_ibkr
    assert import_cash(params, envelope('CONFLICT'))['inserted_count'] == 1
    result = import_cash(params, envelope('CONFLICT', amount='3'))
    assert result['inserted_count'] == 0
    assert result['pending'][0]['reason'] == 'conflicting_cash_transaction'
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 1


def test_cash_failure_rolls_back_fact_and_source(isolated_ibkr):
    params = isolated_ibkr
    event = envelope('BAD')
    event['source_evidence'] = {'source_sha256': 'invalid', 'confirmed_day': DAY.isoformat()}
    with pytest.raises(ValueError, match='verified account'):
        import_cash(params, event)
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0
    assert sql(params, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0


def test_isolation_gate_rejects_before_fact_write(isolated_ibkr, monkeypatch):
    params = isolated_ibkr
    def reject(_cur):
        raise isolation.IsolationError('synthetic isolation rejection')
    monkeypatch.setattr(isolation, 'source_enabled', reject)
    with psycopg2.connect(**params) as conn:
        with pytest.raises(isolation.IsolationError):
            cash.import_cash_events(conn, [envelope('REJECTED')], account_code='ibkr', cutoff=CUTOFF, statement_as_of=DAY)
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM pams_investment_trade_fact')
            assert cur.fetchone()[0] == 0


def test_flex_trade_records_raw_attributes_and_daily_gate_separately(isolated_ibkr):
    params = isolated_ibkr
    sql(params, '''INSERT INTO pams_ibkr_trades(account_code,source_account_id,source_trade_key,trade_date,
        symbol,name,asset_category,currency,side,quantity,price,gross_amount,commission,net_cash,realized_pnl,raw_json)
        VALUES ('ibkr','U_TEST','trade-identity',%s,'TEST','Test','STK','CNY','BUY',2,10,20,-1,-21,0,
                '{"accountId":"U_TEST","tradeID":"T1"}'::jsonb)''', (DAY,))
    snapshot = {'source_account_id': 'U_TEST', 'source_sha256': 'b' * 64}
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        inserted, pending = sync._import_day_trades(cur, snapshot, DAY.isoformat())
        assert inserted == 1 and not pending
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        inserted, pending = sync._import_day_trades(cur, snapshot, DAY.isoformat())
        assert inserted == 0 and not pending
    row = sql(params, 'SELECT raw_fields FROM source_items')[0]['raw_fields']
    assert row['official_attributes']['tradeID'] == 'T1'
    assert row['report_evidence']['evidence_role'] == 'confirmed_day_gate'
    assert row['report_evidence']['source_sha256'] == 'b' * 64
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_item_id IS NOT NULL')[0]['n'] == 1


def test_daily_sync_links_cash_and_trade_in_one_transaction(isolated_ibkr, monkeypatch):
    params = isolated_ibkr
    sql(params, '''CREATE TABLE pams_data_sync_runs(id bigserial PRIMARY KEY, sync_key text, asset_scope text,
        target_trade_date date, status text, started_at timestamptz, finished_at timestamptz,
        rows_upserted integer, summary_json jsonb)''')
    sql(params, '''INSERT INTO pams_ibkr_trades(account_code,source_account_id,source_trade_key,trade_date,
        symbol,name,asset_category,currency,side,quantity,price,gross_amount,commission,net_cash,realized_pnl,raw_json)
        VALUES ('ibkr','U_TEST','trade-sync',%s,'TEST','Test','STK','CNY','BUY',2,10,20,-1,-21,0,
                '{"accountId":"U_TEST","tradeID":"T2"}'::jsonb)''', (DAY,))
    snapshot = {'status': 'ready', 'cutoff': CUTOFF.isoformat(), 'source_account_id': 'U_TEST',
                'source_sha256': 'c' * 64, 'confirmed_day': DAY.isoformat(),
                'days': {DAY.isoformat(): {'events': [envelope('SYNC')], 'section_ok': True,
                                           'statement_sha256': 'd' * 64}}}
    monkeypatch.setattr(sync, '_snapshot', lambda *args: snapshot)
    with psycopg2.connect(**params) as conn:
        result = sync.sync(conn)
        assert result['status'] == 'success' and result['facts_written'] == 2
        assert sync.sync(conn)['status'] == 'idle'
    assert sql(params, 'SELECT count(*) AS n FROM source_batches WHERE status=\'published\'')[0]['n'] == 2
    assert sql(params, 'SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_item_id IS NOT NULL')[0]['n'] == 2
