"""Real PostgreSQL rollback tests. Never use the application's PG* credentials."""
from __future__ import annotations

from decimal import Decimal
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


@pytest.mark.parametrize("lose_response", [False, True])
def test_late_confirmation_uses_official_transaction_and_recovers_ack(
    sync_case, database, monkeypatch, tmp_path, lose_response,
):
    from datetime import timedelta
    from flex_confirmation import FlexConfirmationJournal
    from refresh_state import read_state, write_state

    client, _, report = sync_case
    now = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(ibkr_report_state, "utc_now", lambda: now)
    path = tmp_path / "scheduler.json"
    history = {"status": "blocked", "last_success_date": None,
               "history_gap_since": "2026-10-20", "history_coverage": "gap"}
    write_state(path, {"version": 1, "jobs": {"asia": history, "north_america": history}})
    calls = []
    responses = []
    def probe():
        # Production GET and POST each obtain their own database connection.
        read_conn = database()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(ibkr, "get_conn", lambda: read_conn)
                response = client.get("/api/ibkr/flex/status")
            assert response.status_code == 200
            return response.json()
        finally:
            read_conn.close()
    def sync(target):
        calls.append(target)
        response = client.post("/api/ibkr/flex/sync", json={
            "align_to_latest_snapshot": False, "expected_statement_reference": str(target),
        })
        responses.append(response.json())
        assert response.status_code == 200
        result = response.json()
        if lose_response and result.get("success"):
            raise TimeoutError("injected after official transaction commit")
        return result
    task = FlexConfirmationJournal(path)
    before = business_state(database)
    assert not task.run(probe, sync, now=now)["success"]
    assert business_state(database) == before  # Delayed report must not mutate any business row.
    assert read_state(path)["jobs"]["ibkr_flex"]["last_success_date"] is None
    report["xml"] = report["xml"].replace("20261022", "20261023")
    result = task.run(probe, sync, now=now + timedelta(minutes=31))
    assert result["success"] and result["publication_verified"], (result, responses)
    assert result["statement_as_of"] == "2026-10-23"
    after = business_state(database)
    stock = next(row for row in after["pams_holdings_snapshot"] if row["symbol"] == "TEST" and str(row["as_of"]) == "2026-10-23")
    assert (stock["market_value"], stock["cost"], stock["quantity"], stock["current_price"]) == (381, 340, 2, 190.5)
    fact = next(row for row in after["pams_investment_trade_fact"] if row["trade_date"] == "2026-10-23")
    # The report is dated 10/23; the fixture only has a 10/22 USD rate.
    assert (fact["gross_amount"], fact["fee_amount"], fact["net_cash"], fact["net_cash_cny"]) == (340, 1.25, -341.25, None)
    assert task.run(probe, sync, now=now + timedelta(hours=1))["success"]
    assert len(calls) == 2 and business_state(database) == after
    jobs = read_state(path)["jobs"]
    assert jobs["asia"] == jobs["north_america"] == history


