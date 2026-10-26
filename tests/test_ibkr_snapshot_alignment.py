"""Synthetic snapshot protection tests; optional isolated PG and private replay."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INTRANS
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/api"))
from services import ibkr_snapshot_alignment as align  # noqa: E402


DAY = "2024-04-08"
RAW = b"synthetic authoritative daily report"
SOURCE_HASH = hashlib.sha256(RAW).hexdigest()
SCALES = {table: {field: (10 if table == align.HISTORY else 4 if field in ("cost", "market_value") else 6)
                  for field in align.NUMBERS} for table in align.TABLES}


def row(symbol="ALPHA", identity="conid:101", currency="USD", kind="position", **changes):
    return {"resolved_identity": identity, "kind": kind, "account_code": "test_broker",
            "source_account_id": "SYNTHETIC", "as_of": DAY, "symbol": symbol, "name": symbol + " - Test",
            "asset_liability": "asset", "currency": currency, "major_category": "securities",
            "minor_category": "stock", "market_value": Decimal("125"), "cost": Decimal("100"),
            "quantity": Decimal("5"), "buy_price": Decimal("20"), "current_price": Decimal("25"), **changes}


def evidence(desired, **changes):
    cash = [r["currency"] for r in desired if r["kind"] == "cash"]
    return {"source": "ibkr_flex", "source_sha256": SOURCE_HASH, "source_key": "synthetic-publication",
            "authority_reference": "verified unfiltered daily query", "account_code": "test_broker",
            "source_account_id": "SYNTHETIC", "as_of": DAY, "statement_count": 1,
            "authoritative": True, "complete_account": True,
            "positions_section": "complete" if any(r["kind"] == "position" for r in desired) else "empty",
            "cash_section": "complete" if cash else "empty", "cash_currencies": cash,
            "zero_cash_currencies": [], "numeric_scales": deepcopy(SCALES), **changes}


def stored(source, table, number):
    data = {key: source[key] for key in align.FIELDS}
    if table == align.HISTORY:
        data["source_account_id"] = source["source_account_id"]
    else:
        data["asset_id"] = "stable-existing-asset-" + str(number)
    return {**data, "id": number, "created_at": "2024-04-08T23:00:00+00:00",
            "updated_at": "2024-04-08T23:00:00+00:00"}


def proof(source, table, existing, provenance="source_managed"):
    return {"table": table, "id": existing["id"], "row_sha256": align.fingerprint([existing]),
            "resolved_identity": source["resolved_identity"], "kind": source["kind"],
            "provenance": provenance, "source_account_id": source["source_account_id"],
            "source_sha256": SOURCE_HASH, "reference": "verified per-row synthetic publication lineage"}


def case():
    sources = [row(), row("EXTRA", "conid:202"), row("MANUAL", "asset:manual")]
    before = {table: [stored(source, table, i + 20) for i, source in enumerate(sources)] for table in align.TABLES}
    proofs = [proof(source, table, existing, "manual" if source["symbol"] == "MANUAL" else "source_managed")
              for table in align.TABLES for source, existing in zip(sources, before[table])]
    return {"evidence": evidence([sources[0]]), "desired_rows": [sources[0]], "before_rows": before,
            "before_sha256": {table: align.fingerprint(rows) for table, rows in before.items()}, "ownership": proofs}


def auth(plan, **changes):
    now = datetime.now(timezone.utc)
    return {"purpose": "ibkr_complete_snapshot_sync" if plan["mode"] == "source_sync" else "ibkr_historical_snapshot_alignment",
            "plan_sha256": plan["plan_sha256"], "approved_by": "synthetic reviewer", "reference": "synthetic approval",
            "source_owned": True, "source_sha256": plan["evidence"]["source_sha256"],
            "source_key": plan["evidence"]["source_key"], "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(), **changes}


def rehash(inputs):
    inputs["before_sha256"] = {table: align.fingerprint(rows) for table, rows in inputs["before_rows"].items()}
    for item in inputs["ownership"]:
        existing = next(r for r in inputs["before_rows"][item["table"]] if r["id"] == item["id"])
        item["row_sha256"] = align.fingerprint([existing])


def test_complete_set_deletes_stale_extra_from_both_tables_preserving_manual():
    inputs = case()
    original = deepcopy(inputs)
    plan = align.plan_snapshot_alignment(**inputs)
    assert [(op["table"], op["action"], op["key"]) for op in plan["operations"]] == [
        (table, "delete", ["conid:202", "USD"]) for table in align.TABLES]
    assert all(len(rows) == 2 for rows in plan["projected"].values())
    assert inputs == original


@pytest.mark.parametrize("field,value", [("authoritative", False), ("complete_account", False),
    ("statement_count", 2), ("statement_count", True), ("positions_section", "missing"),
    ("cash_section", "partial"), ("source_sha256", ""), ("source_account_id", ""),
    ("authority_reference", ""), ("numeric_scales", {})])
def test_evidence_is_fail_closed(field, value):
    inputs = case()
    inputs["evidence"][field] = value
    with pytest.raises(align.AlignmentBlocked):
        align.plan_snapshot_alignment(**inputs)


@pytest.mark.parametrize("change", ["foreign_account", "foreign_date", "foreign_source", "duplicate", "duplicate_name",
    "symbol_identity", "missing_number", "nan", "huge", "partial_position", "partial_cash"])
def test_invalid_source_rows_block(change):
    inputs = case()
    target = inputs["desired_rows"][0]
    if change == "foreign_account": target["account_code"] = "elsewhere"
    if change == "foreign_date": target["as_of"] = "2024-04-09"
    if change == "foreign_source": target["source_account_id"] = "OTHER"
    if change == "duplicate": inputs["desired_rows"].append(deepcopy(target))
    if change == "duplicate_name": inputs["desired_rows"].append({**target, "resolved_identity": "asset:other", "currency": "EUR"})
    if change == "symbol_identity": target["resolved_identity"] = "symbol:ALPHA"
    if change == "missing_number": target["market_value"] = None
    if change == "nan": target["market_value"] = Decimal("NaN")
    if change == "huge": target["market_value"] = Decimal("1e30")
    if change == "partial_position": inputs["evidence"]["positions_section"] = "empty"
    if change == "partial_cash": inputs["evidence"]["cash_currencies"] = ["EUR"]
    with pytest.raises(align.AlignmentBlocked):
        align.plan_snapshot_alignment(**inputs)


@pytest.mark.parametrize("change", ["unknown", "missing", "stale", "foreign", "broad", "wrong_hash", "duplicate_id", "duplicate_identity"])
def test_before_and_ownership_guards(change):
    inputs = case()
    if change == "unknown": inputs["ownership"][1]["provenance"] = "unknown"
    if change == "missing": inputs["ownership"].pop()
    if change == "stale": inputs["ownership"][0]["row_sha256"] = "0" * 64
    if change == "foreign": inputs["before_rows"][align.HISTORY][0]["source_account_id"] = "FOREIGN"; rehash(inputs)
    if change == "broad": inputs["before_rows"][align.HOLDINGS][0]["as_of"] = "2024-04-09"; rehash(inputs)
    if change == "wrong_hash": inputs["before_sha256"][align.HISTORY] = "0" * 64
    if change == "duplicate_id": inputs["before_rows"][align.HISTORY].append(inputs["before_rows"][align.HISTORY][0]); rehash(inputs)
    if change == "duplicate_identity": inputs["ownership"][1]["resolved_identity"] = inputs["ownership"][0]["resolved_identity"]
    with pytest.raises(align.AlignmentBlocked):
        align.plan_snapshot_alignment(**inputs)


def test_unknown_extra_needs_evidence_not_account_or_name_ownership():
    inputs = case()
    extra = next(p for p in inputs["ownership"] if p["table"] == align.HOLDINGS and p["resolved_identity"] == "conid:202")
    extra["provenance"] = "unknown"
    with pytest.raises(align.AlignmentBlocked, match="Unknown provenance"):
        align.plan_snapshot_alignment(**inputs)


def test_manual_collision_and_exact_historical_candidate_gate():
    inputs = case()
    inputs["ownership"][0]["provenance"] = "manual"
    with pytest.raises(align.AlignmentBlocked, match="Manual provenance"):
        align.plan_snapshot_alignment(**inputs, mode="source_sync")
    assert align.plan_snapshot_alignment(**inputs)["policy"] == "historical_delete_only"
    item = inputs["ownership"][1]
    item["provenance"] = "manual"
    candidate = {key: item[key] for key in ("table", "id", "row_sha256", "source_account_id", "source_sha256", "reference")}
    plan = align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate])
    assert plan["candidate_proofs"] == [candidate]
    with pytest.raises(align.AlignmentBlocked, match="Historical candidates"):
        align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate], mode="source_sync")
    candidate["source_sha256"] = "a" * 64
    with pytest.raises(align.AlignmentBlocked, match="Unbound historical"):
        align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate])


def test_explicit_empty_removes_positions_but_missing_never_means_empty():
    inputs = case()
    inputs["desired_rows"] = []
    inputs["evidence"] = evidence([])
    plan = align.plan_snapshot_alignment(**inputs)
    assert len(plan["operations"]) == 4
    assert all(len(rows) == 1 for rows in plan["projected"].values())


def test_omitted_cash_requires_explicit_zero_and_preserves_negative_cash():
    inputs = case()
    cash = row("CASH_EUR", "cash:EUR", "EUR", "cash", market_value=Decimal("-11.5"))
    for table in align.TABLES:
        existing = stored(cash, table, 70)
        inputs["before_rows"][table].append(existing)
        inputs["ownership"].append(proof(cash, table, existing))
    rehash(inputs)
    with pytest.raises(align.AlignmentBlocked, match="not proven zero"):
        align.plan_snapshot_alignment(**inputs)
    inputs["evidence"].update(cash_currencies=["EUR"], zero_cash_currencies=["EUR"])
    assert len(align.plan_snapshot_alignment(**inputs)["operations"]) == 4
    inputs["desired_rows"].append(cash)
    inputs["evidence"] = evidence(inputs["desired_rows"])
    assert all(op["key"] != ["cash:EUR", "EUR"] for op in align.plan_snapshot_alignment(**inputs)["operations"])


def test_stable_identity_not_display_name_and_per_currency_sets():
    inputs = case()
    inputs["desired_rows"][0]["name"] = "Renamed description"
    inputs["desired_rows"].append(row("ALPHA_EUR", "conid:101", "EUR"))
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    assert [op["action"] for op in plan["operations"]].count("update") == 2
    assert [op["action"] for op in plan["operations"]].count("insert") == 2
    assert all(op["id"] == 20 for op in plan["operations"] if op["action"] == "update")


def test_storage_rounding_uses_bound_schema_and_is_order_independent():
    inputs = case()
    inputs["desired_rows"][0]["market_value"] = Decimal("125.00005")
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    values = {op["table"]: op["data"]["market_value"] for op in plan["operations"] if op["action"] == "update"}
    assert values == {align.HISTORY: Decimal("125.0000500000"), align.HOLDINGS: Decimal("125.0001")}
    for rows in inputs["before_rows"].values(): rows.reverse()
    inputs["ownership"].reverse()
    assert align.plan_snapshot_alignment(**inputs, mode="source_sync")["plan_sha256"] == plan["plan_sha256"]


def test_historical_delete_only_preserves_all_matched_values_and_metadata():
    inputs = case()
    inputs["desired_rows"][0].update(name="New description", market_value=Decimal("150.5555555555"),
                                    cost=Decimal("99.1234567890"), quantity=Decimal("7"),
                                    buy_price=Decimal("14.1604938270"), current_price=Decimal("21.5079365079"))
    inputs["desired_rows"].append(row("MISSING_SOURCE", "conid:404"))
    for table in align.TABLES:
        inputs["before_rows"][table][0]["buy_price"] = None
        inputs["before_rows"][table][0]["cost"] = Decimal("100.123400")
    rehash(inputs)
    for item in inputs["ownership"]:
        if item["resolved_identity"] == "conid:101": item["provenance"] = "unknown"
    plan = align.plan_snapshot_alignment(**inputs)
    assert plan["policy"] == "historical_delete_only"
    assert len(plan["operations"]) == 2 and all(op["action"] == "delete" for op in plan["operations"])
    assert plan["missing_source_rows"] == {table: [["conid:404", "USD"]] for table in align.TABLES}
    for table in align.TABLES:
        matched = next(item for item in plan["projected"][table] if item["row"]["id"] == 20)
        assert matched == {"row": inputs["before_rows"][table][0], "ignore": []}


def test_historical_cannot_accept_a_matched_candidate_or_obsolete_refresh_plan(memory):
    inputs, conn = memory
    matched = inputs["ownership"][0]
    candidate = {key: matched[key] for key in ("table", "id", "row_sha256", "source_account_id", "source_sha256", "reference")}
    with pytest.raises(align.AlignmentBlocked, match="not an unsupported extra"):
        align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate])
    old = align.plan_snapshot_alignment(**inputs)
    old["version"] = 1
    old["plan_sha256"] = align.fingerprint([{k: v for k, v in old.items() if k != "plan_sha256"}])
    with pytest.raises(align.AlignmentBlocked, match="obsolete"):
        align.apply_snapshot_alignment(conn, old, source_bytes=RAW, dry_run=False, authorization=auth(old))


def test_source_refresh_operations_cannot_be_rebranded_as_historical(memory):
    inputs, conn = memory
    inputs["desired_rows"][0]["market_value"] += 1
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    plan.update(mode="historical", policy="historical_delete_only")
    plan["plan_sha256"] = align.fingerprint([{k: v for k, v in plan.items() if k != "plan_sha256"}])
    with pytest.raises(align.AlignmentBlocked, match="deletion-only"):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    assert conn.rows == inputs["before_rows"] and conn.commits == 0


def xml_report():
    return b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="SYNTHETIC" fromDate="20240408" toDate="20240408">
      <AccountInformation accountId="SYNTHETIC" currency="USD"/>
      <OpenPositions><OpenPosition accountId="SYNTHETIC" reportDate="20240408" conid="101" symbol="ALPHA"
        description="Test" currency="USD" position="5" positionValue="125.00005" costBasisMoney="100"
        costBasisPrice="20" markPrice="25.00001" levelOfDetail="SUMMARY"/></OpenPositions>
      <CashReport><CashReportCurrency accountId="SYNTHETIC" fromDate="20240408" toDate="20240408"
        currency="BASE_SUMMARY" endingCash="-10"/>
        <CashReportCurrency accountId="SYNTHETIC" fromDate="20240408" toDate="20240408"
        currency="EUR" endingCash="-10" levelOfDetail="Currency"/>
        <CashReportCurrency accountId="SYNTHETIC" fromDate="20240408" toDate="20240408"
        currency="JPY" endingCash="0" levelOfDetail="Currency"/></CashReport>
    </FlexStatement></FlexStatements></FlexQueryResponse>'''


def extract(raw=None, **changes):
    return align.source_snapshot_from_xml(source_bytes=raw or xml_report(), account_code="test_broker",
        source_account_id="SYNTHETIC", as_of=DAY, source_key="test-publication", authority_reference="query checked",
        numeric_scales=SCALES, **{"authoritative": True, "complete_account": True, **changes})


def test_xml_adapter_uses_native_cash_decimal_and_explicit_zero():
    source, desired = extract()
    assert source["zero_cash_currencies"] == ["JPY"]
    assert source["source_sha256"] == hashlib.sha256(xml_report()).hexdigest()
    assert len(desired) == 2
    assert desired[0]["market_value"] == Decimal("125.00005")
    assert desired[1]["market_value"] == -10
    assert desired[0]["resolved_identity"] == "conid:101"


@pytest.mark.parametrize("change", ["missing_positions", "missing_cash", "duplicate_statement", "duplicate_position",
    "orphan", "foreign_account", "foreign_date", "partial_model", "lots", "no_conid", "no_value", "period", "doctype"])
def test_xml_adapter_rejects_incomplete_ambiguous_source(change):
    root = ET.fromstring(xml_report())
    stmt = next(root.iter("FlexStatement"))
    position = next(root.iter("OpenPosition"))
    if change == "missing_positions": stmt.remove(stmt.find("OpenPositions"))
    if change == "missing_cash": stmt.remove(stmt.find("CashReport"))
    if change == "duplicate_statement": root.find("FlexStatements").append(deepcopy(stmt))
    if change == "duplicate_position": stmt.find("OpenPositions").append(deepcopy(position))
    if change == "orphan": root.append(deepcopy(position))
    if change == "foreign_account": position.set("accountId", "FOREIGN")
    if change == "foreign_date": position.set("reportDate", "20240409")
    if change == "partial_model": stmt.find("OpenPositions").set("model", "only-some")
    if change == "lots": position.set("levelOfDetail", "LOT")
    if change == "no_conid": position.attrib.pop("conid")
    if change == "no_value": position.attrib.pop("positionValue")
    if change == "period": stmt.set("fromDate", "20240401")
    raw = ET.tostring(root)
    if change == "doctype": raw = b'<!DOCTYPE x [<!ENTITY x "unsafe">]>' + raw
    with pytest.raises(align.AlignmentBlocked):
        extract(raw)


def test_xml_adapter_authority_not_inferred_from_xml_and_explicit_empty():
    with pytest.raises(align.AlignmentBlocked):
        extract(complete_account=False)
    root = ET.fromstring(xml_report())
    root.find(".//OpenPositions").clear()
    root.find(".//CashReport").clear()
    source, rows = extract(ET.tostring(root))
    assert rows == [] and source["positions_section"] == source["cash_section"] == "empty"


@pytest.mark.parametrize("change", ["outer_account", "wrapper_date", "section_date", "cash_duplicate", "cash_kind", "tiny_nonzero"])
def test_no_scope_or_cash_omission_shortcuts(change):
    root = ET.fromstring(xml_report())
    if change == "outer_account": root.set("accountId", "OTHER")
    if change == "section_date": root.find(".//OpenPositions").set("reportDate", "20240409")
    if change == "wrapper_date":
        container = root.find(".//OpenPositions")
        position = container.find("OpenPosition")
        container.remove(position)
        wrapper = ET.SubElement(container, "Wrapper", reportDate="20240409")
        wrapper.append(position)
    if change == "cash_duplicate": root.find(".//CashReport").append(deepcopy(root.findall(".//CashReportCurrency")[1]))
    if change in ("outer_account", "section_date", "wrapper_date", "cash_duplicate"):
        with pytest.raises(align.AlignmentBlocked): extract(ET.tostring(root))
        return
    inputs = case()
    if change == "cash_kind":
        inputs["desired_rows"][0]["symbol"] = "CASH_USD"
    else:
        leaf = root.findall(".//CashReportCurrency")[1]
        leaf.set("endingCash", "0.000000000001")
        source, desired = extract(ET.tostring(root))
        assert "EUR" not in source["zero_cash_currencies"]
        inputs.update(evidence=source, desired_rows=desired)
    with pytest.raises(align.AlignmentBlocked): align.plan_snapshot_alignment(**inputs)


def test_unknown_candidate_requires_row_bound_historical_proof():
    inputs = case()
    extra = next(p for p in inputs["ownership"] if p["table"] == align.HOLDINGS and p["resolved_identity"] == "conid:202")
    extra["provenance"] = "unknown"
    candidate = {key: extra[key] for key in ("table", "id", "row_sha256", "source_account_id", "source_sha256", "reference")}
    plan = align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate])
    assert any(op.get("id") == extra["id"] and op["table"] == extra["table"] for op in plan["operations"])
    with pytest.raises(align.AlignmentBlocked):
        align.plan_snapshot_alignment(**inputs, candidate_proofs=[candidate], mode="source_sync")


class MemoryConnection:
    """Only transaction protocol tests; real SQL behavior is tested separately."""
    autocommit = False

    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.backup = None
        self.status = TRANSACTION_STATUS_IDLE
        self.commands = []
        self.commits = 0
        self.fail_commit = False

    def get_transaction_status(self): return self.status
    def cursor(self, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def fetchone(self): return ("0",)

    def execute(self, command, params=None):
        text = command if isinstance(command, str) else str(command)
        self.commands.append(text)
        if text.startswith("BEGIN") or "SAVEPOINT" in text and "RELEASE" not in text and "ROLLBACK" not in text:
            self.backup = deepcopy(self.rows)
            self.status = TRANSACTION_STATUS_INTRANS
        if "ROLLBACK TO" in text:
            self.rows = deepcopy(self.backup)

    def rollback(self):
        if self.backup is not None: self.rows = deepcopy(self.backup)
        self.status = TRANSACTION_STATUS_IDLE

    def commit(self):
        if self.fail_commit: raise RuntimeError("injected commit failure")
        self.commits += 1
        self.status = TRANSACTION_STATUS_IDLE
        self.backup = None


@pytest.fixture
def memory(monkeypatch):
    inputs = case()
    conn = MemoryConnection(inputs["before_rows"])
    monkeypatch.setattr(align, "read_snapshot_rows", lambda c, **kw: deepcopy(c.rows))
    monkeypatch.setattr(align, "read_snapshot_numeric_scales", lambda c: deepcopy(SCALES))
    def write(cur, op, scope):
        rows = cur.rows[op["table"]]
        if op["action"] == "delete": rows[:] = [r for r in rows if r["id"] != op["id"]]
        elif op["action"] == "update": next(r for r in rows if r["id"] == op["id"]).update(op["data"])
        else: rows.append({**op["data"], "id": 100 + len(rows)})
    monkeypatch.setattr(align, "_write", write)
    return inputs, conn


def test_apply_defaults_dry_run_and_does_not_commit(memory):
    inputs, conn = memory
    plan = align.plan_snapshot_alignment(**inputs)
    result = align.apply_snapshot_alignment(conn, plan, source_bytes=RAW)
    assert result["status"] == "dry_run" and result["planned_changes"] == 2
    assert conn.rows == inputs["before_rows"] and conn.commits == 0
    assert conn.commands[0].endswith("READ ONLY")


def test_source_owned_future_apply_and_idempotent_retry(memory):
    inputs, conn = memory
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    result = align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    assert result["changed"] == 2 and conn.commits == 1
    assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))["status"] == "already_aligned"


@pytest.mark.parametrize("change", ["missing", "wrong_digest", "wrong_purpose", "expired", "unbounded", "no_approver", "source_changed", "plan_changed", "before_changed", "partial_apply", "schema_changed", "autocommit"])
def test_apply_guards_before_any_mutation(memory, monkeypatch, change):
    inputs, conn = memory
    plan = align.plan_snapshot_alignment(**inputs)
    authorization, source = auth(plan), RAW
    if change == "missing": authorization = None
    if change == "wrong_digest": authorization["plan_sha256"] = "a" * 64
    if change == "wrong_purpose": authorization["purpose"] = "ibkr_complete_snapshot_sync"
    if change == "expired": authorization["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    if change == "unbounded": authorization["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    if change == "no_approver": authorization["approved_by"] = ""
    if change == "source_changed": source += b"different"
    if change == "plan_changed": plan["operations"].clear()
    if change == "before_changed": conn.rows[align.HOLDINGS][0]["market_value"] += 1
    if change == "partial_apply": conn.rows[align.HOLDINGS].pop(1)
    if change == "schema_changed": monkeypatch.setattr(align, "read_snapshot_numeric_scales", lambda c: {})
    if change == "autocommit": conn.autocommit = True
    original = deepcopy(conn.rows)
    with pytest.raises(align.AlignmentBlocked):
        align.apply_snapshot_alignment(conn, plan, source_bytes=source, dry_run=False, authorization=authorization)
    assert conn.rows == original and conn.commits == 0


@pytest.mark.parametrize("failure", ["second_table", "verification", "commit"])
def test_apply_rolls_back_both_tables_on_failure(memory, monkeypatch, failure):
    inputs, conn = memory
    plan = align.plan_snapshot_alignment(**inputs)
    original_write = align._write
    def write(cur, op, scope):
        original_write(cur, op, scope)
        if failure == "second_table" and op["table"] == align.HOLDINGS: raise RuntimeError("injected second-table failure")
        if failure == "verification": cur.rows[op["table"]][0]["market_value"] += 1
    monkeypatch.setattr(align, "_write", write)
    conn.fail_commit = failure == "commit"
    with pytest.raises((RuntimeError, align.AlignmentBlocked)):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    assert conn.rows == inputs["before_rows"] and conn.commits == 0


@pytest.fixture(scope="module")
def isolated_database():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PAMS_TEST_POSTGRES_DSN must explicitly identify an isolated test server")
    return lambda: psycopg2.connect(dsn, connect_timeout=5)


@pytest.fixture
def pg_case(isolated_database, monkeypatch):
    conn = isolated_database()
    schema = "pams_ibkr_alignment_test_" + uuid4().hex
    monkeypatch.setattr(align, "SCHEMA", schema)
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        for table in align.TABLES:
            fields = ",".join(field + " numeric(28," + str(SCALES[table][field]) + ")" for field in sorted(align.NUMBERS))
            extra = ",source_account_id text" if table == align.HISTORY else ",asset_id text"
            cur.execute(sql.SQL("CREATE TABLE {} (id bigserial PRIMARY KEY,account_code text,as_of date,symbol text,name text,"
                "asset_liability text,currency text,major_category text,minor_category text,created_at timestamptz DEFAULT now(),"
                "updated_at timestamptz DEFAULT now()," + fields + extra + ",UNIQUE(account_code,name,as_of))").format(sql.Identifier(schema, table)))
            for source in (row(), row("EXTRA", "conid:202"), row("MANUAL", "asset:manual")):
                data = {key: source[key] for key in align.FIELDS}
                if table == align.HISTORY: data["source_account_id"] = "SYNTHETIC"
                cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(schema, table),
                    sql.SQL(",").join(map(sql.Identifier, data)), sql.SQL(",").join(sql.Placeholder() for _ in data)), tuple(data.values()))
            for other in (row("OTHER_ACCOUNT", "asset:other", account_code="other"), row("OTHER_DAY", "asset:other", as_of="2024-04-09")):
                cur.execute(sql.SQL("INSERT INTO {} (account_code,as_of,name,symbol,currency) VALUES (%s,%s,%s,%s,%s)")
                            .format(sql.Identifier(schema, table)), (other["account_code"], other["as_of"], other["name"], other["symbol"], other["currency"]))
    with conn:
        before = align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY)
        scales = align.read_snapshot_numeric_scales(conn)
    identity = {"ALPHA": "conid:101", "EXTRA": "conid:202", "MANUAL": "asset:manual"}
    proofs = [proof(row(r["symbol"], identity[r["symbol"]]), table, r,
                    "manual" if r["symbol"] == "MANUAL" else "source_managed") for table in align.TABLES for r in before[table]]
    desired = [row(market_value=Decimal("126.1234567890")), row("NEW", "conid:303")]
    inputs = {"evidence": evidence(desired, numeric_scales=scales), "desired_rows": desired,
              "before_rows": before, "before_sha256": {t: align.fingerprint(before[t]) for t in align.TABLES}, "ownership": proofs}
    try:
        yield conn, inputs
    finally:
        conn.rollback()
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


def test_postgres_positive_apply_idempotence_and_unrelated_preservation(pg_case):
    conn, inputs = pg_case
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    result = align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    assert result["changed"] == 6
    assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))["status"] == "already_aligned"
    with conn:
        after = align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY)
        for table in align.TABLES:
            assert {r["symbol"] for r in after[table]} == {"ALPHA", "NEW", "MANUAL"}
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE account_code='other' OR as_of='2024-04-09'").format(sql.Identifier(align.SCHEMA, table)))
                assert cur.fetchone()[0] == 2
        assert next(r for r in after[align.HOLDINGS] if r["symbol"] == "ALPHA")["market_value"] == Decimal("126.1235")


def test_postgres_default_dry_run_server_read_only(pg_case, monkeypatch):
    conn, inputs = pg_case
    original = align.read_snapshot_rows
    def read(c, **kwargs):
        with c.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
        return original(c, **kwargs)
    monkeypatch.setattr(align, "read_snapshot_rows", read)
    assert align.apply_snapshot_alignment(conn, align.plan_snapshot_alignment(**inputs), source_bytes=RAW)["changed"] == 0
    with conn:
        assert {t: align.fingerprint(r) for t, r in original(conn, account_code="test_broker", as_of=DAY).items()} == inputs["before_sha256"]


def test_postgres_late_sql_failure_rolls_back_both_tables(pg_case):
    conn, inputs = pg_case
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic late table failure'; END $$;
            CREATE TRIGGER fail_insert BEFORE INSERT ON {}
            FOR EACH ROW EXECUTE FUNCTION {}();""").format(sql.Identifier(align.SCHEMA, "reject_snapshot_insert"),
                sql.Identifier(align.SCHEMA, align.HOLDINGS), sql.Identifier(align.SCHEMA, "reject_snapshot_insert")))
    plan = align.plan_snapshot_alignment(**inputs, mode="source_sync")
    with pytest.raises(psycopg2.Error, match="synthetic late table failure"):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    with conn:
        assert {t: align.fingerprint(r) for t, r in align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY).items()} == inputs["before_sha256"]


