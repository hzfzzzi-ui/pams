"""Synthetic adapter evidence and disposable PostgreSQL only, never production."""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import psycopg2.extras
from psycopg2.extensions import connection, ISOLATION_LEVEL_REPEATABLE_READ
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_account_audit as oracle
from domain.brokers.ibkr import ibkr_account_audit_read as service
from domain.brokers.ibkr import ibkr_performance as performance
from domain.brokers.ibkr import ibkr_report_state as report_state
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows

DAY = date(2026, 10, 22)
START = date(2026, 10, 1)
XML = b'''<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U_TEST" fromDate="20261001" toDate="20261022">
<AccountInformation accountId="U_TEST" currency="USD" />
<CashReport><CashReportCurrency accountId="U_TEST" currency="BASE_SUMMARY"
 fromDate="20261022" toDate="20261022" endingCash="-20" depositWithdrawalsYTD="100" levelOfDetail="BaseCurrency" />
<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022" endingCash="-20" levelOfDetail="Currency" /></CashReport>
<OpenPositions><OpenPosition accountId="U_TEST" conid="synthetic" symbol="TEST" description="TEST STOCK"
 currency="USD" reportDate="20261022" position="2" markPrice="50" positionValue="100"
 costBasisMoney="90" costBasisPrice="45" fifoPnlUnrealized="10" fxRateToBase="1"
 assetCategory="STK" levelOfDetail="SUMMARY" /></OpenPositions>
<EquitySummaryByReportDateInBase accountId="U_TEST" reportDate="20261022" total="83" interestAccruals="3" />
<ChangeInNAV accountId="U_TEST" fromDate="20261001" toDate="20261022" startingValue="50" endingValue="83"
 depositsWithdrawals="20" internalCashTransfers="0" assetTransfers="0" linkingAdjustments="0" debitCardActivity="0" billPay="0" />
</FlexStatement></FlexStatements></FlexQueryResponse>'''


def check(result, key):
    return next(item for item in result["checks"] if item["id"] == key)


def reasons(result, key):
    return set(check(result, key)["reason_counts"])


@pytest.fixture
def local_case(tmp_path, monkeypatch):
    statements = []
    case = {"history_present": True, "dates": [DAY]}
    path = tmp_path / "report.xml"
    path.write_bytes(XML)
    case["report"] = report_state.PublishedReport(path, DAY, 1, hashlib.sha256(XML).hexdigest(), None)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, args=None):
            assert conn.session == {"readonly": True, "isolation_level": "REPEATABLE READ"}
            assert query.lstrip().upper().startswith(("SELECT", "SET LOCAL"))
            statements.append((query, args))
            self.result = ([{"present": case["history_present"]}] if "to_regclass" in query else
                           [{"as_of": d} for d in case["dates"]] if "SELECT DISTINCT" in query else [])

        def fetchone(self):
            return self.result[0]

        def fetchall(self):
            return self.result

    class Connection:
        closed = False

        def set_session(self, **kwargs):
            self.session = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self, **kwargs):
            return Cursor()

        def close(self):
            self.closed = True

    conn = Connection()
    monkeypatch.setattr(service, "select_report", lambda c, raw: (case["report"], {
        "id": 1, "status": "success", "summary_json": {"publication": {"state": "published"}}}))
    monkeypatch.setattr(report_state, "utc_now", lambda: datetime(2026, 10, 22, 22, tzinfo=timezone.utc))

    def forbidden():
        pytest.fail("Display FX must not open another connection")

    monkeypatch.setattr(performance, "_latest_usd_cny_rate", forbidden)
    case.update(conn=conn, statements=statements, factory=lambda: conn)
    return case


def read(case, **kwargs):
    return service.read_ibkr_account_audit(connection_factory=case["factory"], **kwargs)


def rewrite(case, content):
    case["report"].path.write_bytes(content)
    case["report"] = replace(case["report"], sha256=hashlib.sha256(content).hexdigest())