@pytest.mark.parametrize("stage", ["holdings", "source_file", "source_item", "fact", "success_log"])
def test_failure_rolls_back_all_business_rows_and_success_audit(sync_case, database, monkeypatch, stage):
    client, conn, _ = sync_case
    before = business_state(database)
    from domain.brokers.ibkr import ibkr_report_batch
    module, name = {
        "holdings": (collector, "_write_holdings"),
        "source_file": (ibkr_report_batch, "attach_file"),
        "source_item": (ibkr_report_batch, "add_item"),
        "fact": (ibkr_report_batch, "insert_investment_trade_facts"),
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
    assert len(rows(database, "broker_publications")) == 1
    assert rows(database, "pams_data_job_run") == []


def test_success_keeps_fields_amounts_and_retry_is_idempotent(sync_case, database):
    client, conn, _ = sync_case
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_mode"] == "unified"
    assert result["as_of"] == result["statement_as_of"] == "2026-10-22"
    assert result["history_row_count"] == result["confirmed_funding_count"] == 0
    assert conn.commit_calls == 1
    stock = next(row for row in rows(database, "pams_holdings_snapshot") if row["symbol"] == "TEST")
    assert (stock["market_value"], stock["cost"], stock["quantity"], stock["current_price"]) == (381, 340, 2, 190.5)
    facts = rows(database, "pams_investment_trade_fact")
    assert len(facts) == 2 and all(row["source_item_id"] for row in facts)
    assert all((row["gross_amount"], row["fee_amount"], row["net_cash"], row["net_cash_cny"]) ==
               (340, 1.25, -341.25, -2388.75) for row in facts)
    before = business_state(database)
    repeated = client.post("/api/ibkr/flex/sync", json={}).json()
    assert repeated["sync_run"]["publication"]["state"] == "unchanged"
    assert business_state(database) == before


def test_failure_then_retry_matches_success_amounts(sync_case, database, monkeypatch):
    from domain.brokers.ibkr import ibkr_report_batch
    client, _, _ = sync_case
    original = ibkr_report_batch.publish_report_batch
    before = business_state(database)
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after facts")
    with monkeypatch.context() as patch:
        patch.setattr(ibkr_report_batch, "publish_report_batch", fail)
        assert client.post("/api/ibkr/flex/sync", json={}).status_code == 500
    assert business_state(database) == before
    result = client.post("/api/ibkr/flex/sync", json={}).json()
    assert result["success"] and result["source_mode"] == "unified"
    facts = rows(database, "pams_investment_trade_fact")
    assert len(facts) == 2 and all(Decimal(str(row["net_cash_cny"])) == Decimal("-2388.75") for row in facts)


def test_commit_failure_rolls_back_and_records_failure(sync_case, database):
    client, conn, _ = sync_case
    before = business_state(database)
    conn.fail_commit = True
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert response.json() == {"detail": "injected before commit"}
    assert business_state(database) == before
    assert len(rows(database, "broker_publications")) == 1


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
    assert len(rows(database, "broker_publications")) == 1
    assert rows(database, "pams_data_job_run") == []


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
    assert len(rows(database, "pams_investment_trade_fact")) == 2


def test_failed_audit_cannot_mask_original_error(sync_case, database, monkeypatch, caplog):
    client, _, _ = sync_case
    before = business_state(database)

    def fail_facts(*args, **kwargs):
        raise RuntimeError("original fact failure")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("secondary audit failure")

    from domain.brokers.ibkr import ibkr_report_batch
    monkeypatch.setattr(ibkr_report_batch, "publish_report_batch", fail_facts)
    monkeypatch.setattr(collector, "record_sync_run", fail_audit)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 500
    assert response.json() == {"detail": "original fact failure"}
    assert business_state(database) == before
    assert "ibkr_sync_commit_failed" in caplog.text
    assert len(rows(database, "broker_publications")) == 1


@pytest.mark.parametrize("options,expected", [
    ({"as_of": "2026-10-22"}, "2026-10-22"),
    ({"align_to_latest_snapshot": True}, "2026-10-22"),
])
def test_date_options_keep_existing_selection(sync_case, options, expected):
    client, _, _ = sync_case
    response = client.post("/api/ibkr/flex/sync", json=options)
    assert response.status_code == 200
    assert response.json()["as_of"] == expected
    assert response.json()["statement_as_of"] == "2026-10-22"


@pytest.mark.parametrize("target", ["2026-10-20", "2026-10-24"])
@pytest.mark.parametrize("explicit", [False, True])
def test_misdated_sync_rolls_back_all_business_rows(sync_case, database, monkeypatch, target, explicit):
    client, _, _ = sync_case
    before = business_state(database)
    if explicit:
        options = {"as_of": target}
    else:
        from datetime import date

        monkeypatch.setattr(collector, "_latest_portfolio_as_of", lambda conn: date.fromisoformat(target))
        options = {"align_to_latest_snapshot": True}
    response = client.post("/api/ibkr/flex/sync", json=options)
    assert response.status_code == 502
    assert "does not match IBKR report date" in response.json()["detail"]
    assert business_state(database) == before


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


def test_cash_mode_cutoff_all_types_idempotency_and_no_balance_addition(sync_case, database, monkeypatch):
    client, conn, report = sync_case
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    report["xml"] = cash_report(
        {}, {"transactionID": "OLD65000", "dateTime": "20261021;120000", "amount": "65000"},
        {"transactionID": "W1", "amount": "-20"},
        {"transactionID": "D1", "type": "Dividends", "amount": "4"},
        {"transactionID": "TAX1", "type": "Withholding Tax", "amount": "-1"},
        {"transactionID": "I1", "type": "Broker Interest Received", "amount": "2"},
        {"transactionID": "F1", "type": "Other Fees", "amount": "-3"},
    )
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200, response.text
    result = response.json()["sync_run"]["publication"]["cash_events"]
    assert result["inserted_count"] == 6 and result["historical_count"] == 1
    facts = {row["trade_type"]: row for row in cash_facts(database)}
    assert set(facts) == {"deposit", "withdraw", "dividend", "tax", "interest", "fee"}
    assert facts["deposit"]["net_cash"] == 100 and facts["deposit"]["net_cash_cny"] == 700
    assert facts["fee"]["net_cash"] == -3 and facts["fee"]["fee_amount"] == 3
    assert facts["tax"]["net_cash"] == -1 and facts["tax"]["tax_amount"] == 1
    assert all(not row["affects_position"] and not row["affects_cost_basis"] for row in facts.values())
    assert rows(database, "pams_cashflow_fact") == []
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pams_bank_transfer_matches')")
        assert cur.fetchone()[0] is None
    conn.rollback()
    balance = next(row for row in rows(database, "pams_holdings_snapshot") if row["symbol"] == "CASH_USD")
    assert balance["market_value"] == balance["quantity"] == -500
    before = business_state(database)
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    assert business_state(database) == before
    assert conn.commit_calls == 2


def test_cash_mode_ignores_summary_and_default_off_preserves_legacy_funding(sync_case, database, monkeypatch):
    client, _, report = sync_case
    report["xml"] = cash_report({})
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    assert not cash_facts(database)
    assert rows(database, "pams_cashflow_fact") == []
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    report["xml"] = XML  # A CashReport deposits total is not a single event.
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    assert not cash_facts(database)


@pytest.mark.parametrize("stage", ["cash_writer", "source_fact", "success_log", "commit"])
def test_cash_mode_failure_rolls_back_same_transaction(sync_case, database, monkeypatch, stage):
    from domain.brokers.ibkr import ibkr_cash_events, ibkr_report_batch

    client, conn, report = sync_case
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    report["xml"] = cash_report({})
    before = business_state(database)
    if stage == "commit":
        conn.fail_commit = True
    else:
        module, name = {
            "cash_writer": (ibkr_cash_events, "insert_investment_trade_facts"),
            "source_fact": (ibkr_report_batch, "insert_investment_trade_facts"),
            "success_log": (collector, "record_sync_run"),
        }[stage]
        original = getattr(module, name)

        def fail(*args, **kwargs):
            if stage == "success_log":
                if kwargs["status"] == "failed":
                    return original(*args, **kwargs)
            else:
                original(*args, **kwargs)
            with conn.cursor() as cur:
                cur.execute("SELECT 1/0")
        monkeypatch.setattr(module, name, fail)
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 500
    assert business_state(database) == before
    assert len(rows(database, "broker_publications")) == 1


@pytest.mark.parametrize("in_one_report", [False, True])
def test_cash_mode_revision_waits_without_blocking_other_official_events(sync_case, database, monkeypatch, in_one_report):
    client, _, report = sync_case
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    if not in_one_report:
        report["xml"] = cash_report({})
        assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    before = cash_facts(database)
    good = {"transactionID": "KNOWN", "type": "Dividends", "amount": "3"}
    report["xml"] = cash_report({}, {"amount": "101"}, good) if in_one_report else cash_report({"amount": "101"}, good)
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200
    pending = response.json()["sync_run"]["cash_events"]["pending"]
    assert pending[0]["reason"] == "conflicting_cash_transaction" and not pending[0]["retryable"]
    after = cash_facts(database)
    assert all(row in after for row in before)
    assert len(after) == len(before) + 1
    assert any(row["trade_type"] == "dividend" and row["net_cash"] == 3 for row in after)


def test_cash_mode_pending_unknown_invalid_and_missing_fx_then_same_xml_retry(sync_case, database, monkeypatch):
    client, conn, report = sync_case
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    report["xml"] = cash_report({"transactionID": "UNK", "type": "Adjustment"},
                                {"transactionID": "BAD", "amount": "NaN"},
                                {"transactionID": "JPY1", "currency": "JPY"})
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200
    assert not cash_facts(database)
    assert len(response.json()["sync_run"]["publication"]["cash_events"]["pending"]) == 3
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,rate,as_of,source) VALUES ('CNY','JPY',0.05,'2026-10-22','synthetic')")
    conn.commit()
    # Reprocessing identical XML must allow a previously missing dated FX input to become available.
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    assert cash_facts(database)[0]["currency"] == "JPY"
    assert cash_facts(database)[0]["net_cash_cny"] == 5