def test_postgres_nested_savepoint_preserves_outer_work_and_timeouts(pg_case, monkeypatch):
    conn, inputs = pg_case
    with conn.cursor() as cur:
        cur.execute(sql.SQL("INSERT INTO {}(account_code,as_of,name) VALUES ('outer','2024-04-01','outer')")
                    .format(sql.Identifier(align.SCHEMA, align.HOLDINGS)))
        cur.execute("SET LOCAL lock_timeout='1234ms'")
    plan = align.plan_snapshot_alignment(**inputs)
    original = align._write
    def fail(cur, op, scope):
        original(cur, op, scope)
        if op["table"] == align.HOLDINGS: raise RuntimeError("nested failure")
    monkeypatch.setattr(align, "_write", fail)
    with pytest.raises(RuntimeError, match="nested failure"):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan), within_transaction=True)
    assert {t: align.fingerprint(r) for t, r in align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY).items()} == inputs["before_sha256"]
    monkeypatch.setattr(align, "_write", original)
    assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan), within_transaction=True)["commit_owned_by_caller"]
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE account_code='outer'").format(sql.Identifier(align.SCHEMA, align.HOLDINGS)))
        assert cur.fetchone()[0] == 1
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] == "1234ms"
    conn.rollback()


def test_postgres_concrete_xml_source_to_future_sync_apply(pg_case):
    conn, inputs = pg_case
    source, desired = extract()
    plan = align.plan_snapshot_alignment(**{**inputs, "evidence": source, "desired_rows": desired}, mode="source_sync")
    result = align.apply_snapshot_alignment(conn, plan, source_bytes=xml_report(), dry_run=False, authorization=auth(plan))
    assert result["status"] == "applied"
    with conn:
        current = align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY)
    assert all({r["symbol"] for r in rows} == {"ALPHA", "CASH_EUR", "MANUAL"} for rows in current.values())


