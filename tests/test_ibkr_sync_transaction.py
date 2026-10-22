"""Real PostgreSQL rollback tests. Never use the application's PG* credentials."""
from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, connection
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from collectors import ibkr_flex as collector  # noqa: E402
from endpoints import ibkr  # noqa: E402

XML = """<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U_TEST" toDate="20261022">
<OpenPosition accountId="U_TEST" symbol="TEST" description="TEST STOCK"
 currency="USD" reportDate="20261022" position="2" markPrice="190.5"
 marketValue="381" costBasisMoney="340" costBasisPrice="170" assetCategory="STK" />
<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022"
 endingCash="-500" deposits="100" />
<Trade accountId="U_TEST" symbol="TEST" description="TEST STOCK" assetCategory="STK"
 currency="USD" tradeID="T1" tradeDate="20261022" buySell="BUY" quantity="2"
 tradePrice="170" tradeMoney="340" ibCommission="-1.25" netCash="-341.25" />
</FlexStatement></FlexStatements></FlexQueryResponse>"""

TABLES = (
    "pams_holdings_snapshot", "pams_ibkr_position_history", "pams_ibkr_trades",
    "pams_cashflow_fact", "pams_bank_transfer_matches", "pams_investment_trade_fact",
)


class ObservedConnection(connection):
    commit_calls = 0
    fail_commit = False

    def commit(self):
        self.commit_calls += self.get_transaction_status() != TRANSACTION_STATUS_IDLE
        if self.fail_commit:
            self.fail_commit = False
            raise RuntimeError("injected before commit")
        return super().commit()


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set PAMS_TEST_POSTGRES_DSN to an isolated PostgreSQL test server")
    # All DDL and writes target a newly-created disposable database, never the DSN's database.
    name = "pams_ibkr_tx_test_" + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        yield lambda **kwargs: psycopg2.connect(dsn, dbname=name, **kwargs)
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def sync_case(database, monkeypatch, tmp_path):
    conn = database(connection_factory=ObservedConnection)
    with conn.cursor() as cur:
        cur.execute("""
            DROP SCHEMA public CASCADE;
            CREATE SCHEMA public;
            CREATE TABLE pams_accounts (
                account_code varchar(80) PRIMARY KEY, display_name text,
                base_currency text, is_asset boolean, active boolean
            );
            CREATE TABLE pams_asset_master (asset_id varchar(120) PRIMARY KEY);
            CREATE TABLE pams_holdings_snapshot (
                id bigserial PRIMARY KEY, symbol text, name text, asset_liability text,
                currency text, account_code text, major_category text, minor_category text,
                market_value numeric(28,10), cost numeric(28,10), quantity numeric(28,10),
                buy_price numeric(28,10), current_price numeric(28,10), as_of date,
                updated_at timestamptz DEFAULT now(), UNIQUE(account_code,name,as_of)
            );
            CREATE TABLE pams_fx_rates_daily (
                base_currency text, quote_currency text, rate numeric, as_of date
            );
            INSERT INTO pams_fx_rates_daily VALUES ('CNY','USD',7,'2026-10-22');
            CREATE TABLE pams_data_job_run (
                job_key text, job_type text, source_table text, source_id text,
                asset_scope text, target_date date, data_as_of date, status text,
                started_at timestamptz, finished_at timestamptz, rows_affected integer,
                summary_json jsonb, error_text text, UNIQUE(source_table,source_id)
            );
        """)
        for filename in ("022_bank_reconcile.sql", "024_cashflow_and_trade_fact.sql"):
            cur.execute((ROOT / "database/postgres/schema" / filename).read_text(encoding="utf-8"))
    conn.commit()
    monkeypatch.setenv("PAMS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ibkr, "_credentials", lambda: ("test-token", "test-query"))
    monkeypatch.setattr(ibkr, "get_conn", lambda: conn)
    monkeypatch.setattr(collector, "request_flex_reference", lambda *a, **k: {"reference_code": "TEST"})
    report = {"xml": XML.replace('tradeID="T1"', 'tradeID="T0"').replace('deposits="100"', 'deposits="0"')}
    monkeypatch.setattr(collector, "fetch_flex_statement", lambda *a, **k: report["xml"])
    app = FastAPI()
    app.include_router(ibkr.router, prefix="/api")
    with TestClient(app) as client:
        assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pams_holdings_snapshot SET market_value=123 WHERE symbol='TEST';
                INSERT INTO pams_cashflow_fact (
                    event_date,account_code,counter_account_code,amount,currency,amount_cny,
                    cashflow_type,transfer_group_id,source_table,source_id,source_hash
                ) VALUES ('2026-10-21','test_bank','ibkr',-100,'USD',-700,
                    'internal_transfer','account_transfer:test','test','1','test_hash');
                INSERT INTO pams_bank_transfer_matches (
                    match_key,match_type,relation_code,from_account_code,to_account_code,
                    amount,currency,note
                ) VALUES ('account_transfer:test','internal_transfer','test',
                    'test_bank','ibkr',100,'USD','keep user note');
            """)
        conn.commit()
        report["xml"] = XML
        conn.commit_calls = 0
        yield client, conn, report
    conn.close()


def rows(database, table):
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT row_to_json(t) FROM {} t ORDER BY row_to_json(t)::text")
                        .format(sql.Identifier(table)))
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def business_state(database):
    return {table: rows(database, table) for table in TABLES}


@pytest.mark.parametrize("stage", ["holdings", "history", "trades", "funding", "facts", "success_log"])
def test_failure_rolls_back_all_business_rows_and_success_audit(sync_case, database, monkeypatch, stage):
    client, conn, _ = sync_case
    before = business_state(database)
    module, name = {
        "holdings": (collector, "_write_holdings"),
        "history": (collector, "_write_position_history"),
        "trades": (collector, "_write_trades"),
        "funding": (collector, "_confirm_investment_funding_cashflows"),
        "facts": (ibkr, "backfill_investment_trade_facts"),
        "success_log": (collector, "record_sync_run"),
    }[stage]
    original = getattr(module, name)

    def fail(*args, **kwargs):
        if stage == "success_log":
            if kwargs["status"] == "failed":
                return original(*args, **kwargs)
        else:
            original(*args, **kwargs)
        # Exercise PostgreSQL's aborted-transaction state, not just a Python exception.
        with conn.cursor() as cur:
            cur.execute("SELECT 1/0")

    monkeypatch.setattr(module, name, fail)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert "division by zero" in response.json()["detail"]
    assert business_state(database) == before
    assert [row["status"] for row in rows(database, "pams_data_sync_runs")].count("success") == 1
    for table in ("pams_data_sync_runs", "pams_data_job_run"):
        assert sorted(row["status"] for row in rows(database, table)) == ["failed", "success"]


def test_success_keeps_fields_amounts_and_retry_is_idempotent(sync_case, database):
    client, conn, _ = sync_case
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {
        "success", "status", "as_of", "statement_as_of", "row_count", "trade_count",
        "history_row_count", "confirmed_funding_count", "confirmed_fundings", "raw_file",
        "reference_code", "sync_run", "trade_fact_backfill",
    }
    assert result["as_of"] == result["statement_as_of"] == "2026-10-22"
    assert (result["row_count"], result["history_row_count"], result["trade_count"]) == (2, 2, 1)
    assert result["trade_fact_backfill"] == {"source_count": 1, "inserted_count": 1, "skipped_count": 0}
    assert result["confirmed_funding_count"] == 1
    assert set(result["sync_run"]) == {
        "id", "source", "asset_scope", "status", "started_at", "finished_at", "as_of",
        "statement_as_of", "row_count", "trade_count", "history_row_count",
        "confirmed_funding_count", "raw_file", "message", "error",
    }
    assert conn.commit_calls == 1
    stock = next(row for row in rows(database, "pams_holdings_snapshot") if row["symbol"] == "TEST")
    assert (stock["market_value"], stock["cost"], stock["quantity"], stock["current_price"]) == (381, 340, 2, 190.5)
    trade = next(row for row in rows(database, "pams_ibkr_trades") if row["trade_id"] == "T1")
    fact = next(row for row in rows(database, "pams_investment_trade_fact") if row["source_id"] == str(trade["id"]))
    assert (fact["gross_amount"], fact["fee_amount"], fact["net_cash"], fact["net_cash_cny"]) == (340, 1.25, -341.25, -2388.75)
    funding = rows(database, "pams_cashflow_fact")[0]
    assert funding["cashflow_type"] == "investment_funding"
    assert funding["amount"] == -100 and funding["amount_cny"] == -700
    assert rows(database, "pams_bank_transfer_matches")[0]["note"].startswith("keep user note")
    counts = {table: len(rows(database, table)) for table in TABLES}
    repeated = client.post("/api/ibkr/flex/sync", json={}).json()
    assert repeated["trade_fact_backfill"] == {"source_count": 0, "inserted_count": 0, "skipped_count": 0}
    assert repeated["confirmed_funding_count"] == 0
    assert {table: len(rows(database, table)) for table in TABLES} == counts
    assert all(row["net_cash_cny"] == fact["net_cash_cny"] for row in rows(database, "pams_investment_trade_fact"))


def test_failure_then_retry_matches_success_amounts(sync_case, database, monkeypatch):
    client, _, _ = sync_case
    original = ibkr.backfill_investment_trade_facts

    def fail(cur):
        original(cur)
        raise RuntimeError("injected after facts")

    with monkeypatch.context() as patch:
        patch.setattr(ibkr, "backfill_investment_trade_facts", fail)
        assert client.post("/api/ibkr/flex/sync", json={}).status_code == 500
    result = client.post("/api/ibkr/flex/sync", json={}).json()
    assert result["trade_fact_backfill"]["inserted_count"] == 1
    assert result["confirmed_funding_count"] == 1
    facts = rows(database, "pams_investment_trade_fact")
    assert len(facts) == 2
    assert all(Decimal(str(row["net_cash_cny"])) == Decimal("-2388.75") for row in facts)


def test_commit_failure_rolls_back_and_records_failure(sync_case, database):
    client, conn, _ = sync_case
    before = business_state(database)
    conn.fail_commit = True
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert response.json() == {"detail": "injected before commit"}
    assert business_state(database) == before
    assert sorted(row["status"] for row in rows(database, "pams_data_sync_runs")) == ["failed", "success"]


def test_postgres_rejects_deferred_constraint_at_commit(sync_case, database):
    client, conn, _ = sync_case
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE test_allowed_source (source_id text PRIMARY KEY);
            INSERT INTO test_allowed_source SELECT source_id FROM pams_investment_trade_fact;
            ALTER TABLE pams_investment_trade_fact ADD CONSTRAINT test_deferred_source
                FOREIGN KEY (source_id) REFERENCES test_allowed_source(source_id)
                DEFERRABLE INITIALLY DEFERRED;
        """)
    conn.commit()
    before = business_state(database)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert "test_deferred_source" in response.json()["detail"]
    assert business_state(database) == before
    for table in ("pams_data_sync_runs", "pams_data_job_run"):
        assert sorted(row["status"] for row in rows(database, table)) == ["failed", "success"]