@pytest.mark.parametrize("candidate_count", [1, 2])
def test_cash_mode_links_only_unique_confirmed_bank_identity(sync_case, database, monkeypatch, candidate_count):
    client, conn, report = sync_case
    with conn.cursor() as cur:
        for i in range(candidate_count):
            cur.execute("""INSERT INTO pams_cashflow_fact
                (event_date, account_code, counter_account_code, amount, currency, amount_cny,
                 cashflow_type, source_table, source_id, source_hash, review_status)
                VALUES ('2026-10-21','test_bank','ibkr',-100,'USD',-700,
                        'investment_funding','test',%s,%s,'confirmed')""", (str(i), 'synthetic-'+str(i)))
    conn.commit()
    bank_before = rows(database, "pams_cashflow_fact")
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    report["xml"] = cash_report({})
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 200
    assert cash_facts(database)[0]["cashflow_id"] is None
    assert rows(database, "pams_cashflow_fact") == bank_before


def independent_status(database):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    conn = database()
    try:
        with conn:
            return service.status(conn)
    finally:
        conn.close()


def test_published_cash_sync_source_hash_no_balance_writes_and_single_commit(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    manifest, source, digest = publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [
        {}, {"transactionID": "UNKNOWN", "type": "Unknown"},
        {"transactionID": "FEE", "type": "Other Fees", "amount": "-2"}]})
    originals = (manifest.read_bytes(), source.read_bytes())
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    before = business_state(database)
    result = service.sync(conn)
    assert result["status"] == "success" and result["facts_written"] == 2
    assert result["confirmed_day"] == "2026-10-22" and result["source_sha256"] == digest
    assert result["broker_source"] == service.BROKER_SOURCE and conn.commit_calls == 1
    after = business_state(database)
    assert all(after[table] == before[table] for table in ("pams_holdings_snapshot", "pams_cashflow_fact"))
    assert all(row in after[table] for table in ("source_batches", "source_files", "source_items") for row in before[table])
    assert len(after["pams_investment_trade_fact"]) == len(before["pams_investment_trade_fact"]) + 2
    assert all(row["raw_json"]["source_row"]["source_evidence"]["source_sha256"] == digest for row in cash_facts(database))
    progress = rows(database, "broker_cash_progress")
    assert len(progress) == 1 and progress[0]["report_sha256"] == digest
    current = independent_status(database)
    assert current["confirmed_day"] == "2026-10-22" and current["waiting_evidence_count"] == 3
    assert current["retryable_pending_count"] == 0 and not current["next_days"]
    assert service.sync(conn)["status"] == "idle" and conn.commit_calls == 1
    assert business_state(database) == after
    assert (manifest.read_bytes(), source.read_bytes()) == originals