def test_subject_cash_positions_only_never_certifies_nav_ytd_fx_or_history(local_case):
    result = read(local_case, period_start=START)
    assert result["status"] == "review" and result["read_only"] and not result["executable"]
    assert result["scope"]["account_id"] == "U_TEST" and result["scope"]["base_currency"] == "USD"
    for key in ("cash", "positions"):
        assert check(result, key)["status"] == "ok" and check(result, key)["compared"] == 1
    assert "semantic_mismatch" in reasons(result, "nav")
    assert check(result, "nav")["evidence"]["net_liquidation_base"] == "83"
    assert "missing_internal_fx" in reasons(result, "fx")
    assert "missing_internal_period" in reasons(result, "period_return")
    assert not check(result, "period_return")["evidence"]["return_ratio_verified"]
    assert not check(result, "history_coverage")["evidence"]["all_dates_verified"]
    assert result["adapter"]["internal_nav_basis"] == "cash_plus_positions_not_official_nav_total"
    assert result["adapter"]["history_date_count"] == 0
    assert result["adapter"]["history_dates_source"] == "unavailable_without_official_daily_coverage"
    assert local_case["conn"].closed and json.loads(json.dumps(result)) == result
    assert all("pams_ibkr_position_history" not in query for query, _ in local_case["statements"])


@pytest.mark.parametrize("field,value,key,expected", [
    ("cash_base", -40, "cash", "-40"), ("position_value_base", 120, "positions", "120")])
def test_fake_subject_amount_is_not_replaced_by_oracle(local_case, monkeypatch, field, value, key, expected):
    parse = performance._parse_flex_performance
    def subject(path, *, include_display_fx):
        assert include_display_fx is False
        return {**parse(path, include_display_fx=False), field: value}
    monkeypatch.setattr(performance, "_parse_flex_performance", subject)
    result = read(local_case)
    assert result["status"] == "error" and "financial_delta" in reasons(result, key)
    assert check(result, key)["issues"][0]["internal"] == expected


def test_oracle_receives_original_bytes_and_only_subject_values(local_case, monkeypatch):
    calls = []
    def audit(**kwargs):
        calls.append(kwargs)
        return oracle.audit_ibkr_account(**kwargs)
    monkeypatch.setattr(service, "audit_ibkr_account", audit)
    read(local_case)
    kwargs = calls[0]
    assert kwargs["xml_text"].encode("utf-8") == XML
    assert kwargs["expected_sha256"] == hashlib.sha256(XML).hexdigest()
    state = kwargs["internal_state"]
    assert state["net_liquidation_base"] == 80 and state["nav_basis"] != "official_nav_total"
    assert state["cash_base"] == -20 and state["position_value_base"] == 100
    assert not {"fx_rates_to_base", "fx_basis", "period", "positions", "cash_by_currency"} & state.keys()
    tree = ast.parse(Path(oracle.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.ImportFrom) and "performance" in (n.module or "") for n in ast.walk(tree))


@pytest.mark.parametrize("stage", ["before", "after"])
def test_hash_race_never_emits_audit_amounts(local_case, monkeypatch, stage):
    original = performance._parse_flex_performance
    if stage == "before":
        local_case["report"].path.write_bytes(XML + b"\n")
        monkeypatch.setattr(performance, "_parse_flex_performance", lambda *a, **k: pytest.fail("parse before hash check"))
    else:
        def changed(path, **kwargs):
            result = original(path, **kwargs)
            path.write_bytes(XML + b"\n")
            return result
        monkeypatch.setattr(performance, "_parse_flex_performance", changed)
    result = read(local_case)
    assert result["status"] == "unavailable" and result["reason"] == "report_changed" and not result["checks"]


@pytest.mark.parametrize("field,value", [("source_file", "other.xml"), ("as_of", "2026-10-21"),
                                         ("as_of", None), ("status", "review")])
def test_subject_must_be_tied_to_selected_report(local_case, monkeypatch, field, value):
    parse = performance._parse_flex_performance
    monkeypatch.setattr(performance, "_parse_flex_performance",
                        lambda path, **kwargs: {**parse(path, **kwargs), field: value})
    result = read(local_case)
    assert result["status"] == "unavailable" and not result["checks"]


@pytest.mark.parametrize("mutation", ["missing_info", "missing_base", "wrong_info_account", "duplicate_info",
                                     "duplicate_statement", "date", "doctype"])