@pytest.mark.parametrize("kind,status", [(collector.IBKRFlexError, 502), (RuntimeError, 500)])
def test_download_errors_keep_http_contract(sync_case, database, monkeypatch, kind, status):
    client, _, _ = sync_case
    before = business_state(database)

    def fail(*args, **kwargs):
        raise kind("injected download failure")

    monkeypatch.setattr(collector, "fetch_flex_statement", fail)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == status
    assert response.json() == {"detail": "injected download failure"}
    assert business_state(database) == before


def test_missing_credentials_does_not_open_database(monkeypatch):
    monkeypatch.setattr(ibkr, "_credentials", lambda: (None, None))

    def unexpected_connection():
        pytest.fail("Missing credentials must not open a database connection")

    monkeypatch.setattr(ibkr, "get_conn", unexpected_connection)
    with pytest.raises(HTTPException) as error:
        ibkr.ibkr_flex_sync(ibkr.IBKRFlexSyncRequest())
    assert error.value.status_code == 400


def test_standalone_collector_retains_default_commit(sync_case, database):
    _, conn, _ = sync_case
    result = collector.sync_ibkr_flex_to_db(conn, token="test-token", query_id="test-query")
    assert result["status"] == "success"
    assert "trade_fact_backfill" not in result
    assert conn.commit_calls == 1
    assert len(rows(database, "pams_ibkr_trades")) == 2


def test_failed_audit_cannot_mask_original_error(sync_case, database, monkeypatch, caplog):
    client, _, _ = sync_case
    before = business_state(database)

    def fail_facts(cur):
        raise RuntimeError("original fact failure")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("secondary audit failure")

    monkeypatch.setattr(ibkr, "backfill_investment_trade_facts", fail_facts)
    monkeypatch.setattr(collector, "record_sync_run", fail_audit)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert response.json() == {"detail": "original fact failure"}
    assert business_state(database) == before
    assert "Could not persist failed IBKR Flex sync audit" in caplog.text
    assert len(rows(database, "pams_data_sync_runs")) == 1


@pytest.mark.parametrize("options,expected", [
    ({"as_of": "2026-10-20"}, "2026-10-20"),
    ({"align_to_latest_snapshot": True}, "2026-10-22"),
])
def test_date_options_keep_existing_selection(sync_case, options, expected):
    client, _, _ = sync_case
    response = client.post("/api/ibkr/flex/sync", json=options)
    assert response.status_code == 200
    assert response.json()["as_of"] == expected
    assert response.json()["statement_as_of"] == "2026-10-22"