def test_published_cash_cutoff_protects_all_old_events_and_off_is_disabled(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [{"amount": "65000"}]})
    before = business_state(database)
    assert service.sync(conn)["status"] == "disabled"
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-23")
    result = service.sync(conn)
    assert result["facts_written"] == 0 and result["status"] == "idle"
    assert result["historical_day_count"] == 1 and business_state(database) == before
    assert conn.commit_calls == 0


@pytest.mark.parametrize("stage", ["writer", "commit"])
def test_published_cash_sync_database_failure_rolls_back_audit_and_facts(sync_case, database, monkeypatch, tmp_path, stage):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [{}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    before = business_state(database)
    audit_before = rows(database, "broker_cash_progress")
    if stage == "writer":
        original = service.cash.insert_investment_trade_facts

        def fail(cur, facts, **kwargs):
            original(cur, facts, **kwargs)
            cur.execute("SELECT 1/0")
        monkeypatch.setattr(service.cash, "insert_investment_trade_facts", fail)
    else:
        conn.fail_commit = True
    with pytest.raises(Exception, match="division by zero|injected before commit"):
        service.sync(conn)
    assert business_state(database) == before
    assert rows(database, "broker_cash_progress") == audit_before


def test_published_cash_retryable_fx_is_persistent_and_retries_same_day(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [
        {"transactionID": "JPY1", "currency": "JPY", "type": "Dividends"},
        {"transactionID": "BAD", "type": "Unsupported"}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    assert service.sync(conn)["facts_written"] == 0
    current = independent_status(database)
    assert current["retryable_pending_count"] == 1 and current["waiting_evidence_count"] == 2
    assert current["next_days"] == ["2026-10-22"]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,rate,as_of,source) VALUES ('CNY','JPY',0.05,'2026-10-22','synthetic')")
    conn.commit()
    result = service.sync(conn)
    assert result["facts_written"] == 1 and result["retryable_pending_count"] == 0
    assert result["waiting_evidence_count"] == 2 and cash_facts(database)[0]["net_cash_cny"] == 5
    assert independent_status(database)["confirmed_day"] == "2026-10-22"


def test_published_cash_sync_is_bounded_progresses_and_does_not_retry_permanent_unknown(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    publish_cash_days(tmp_path, monkeypatch, {f"2026-10-{day}": [
        {"transactionID": f"C{day}", "type": "Dividends" if day != 19 else "Unsupported"}]
        for day in (19, 20, 21, 22)})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-18")
    first = service.sync(conn, max_days=2)
    assert [day["day"] for day in first["days"]] == ["2026-10-19", "2026-10-20"]
    second = service.sync(conn, max_days=2)
    assert [day["day"] for day in second["days"]] == ["2026-10-21", "2026-10-22"]
    assert second["facts_written"] == 2 and second["waiting_evidence_count"] == 5
    assert len(cash_facts(database)) == 3 and service.sync(conn)["status"] == "idle"


def test_published_cash_concurrent_syncs_are_serialized_and_idempotent(sync_case, database, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, _, _ = sync_case
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [{}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    entered, release = Event(), Event()
    original = service.cash.import_cash_events

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test transaction release timed out"
        return original(*args, **kwargs)

    monkeypatch.setattr(service.cash, "import_cash_events", hold)

    def run():
        conn = database()
        try:
            return service.sync(conn)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert entered.wait(10), "first sync did not reach its transaction"
        second = executor.submit(run)
        try:
            assert not second.done()
        finally:
            release.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(result["facts_written"] for result in results) == [0, 1]
    assert len(cash_facts(database)) == 1
    assert len(rows(database, "broker_cash_progress")) == 1


def test_published_cash_tampered_source_is_rejected_before_writes(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    _, source, _ = publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [{}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    source.write_bytes(source.read_bytes() + b" ")
    before = business_state(database)
    result = service.sync(conn)
    assert result["status"] == "unavailable" and result["reasons"] == ["source_sha256_mismatch"]
    assert business_state(database) == before and conn.commit_calls == 0


@pytest.mark.parametrize("missing", [False, True])
def test_published_cash_explicit_empty_section_vs_missing_and_stable_status(sync_case, database, monkeypatch, tmp_path, missing):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    disabled = service.status(None)
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": []},
                      missing_sections={"2026-10-22"} if missing else ())
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    ready = independent_status(database)
    assert set(ready) == set(disabled)
    before = business_state(database)
    result = service.sync(conn)
    after = business_state(database)
    assert result["facts_written"] == 0
    assert all(after[table] == before[table] for table in ("pams_holdings_snapshot", "pams_cashflow_fact", "pams_investment_trade_fact"))
    assert all(row in after[table] for table in ("source_batches", "source_files", "source_items") for row in before[table])
    assert result["days"][0]["complete"] is False  # The separate trade section is unavailable.
    assert result["days"][0]["cash_section_present"] == (not missing)
    assert result["blocking_pending_count"] == 1 + int(missing)
    assert result["retryable_pending_count"] == 0  # Missing sections require changed source, not an FX retry loop.
    assert independent_status(database)["blocking_pending_count"] == 1 + int(missing)


def test_published_cash_changed_source_reports_revision_and_keeps_known_updates(sync_case, database, monkeypatch, tmp_path):
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    _, _, first_hash = publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [{}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    assert service.sync(conn)["facts_written"] == 1
    saved = cash_facts(database)[0]
    _, _, new_hash = publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [
        {"amount": "101"}, {"transactionID": "DIV2", "type": "Dividends", "amount": "2"}]})
    assert first_hash != new_hash
    assert independent_status(database)["next_days"] == ["2026-10-22"]
    result = service.sync(conn)
    assert result["source_sha256"] == new_hash and result["facts_written"] == 1
    assert result["pending"][0]["reason"] == "conflicting_cash_transaction"
    assert saved in cash_facts(database)
    assert result["blocking_pending_count"] == 2


def test_cash_fact_consumers_preserve_balance_position_cost_and_official_accrual_nav(sync_case, database, monkeypatch, tmp_path):
    from copy import deepcopy
    from datetime import date
    from psycopg2.extras import RealDictCursor
    from domain.accounts import account_balance
    from domain.investments import holdings_positions
    from domain.investments import investment_return_ibkr
    from domain.brokers.ibkr import ibkr_cash_event_sync as service
    from domain.brokers.ibkr import ibkr_daily_close_read as daily
    from tests.brokers.ibkr.test_ibkr_cash_events import publish_cash_days

    _, conn, _ = sync_case
    with conn.cursor() as cur:
        cur.execute("""UPDATE pams_holdings_snapshot SET market_value=100, cost=100, quantity=100
                WHERE symbol='CASH_USD'""")
    conn.commit()
    publish_cash_days(tmp_path, monkeypatch, {"2026-10-22": [
        {"type": "Broker Interest Received", "currency": "USD", "amount": "2"},
        {"transactionID": "DIV", "type": "Dividends", "currency": "USD", "symbol": "TEST", "amount": "3"},
        {"transactionID": "FEE", "type": "Other Fees", "currency": "USD", "amount": "-1"}]})
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    monkeypatch.setattr(investment_return_ibkr, "_fetch_yfinance_closes", lambda *a, **k: (
        {"TEST": {date(2026, 10, 22): Decimal("190.5"), date(2026, 10, 23): Decimal("190.5")}}, []))

    def consume():
        read = database()
        try:
            with read, read.cursor(cursor_factory=RealDictCursor) as cur:
                basis = account_balance.load_account_balance_basis(cur, account_code="ibkr", owner_ratio=Decimal(1))
                cur.execute("SELECT *, account_code AS account FROM pams_holdings_snapshot ORDER BY symbol")
                holdings = holdings_positions._enrich_realized_trade_pnl(cur, deepcopy([dict(row) for row in cur.fetchall()]))
                closed = holdings_positions._closed_position_rows(cur)
                equities = investment_return_ibkr._ibkr_full_trade_return_series(cur,
                    dates=[date(2026, 10, 22), date(2026, 10, 23)], base_currency="CNY",
                    fx_timelines={"USD": [(date(2026, 10, 21), Decimal(7))]})
                return basis, [(h["symbol"], h["quantity"], h["cost"], h["market_value"]) for h in holdings], closed, equities
        finally:
            read.close()

    before, official = consume(), daily.read_close(as_of=date(2026, 10, 22))
    assert service.sync(conn)["facts_written"] == 3
    assert consume() == before
    assert daily.read_close(as_of=date(2026, 10, 22)) == official
    assert official["nav_base"] == 100  # Interest already represented in the broker's cash/NAV, never add it again.
    assert not any(row["symbol"] == "CASH_USD" and row["quantity"] is not None for row in cash_facts(database))


@pytest.mark.parametrize("cutoff,expected_cash", [("2026-10-21", 1), ("2026-10-22", 0)])
def test_enabling_cash_on_identical_xml_never_replays_old_business_rows(sync_case, database, monkeypatch, cutoff, expected_cash):
    client, _, report = sync_case
    report["xml"] = cash_report({})
    first = client.post("/api/ibkr/flex/sync", json={})
    assert first.status_code == 200
    before = business_state(database)
    identity = first.json()["sync_run"]["publication"]["data_sha256"]
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", cutoff)

    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200, response.text
    assert response.json()["sync_run"]["publication"]["data_sha256"] == identity
    after = business_state(database)
    assert all(after[table] == before[table] for table in ("pams_holdings_snapshot", "pams_cashflow_fact"))
    assert all(row in after[table] for table in ("source_batches", "source_files", "source_items") for row in before[table])
    assert all(row in after["pams_investment_trade_fact"] for row in before["pams_investment_trade_fact"])
    assert len(cash_facts(database)) == expected_cash
    if not expected_cash:
        assert all(after[table] == before[table] for table in TABLES
                   if table != "broker_publications")
        assert all(row in after["broker_publications"]
                   for row in before["broker_publications"])
        new_publications = [row for row in after["broker_publications"]
                            if row not in before["broker_publications"]]
        assert len(new_publications) == 1
        assert new_publications[0]["cash_state"]["cutoff"] == cutoff


def test_cutoff_keeps_existing_facts_but_updates_current_report_holdings(sync_case, database, monkeypatch):
    client, _, report = sync_case
    before = business_state(database)
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-22")
    report["xml"] = cash_report({})
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200, response.text
    after = business_state(database)
    assert all(after[table] == before[table] for table in ("pams_cashflow_fact", "pams_investment_trade_fact"))
    assert next(row for row in after["pams_holdings_snapshot"] if row["symbol"] == "TEST")["market_value"] == 381
    assert all(row in after[table] for table in ("source_batches", "source_files", "source_items") for row in before[table])
    assert len(after["source_files"]) > len(before["source_files"])


def test_cash_event_waits_for_sourced_fx_and_replays_same_original(sync_case,database,monkeypatch):
    client,conn,report=sync_case
    monkeypatch.setenv('PAMS_IBKR_CASH_EVENTS_AFTER','2026-10-21')
    report['xml']=cash_report({'transactionID':'JPY_SOURCE','currency':'JPY'})
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,rate,as_of,source) VALUES ('CNY','JPY',0.05,'2026-10-22','')")
    conn.commit()
    response=client.post('/api/ibkr/flex/sync',json={})
    assert response.status_code==200,response.text
    assert not cash_facts(database)
    pending=response.json()['sync_run']['publication']['cash_events']['pending']
    assert any(item['reason']=='missing_or_invalid_dated_fx' for item in pending)
    with conn.cursor() as cur:
        cur.execute("UPDATE pams_fx_rates_daily SET source='synthetic' WHERE quote_currency='JPY' AND as_of='2026-10-22'")
    conn.commit()
    response=client.post('/api/ibkr/flex/sync',json={})
    assert response.status_code==200,response.text
    assert len(cash_facts(database))==1 and cash_facts(database)[0]['currency']=='JPY'
    assert client.post('/api/ibkr/flex/sync',json={}).status_code==200
    assert len(cash_facts(database))==1