def test_missing_or_ambiguous_original_scope_is_not_defaulted(local_case, mutation):
    info = b'<AccountInformation accountId="U_TEST" currency="USD" />'
    content = XML
    if mutation == "missing_info":
        content = content.replace(info, b"")
    elif mutation == "missing_base":
        content = content.replace(info, b'<AccountInformation accountId="U_TEST" />')
    elif mutation == "wrong_info_account":
        content = content.replace(info, info.replace(b"U_TEST", b"OTHER"))
    elif mutation == "duplicate_info":
        content = content.replace(info, info + info)
    elif mutation == "duplicate_statement":
        content = content.replace(b"</FlexStatements>", b'<FlexStatement accountId="OTHER" /></FlexStatements>')
    elif mutation == "date":
        content = content.replace(b'toDate="20261022"', b'toDate="20261021"')
    else:
        content = b'<!DOCTYPE FlexQueryResponse [<!ENTITY private "secret">]>' + content
    rewrite(local_case, content)
    assert read(local_case)["status"] == "unavailable"


def test_official_non_usd_base_does_not_change_subject_usd_claim(local_case):
    rewrite(local_case, XML.replace(b'currency="USD"', b'currency="EUR"'))
    result = read(local_case)
    assert result["scope"]["base_currency"] == "EUR"
    assert "semantic_mismatch" in reasons(result, "source")
    assert check(result, "cash")["compared"] == 0


def test_model_ancestor_is_left_to_independent_scope_gate(local_case):
    rewrite(local_case, XML.replace(b"<FlexQueryResponse>", b'<FlexQueryResponse model="subaccount">'))
    result = read(local_case)
    assert "official_account_or_ownership_mismatch" in reasons(result, "source")
    assert all(c["compared"] == 0 for c in result["checks"])


def test_rows_outside_statement_cannot_inherit_subject_account_even_at_zero(local_case):
    content = XML.replace(b"</FlexQueryResponse>", b'<OpenPosition accountId="OTHER" '
        b'currency="USD" reportDate="20261022" positionValue="0" /></FlexQueryResponse>')
    rewrite(local_case, content)
    assert read(local_case)["status"] == "unavailable"


def test_daily_statement_history_does_not_replace_exact_closing_scope(local_case):
    earlier = (b'<FlexStatement accountId="U_TEST" fromDate="20261021" toDate="20261021">'
        b'<AccountInformation accountId="U_TEST" currency="USD" />'
        b'<CashReportCurrency accountId="U_TEST" currency="BASE_SUMMARY" toDate="20261021" endingCash="-1000" />'
        b'<OpenPosition accountId="U_TEST" currency="USD" reportDate="20261021" positionValue="9999" />'
        b'</FlexStatement>')
    rewrite(local_case, XML.replace(b"<FlexStatements>", b"<FlexStatements>" + earlier))
    result = read(local_case)
    assert result["status"] == "review"
    assert check(result, "cash")["status"] == check(result, "positions")["status"] == "ok"
    assert check(result, "cash")["evidence"]["cash_base"] == "-20"


@pytest.mark.parametrize("row", [
    b'<CashReportCurrency accountId="U_TEST" currency="BASE_SUMMARY" toDate="20261022" endingCash="0" />',
    b'<CashReportCurrency accountId="U_TEST" currency="USD" toDate="20261022" endingCash="0" />',
    b'<OpenPosition accountId="U_TEST" currency="USD" reportDate="20261022" positionValue="0" />',
])
def test_older_statement_cannot_inject_closing_rows_into_subject(local_case, row):
    earlier = (b'<FlexStatement accountId="U_TEST" fromDate="20261021" toDate="20261021">'
               + row + b'</FlexStatement>')
    rewrite(local_case, XML.replace(b"<FlexStatements>", b"<FlexStatements>" + earlier))
    result = read(local_case)
    assert result["status"] == "unavailable" and result["checks"] == []


def test_duplicate_closing_statement_is_not_selected_by_order(local_case):
    duplicate = b'<FlexStatement accountId="U_TEST" fromDate="20261022" toDate="20261022" />'
    rewrite(local_case, XML.replace(b"</FlexStatements>", duplicate + b"</FlexStatements>"))
    assert read(local_case)["status"] == "unavailable"