def test_postgres_historical_delete_only_leaves_matched_and_missing_rows_untouched(pg_case):
    conn, inputs = pg_case
    plan = align.plan_snapshot_alignment(**inputs)
    assert all(op["action"] == "delete" for op in plan["operations"])
    assert len(plan["operations"]) == 2
    assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))["changed"] == 2
    with conn:
        current = align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY)
    for table in align.TABLES:
        assert {r["symbol"] for r in current[table]} == {"ALPHA", "MANUAL"}
        for actual in current[table]:
            original = next(r for r in inputs["before_rows"][table] if r["id"] == actual["id"])
            assert align.fingerprint([actual]) == align.fingerprint([original])
    assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))["status"] == "already_aligned"


def test_postgres_stale_before_hash_and_phantom_block(pg_case):
    conn, inputs = pg_case
    plan = align.plan_snapshot_alignment(**inputs)
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("INSERT INTO {} (account_code,as_of,name) VALUES ('test_broker',%s,'late phantom')")
                    .format(sql.Identifier(align.SCHEMA, align.HOLDINGS)), (DAY,))
    with pytest.raises(align.AlignmentBlocked, match="drift"):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))
    with conn:
        assert len(align.read_snapshot_rows(conn, account_code="test_broker", as_of=DAY)[align.HOLDINGS]) == 4


