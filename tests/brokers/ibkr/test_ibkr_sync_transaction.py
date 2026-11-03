"""Shared isolated Flex fixtures and the legacy endpoint credential guard."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, connection, parse_dsn
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend" / "api"))
sys.path.insert(0, str(ROOT / "backend" / "scheduler"))

from collectors import ibkr_flex as collector  # noqa: E402
from endpoints import ibkr  # noqa: E402
from domain.brokers.ibkr import ibkr_report_state  # noqa: E402

XML = """<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U_TEST" toDate="20261022">
<OpenPosition accountId="U_TEST" symbol="TEST" description="TEST STOCK"
 currency="USD" reportDate="20261022" position="2" markPrice="190.5"
 marketValue="381" positionValue="381" costBasisMoney="340" costBasisPrice="170" assetCategory="STK" />
<CashReportCurrency currency="BASE_SUMMARY" toDate="20261022"
 endingCash="-500" depositWithdrawalsYTD="100" />
<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022"
 endingCash="-500" deposits="100" />
<Trade accountId="U_TEST" symbol="TEST" description="TEST STOCK" assetCategory="STK"
 currency="USD" tradeID="T1" tradeDate="20261022" buySell="BUY" quantity="2"
 tradePrice="170" tradeMoney="340" ibCommission="-1.25" netCash="-341.25" />
</FlexStatement></FlexStatements></FlexQueryResponse>"""

TABLES = (
    "pams_holdings_snapshot", "pams_cashflow_fact", "pams_investment_trade_fact",
    "source_batches", "source_files", "source_items", "valuation_evidence", "evidence_confirmation",
    "broker_publications", "broker_cash_progress",
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


@pytest.fixture
def database():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set PAMS_TEST_POSTGRES_DSN to an isolated PostgreSQL test server")
    # Only the marked local PG16 test server can create a disposable database.
    params = parse_dsn(dsn)
    if params.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("explicit local disposable test server required")
    name = "pams_import_test_" + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    created = False
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
            if admin.server_version // 10000 != 16 or cur.fetchone()[0] != "pams-disposable-schema-test":
                raise ValueError("marked disposable PG16 test server required")
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            created = True
            cur.execute(sql.SQL("COMMENT ON DATABASE {} IS 'pams-isolated-account-import'").format(sql.Identifier(name)))
        yield lambda **kwargs: psycopg2.connect(dsn, dbname=name, **kwargs)
    finally:
        if created:
            with admin.cursor() as cur:
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def sync_case(database, monkeypatch, tmp_path):
    monkeypatch.delenv("PAMS_IBKR_CASH_EVENTS_AFTER", raising=False)
    monkeypatch.setattr(ibkr_report_state, "utc_now", lambda: datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
    monkeypatch.setenv("PAMS_UNIFIED_SOURCE_ISOLATED", "true")
    monkeypatch.setenv("PAMS_HOLDING_EVIDENCE_ISOLATED", "true")
    conn = database(connection_factory=ObservedConnection)
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    initialize_isolated_sources(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(as_of,base_currency,quote_currency,rate,source) VALUES ('2026-10-22','CNY','USD',7,'synthetic')")
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Synthetic bank') ON CONFLICT DO NOTHING")
    conn.commit()
    monkeypatch.setenv("PAMS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ibkr, "_credentials", lambda: ("test-token", "test-query"))
    monkeypatch.setattr(ibkr, "get_conn", lambda: conn)
    monkeypatch.setattr(collector, "request_flex_reference", lambda *a, **k: {"reference_code": "TEST"})
    fixture_xml = XML.replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD" taxAmount="0"')
    report = {"xml": fixture_xml.replace('tradeID="T1"', 'tradeID="T0"').replace('deposits="100"', 'deposits="0"')}
    monkeypatch.setattr(collector, "fetch_flex_statement", lambda *a, **k: report["xml"])
    app = FastAPI()
    app.include_router(ibkr.router, prefix="/api")
    with TestClient(app) as client:
        first = client.post("/api/ibkr/flex/sync", json={})
        assert first.status_code == 200, first.text
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_holdings_snapshot SET market_value=123 WHERE symbol='TEST'")
        conn.commit()
        report["xml"] = fixture_xml
        conn.commit_calls = 0
        yield client, conn, report
    conn.close()


def rows(database, table):
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            if table == "pams_data_job_run":
                cur.execute("SELECT to_regclass('public.pams_data_job_run')")
                assert cur.fetchone()[0] is None
                return []
            if table in {"broker_publications", "broker_cash_progress"}:
                kind = "publication" if table == "broker_publications" else "cash_progress"
                cur.execute("SELECT row_to_json(t) FROM broker_processing_state t WHERE state_type=%s ORDER BY row_to_json(t)::text", (kind,))
            else:
                cur.execute(sql.SQL("SELECT row_to_json(t) FROM {} t ORDER BY row_to_json(t)::text")
                            .format(sql.Identifier(table)))
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def business_state(database):
    return {table: rows(database, table) for table in TABLES}


def cash_report(*events):
    from xml.sax.saxutils import quoteattr

    records = []
    for overrides in events:
        attrs = {"accountId": "U_TEST", "transactionID": "C1", "currency": "USD",
                 "dateTime": "20261022;120000", "amount": "100", "type": "Deposits & Withdrawals"}
        attrs.update(overrides)
        records.append("<CashTransaction " + " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items()) + " />")
    return XML.replace("</FlexStatement>", "".join(records) + "</FlexStatement>")


def cash_facts(database):
    return [row for row in rows(database, "pams_investment_trade_fact")
            if row["source_table"] == "ibkr_flex_cash_transaction"]


def test_missing_credentials_does_not_open_database(monkeypatch):
    monkeypatch.setattr(ibkr, "_credentials", lambda: (None, None))

    def unexpected_connection():
        pytest.fail("Missing credentials must not open a database connection")

    monkeypatch.setattr(ibkr, "get_conn", unexpected_connection)
    with pytest.raises(HTTPException) as error:
        ibkr.ibkr_flex_sync(ibkr.IBKRFlexSyncRequest())
    assert error.value.status_code == 400