def test_missing_statement_fx_is_not_latest_display_fx(local_case):
    content = XML.replace(b'currency="USD" reportDate=', b'currency="HKD" reportDate=').replace(b' fxRateToBase="1"', b'')
    rewrite(local_case, content)
    result = read(local_case)
    assert "missing_historical_fx" in reasons(result, "fx")
    assert "missing_internal_fx" in reasons(result, "fx")
    assert check(result, "positions")["compared"] == 0


def test_stale_quality_does_not_certify_subject(local_case, monkeypatch):
    monkeypatch.setattr(report_state, "utc_now", lambda: datetime(2026, 10, 24, 22, tzinfo=timezone.utc))
    result = read(local_case)
    assert result["report_quality"]["status"] == "stale"
    assert "source_unverified" in reasons(result, "source")
    assert check(result, "cash")["compared"] == 0


def test_missing_report_and_missing_history_are_read_only(local_case, monkeypatch):
    local_case["history_present"] = False
    result = read(local_case)
    assert result["adapter"]["history_dates_source"] == "unavailable_without_official_daily_coverage"
    assert result["adapter"]["history_date_count"] == 0
    assert not any("SELECT DISTINCT" in q for q, _ in local_case["statements"])
    monkeypatch.setattr(service, "select_report", lambda *args: (None, None))
    assert read(local_case)["reason"] == "missing_report"