def test_postgres_nested_repeatable_read_is_rejected(pg_case):
    conn, inputs = pg_case
    with conn.cursor() as cur:
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    plan = align.plan_snapshot_alignment(**inputs)
    with pytest.raises(align.AlignmentBlocked, match="READ COMMITTED"):
        align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan), within_transaction=True)
    conn.rollback()


def test_postgres_table_lock_excludes_concurrent_legacy_writes(pg_case, isolated_database, monkeypatch):
    conn, inputs = pg_case
    contender = isolated_database()
    original_write = align._write
    observed = []
    def write(cur, op, scope):
        if not observed:
            with contender.cursor() as competing:
                competing.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    competing.execute(sql.SQL("INSERT INTO {} (account_code,as_of,name) VALUES ('test_broker',%s,'concurrent')")
                                      .format(sql.Identifier(align.SCHEMA, align.HOLDINGS)), (DAY,))
            contender.rollback()
            observed.append(True)
        original_write(cur, op, scope)
    monkeypatch.setattr(align, "_write", write)
    plan = align.plan_snapshot_alignment(**inputs)
    try:
        assert align.apply_snapshot_alignment(conn, plan, source_bytes=RAW, dry_run=False, authorization=auth(plan))["status"] == "applied"
        assert observed == [True]
    finally:
        contender.close()


def test_private_official_frozen_extra_preview_without_embedded_identity():
    directory = os.environ.get("PAMS_IBKR_ALIGNMENT_PRIVATE_DIR")
    if not directory:
        pytest.skip("Optional real evidence replay requires PAMS_IBKR_ALIGNMENT_PRIVATE_DIR")
    comparison = json.loads((Path(directory) / "PAMS_READONLY_COMPARISON.json").read_text(encoding="utf-8"))
    inputs = comparison["inputs"]
    raw = Path(inputs["audit_xml"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == inputs["audit_xml"]["sha256"]
    frozen = {}
    for item in inputs["frozen_files"]:
        if Path(item["path"]).stem in ("frozen-inputs", "frozen-support"):
            data = Path(item["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            frozen[Path(item["path"]).stem] = json.loads(data, parse_float=Decimal)
    root = ET.fromstring(raw)
    conids = {}
    for element in root.iter("OpenPosition"):
        key = element.get("symbol"), element.get("currency")
        conids.setdefault(key, set()).add("conid:" + element.get("conid"))
    extras = comparison["history"]["extra_internal_positions"]
    assert extras, "Private comparison must include an independently identified extra candidate"
    summaries = []
    for extra in extras:
        day, account = extra["as_of"], extra["source_account_id"]
        before = {align.HISTORY: [r for r in frozen["frozen-support"]["history"] if r["as_of"] == day],
                  align.HOLDINGS: [r for r in frozen["frozen-inputs"]["snapshots"] if r["as_of"] == day]}
        assert all(before.values())
        codes = {r["account_code"] for rows in before.values() for r in rows}
        assert len(codes) == 1
        source, desired = align.source_snapshot_from_xml(source_bytes=raw, account_code=next(iter(codes)),
            source_account_id=account, as_of=day, source_key="private-official-candidate-preview",
            authority_reference="private fixed acceptance completeness review; not execution permission",
            numeric_scales=SCALES, complete_account=True, authoritative=True)
        proofs = []
        for table in align.TABLES:
            for existing in before[table]:
                symbol, currency = existing["symbol"], existing["currency"]
                identity = "cash:" + currency if symbol.startswith("CASH_") else next(iter(conids[symbol, currency]))
                if not symbol.startswith("CASH_"): assert len(conids[symbol, currency]) == 1
                item = proof({"resolved_identity": identity, "kind": "cash" if symbol.startswith("CASH_") else "position",
                              "source_account_id": account}, table, existing)
                item["source_sha256"] = source["source_sha256"]
                item["reference"] = "offline hypothetical lineage proof; not approved for live apply"
                proofs.append(item)
        args = {"evidence": source, "desired_rows": desired, "before_rows": before,
                "before_sha256": {t: align.fingerprint(r) for t, r in before.items()}, "ownership": proofs}
        refresh_plan = align.plan_snapshot_alignment(**args, mode="source_sync")
        plan = align.plan_snapshot_alignment(**args)
        assert plan["policy"] == "historical_delete_only"
        assert all(op["action"] == "delete" for op in plan["operations"])
        financial_differences = []
        for op in refresh_plan["operations"]:
            if op["action"] != "update": continue
            previous = next(r for r in before[op["table"]] if r["id"] == op["id"])
            original_source = next(r for r in desired if [r["resolved_identity"], r["currency"]] == op["key"])
            fields = []
            for field, value in op["data"].items():
                if align.fingerprint([{field: value}]) == align.fingerprint([{field: previous.get(field)}]): continue
                numeric = field in align.NUMBERS and previous.get(field) is not None
                raw_value = original_source[field]
                scale = SCALES[op["table"]].get(field)
                difference = value - previous[field] if numeric else None
                legacy_float_value = align._number(float(raw_value), nullable=False, scale=scale) if numeric else None
                fields.append({"field": field, "before_present": field in previous, "before_null": previous.get(field) is None,
                               "before": previous.get(field), "official_raw": raw_value, "source_sync_target": value,
                               "target_minus_before": difference, "storage_scale": scale,
                               "within_one_storage_unit": abs(difference) <= Decimal(1).scaleb(-scale) if numeric else None,
                               "equals_simulated_float_storage_target": previous.get(field) == legacy_float_value if numeric else None})
            financial_differences.append({"table": op["table"], "id": op["id"], "symbol": previous["symbol"],
                                          "currency": previous["currency"], "kind": original_source["kind"], "fields": fields})
        expected_identity = next(iter(conids[extra["symbol"], extra["currency"]]))
        deletes = [op for op in plan["operations"] if op["action"] == "delete" and op["key"] == [expected_identity, extra["currency"]]]
        assert {op["table"] for op in deletes} == set(align.TABLES)
        summary = {"preview_only": True, "execution_authorized": False, "ownership_proofs_verified": False,
                          "hypothetical_ownership_only": True, "date": day, "source_sha256": source["source_sha256"],
                          "extra_candidates": [{"table": op["table"], "id": op["id"], "key": op["key"],
                              "row_sha256": op["before_sha256"]} for op in deletes],
                          "historical_delete_only_plan": plan, "historical_operation_count": len(plan["operations"]),
                          "source_refresh_comparison_only_operation_count": len(refresh_plan["operations"]),
                          "not_approved_source_refresh_financial_differences": financial_differences,
                          "live_apply_blocker": "verified per-row lineage or exact historical candidate proof/approval required"}
        changed_fields = [field for difference in financial_differences for field in difference["fields"]]
        native_mv_deltas: dict[str, Decimal] = {}
        for difference in financial_differences:
            for field in difference["fields"]:
                if field["field"] == "market_value":
                    currency = difference["currency"]
                    native_mv_deltas[currency] = native_mv_deltas.get(currency, Decimal(0)) + field["target_minus_before"]
        summary["difference_classification"] = {
            "snapshot_rows_with_financial_differences": len(financial_differences),
            "field_differences": len(changed_fields),
            "missing_before_fields": sum(not field["before_present"] for field in changed_fields),
            "null_before_fields": sum(field["before_null"] for field in changed_fields),
            "within_one_storage_unit": sum(field["within_one_storage_unit"] is True for field in changed_fields),
            "matches_simulated_float_storage": sum(field["equals_simulated_float_storage_target"] is True for field in changed_fields),
            "cash_rows": sum(difference["kind"] == "cash" for difference in financial_differences),
            "native_market_value_deltas_source_refresh_only": native_mv_deltas,
            "cash_cost_quantity_are_balance_representations_not_extra_deposits": True,
            "underlying_origin_of_snapshot_date_value_mismatch": "not established by this comparison",
            "historical_matched_field_rewrites": 0}
        summaries.append(summary)
        print(json.dumps({k: v for k, v in summary.items() if k not in (
            "historical_delete_only_plan", "not_approved_source_refresh_financial_differences")}, default=str))
        # Presence in a frozen account/name set is NOT sufficient destructive proof.
        for item in proofs:
            if item["resolved_identity"] == expected_identity: item["provenance"] = "unknown"
        with pytest.raises(align.AlignmentBlocked, match="Unknown provenance"):
            align.plan_snapshot_alignment(**args)
    output = os.environ.get("PAMS_IBKR_ALIGNMENT_PRIVATE_SUMMARY")
    if output:
        destination = Path(output).resolve()
        assert destination.is_relative_to(Path(directory).resolve().parent)
        assert destination.name.startswith("snapshot-alignment-preview") and destination.suffix == ".json"
        artifact = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "service_sha256": hashlib.sha256(Path(align.__file__).read_bytes()).hexdigest(),
                    "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "comparison_observed_at_utc": comparison["observed_at_utc"],
                    "numeric_scales_basis": "frozen record precision and project schema contract; no live schema inspection",
                    "scope": "offline private preview; actual apply needs fresh before-state, schema, proof and approval",
                    "snapshots": summaries}
        destination.write_text(json.dumps(artifact, indent=2, ensure_ascii=True, default=str) + "\n", encoding="utf-8")