@pytest.mark.parametrize("stage", ["connect", "select", "subject", "oracle"])
def test_exception_messages_and_credentials_never_escape(local_case, monkeypatch, stage):
    def secret(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret-password@host/private-report.xml")
    if stage == "connect":
        local_case["factory"] = secret
    elif stage == "select":
        monkeypatch.setattr(service, "select_report", secret)
    elif stage == "subject":
        monkeypatch.setattr(performance, "_parse_flex_performance", secret)
    else:
        monkeypatch.setattr(service, "audit_ibkr_account", secret)
    result = read(local_case)
    assert result["status"] == "unavailable" and result["error_type"] == "RuntimeError"
    assert "secret" not in json.dumps(result) and "private-report" not in json.dumps(result)


def test_optional_display_fx_default_contract_and_base_formula_ast(tmp_path, monkeypatch):
    from tests.brokers.ibkr.test_ibkr_performance_service import XML as BASE_XML, EXPECTED
    path = tmp_path / "report.xml"
    path.write_text(BASE_XML, encoding="utf-8")
    calls = []
    monkeypatch.setattr(performance, "_latest_usd_cny_rate", lambda: calls.append(True) or Decimal("7"))
    default = performance._parse_flex_performance(path)
    explicit = performance._parse_flex_performance(path, include_display_fx=True)
    assert default == explicit and len(calls) == 2
    without = performance._parse_flex_performance(path, include_display_fx=False)
    assert without == EXPECTED and len(calls) == 2
    assert {k: v for k, v in default.items() if not k.endswith("_cny")} == without
    node = next(n for n in ast.parse(Path(performance.__file__).read_text(encoding="utf-8")).body
                if isinstance(n, ast.FunctionDef) and n.name == "_parse_flex_performance")
    node = deepcopy(node)
    assert [a.arg for a in node.args.kwonlyargs] == ["include_display_fx"]
    assert isinstance(node.args.kw_defaults[0], ast.Constant) and node.args.kw_defaults[0].value is True
    node.args.kwonlyargs, node.args.kw_defaults = [], []
    assert ast.unparse(node.args.args[0].annotation) == 'Path | report_state.StoredReport'
    node.args.args[0].annotation = ast.Name(id='Path', ctx=ast.Load())
    root_assignment = next(n for n in node.body if isinstance(n, ast.Assign)
                           and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'root')
    assert ast.unparse(root_assignment.value) == 'ET.fromstring(path.read_bytes())'
    root_assignment.value = ast.parse('ET.parse(path).getroot()', mode='eval').body
    assignment = next(n for n in node.body if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "usd_cny")
    assert isinstance(assignment.value, ast.IfExp) and isinstance(assignment.value.test, ast.Name)
    assert assignment.value.test.id == "include_display_fx"
    assert isinstance(assignment.value.orelse, ast.Constant) and assignment.value.orelse.value is None
    assignment.value = assignment.value.body
    assert hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() == (
        "fd85b84f13f853c2acd1b2f5e3f8607cc4948f96c18b1cf548dfef485416d405")


@pytest.fixture
def published_case(sync_case):
    client, conn, payload = sync_case
    payload["xml"] = XML.decode("utf-8")
    response = client.post("/api/ibkr/flex/sync", json={})
    assert response.status_code == 200 and response.json().get("success"), response.json()
    return client, conn


def stored_state(database):
    return {**business_state(database), "jobs": rows(database, "pams_data_job_run")}


def test_postgres_route_is_server_readonly_repeatable_and_preserves_old_routes(published_case, database, monkeypatch):
    statements = []
    class GuardCursor(psycopg2.extras.RealDictCursor):
        def execute(self, query, args=None):
            assert self.connection.readonly is True
            assert self.connection.isolation_level == ISOLATION_LEVEL_REPEATABLE_READ
            assert str(query).lstrip().upper().startswith(("SELECT", "SET LOCAL"))
            statements.append(str(query))
            return super().execute(query, args)
    class GuardConnection(connection):
        def cursor(self, *args, **kwargs):
            kwargs["cursor_factory"] = GuardCursor
            return super().cursor(*args, **kwargs)
    def guarded():
        return database(connection_factory=GuardConnection)
    client, _ = published_case
    monkeypatch.setattr(performance, "get_conn", database)
    before = stored_state(database)
    old = client.get("/api/ibkr/performance").json()
    monkeypatch.setattr(performance, "get_conn", guarded)
    for _ in range(2):
        response = client.get("/api/ibkr/account-audit?period_start=2026-10-01")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "review" and check(result, "cash")["status"] == "ok"
    assert client.get("/api/ibkr/account-audit?period_start=not-a-date").status_code == 422
    monkeypatch.setattr(performance, "get_conn", database)
    assert client.get("/api/ibkr/performance").json() == old
    assert statements and before == stored_state(database)


def test_postgres_server_rejects_writes_without_relying_on_guard(published_case, database, monkeypatch):
    before = stored_state(database)
    def write(conn, raw):
        with conn.cursor() as cur:
            cur.execute("UPDATE pams_holdings_snapshot SET market_value = 0")
    monkeypatch.setattr(service, "select_report", write)
    result = service.read_ibkr_account_audit(connection_factory=database)
    assert result["status"] == "unavailable" and result["error_type"] == "ReadOnlySqlTransaction"
    assert before == stored_state(database)


def test_postgres_selection_quality_and_history_share_snapshot(published_case, database, monkeypatch):
    selected = service.select_report
    calls = []
    def concurrent(conn, raw):
        report, latest = selected(conn, raw)
        calls.append(report.run_id)
        with conn.cursor() as cur:
            cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE symbol='TEST'")
            before = cur.fetchone()[0]
        other = database()
        try:
            with other, other.cursor() as cur:
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=market_value+1 WHERE symbol='TEST'")
        finally:
            other.close()
        with conn.cursor() as cur:
            cur.execute("SELECT market_value FROM pams_holdings_snapshot WHERE symbol='TEST'")
            assert cur.fetchone()[0] == before
        return report, latest
    monkeypatch.setattr(service, "select_report", concurrent)
    monkeypatch.setattr(performance, "_latest_usd_cny_rate", lambda: pytest.fail("external display FX"))
    result = service.read_ibkr_account_audit(connection_factory=database)
    assert len(calls) == 1 and result["report_quality"]["status"] == "ok"
    assert result["adapter"]["history_date_count"] == 0
    assert result["adapter"]["history_dates_source"] == "unavailable_without_official_daily_coverage"


def test_postgres_missing_history_is_not_created(published_case, database):
    result = service.read_ibkr_account_audit(connection_factory=database)
    assert result["adapter"]["history_dates_source"] == "unavailable_without_official_daily_coverage"
    conn = database()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_ibkr_position_history')")
            assert cur.fetchone()[0] is None
    finally:
        conn.close()
