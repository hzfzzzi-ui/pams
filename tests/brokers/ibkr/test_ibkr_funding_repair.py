"""Synthetic funding evidence only. PG writes require the explicit disposable test DSN."""
from copy import deepcopy
from datetime import timedelta
from decimal import (Clamped, Context, Decimal, Inexact, Overflow, ROUND_CEILING, ROUND_FLOOR,
                     Rounded, Subnormal, Underflow, localcontext)
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import connection
from psycopg2.extras import Json, RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend/api"))
from domain.brokers.ibkr import ibkr_funding_repair as tool  # noqa: E402
from domain.ledger.cashflow_fact import insert_cashflow_facts, make_source_hash  # noqa: E402
from domain.investments.investment_trade_fact import insert_investment_trade_facts  # noqa: E402

XML = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_SYNTHETIC" fromDate="20240201" toDate="20240331">
<CashTransaction accountId="U_SYNTHETIC" transactionID="TX-ALPHA" type="Deposits/Withdrawals"
 dateTime="20240203;122438" settleDate="20240203" reportDate="20240205" currency="MXN" amount="8123.45" />
<CashTransaction accountId="U_SYNTHETIC" transactionID="TX-BETA" type="Deposits/Withdrawals"
 dateTime="20240306;122438" settleDate="20240306" reportDate="20240307" currency="MXN" amount="907.12" />
<CashTransaction accountId="U_SYNTHETIC" transactionID="INTEREST" type="Broker Interest Paid"
 dateTime="20240203;122438" settleDate="20240203" reportDate="20240205" currency="MXN" amount="8123.45" />
</FlexStatement></FlexStatements></FlexQueryResponse>'''


@pytest.fixture
def evidence(tmp_path):
    receipt, report = tmp_path / "receipt.bin", tmp_path / "report.xml"
    receipt.write_bytes(b"synthetic masked bank receipt; manually attested fields")
    report.write_bytes(XML)
    bank = {"source_table": "bank_receipt", "source_id": "receipt-alpha", "source_key": "native-bank-alpha",
            "source_hash": make_source_hash("bank_receipt", {"source_key": "native-bank-alpha"}),
            "evidence_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "account_code": "synthetic_bank", "masked_account": "12**9876", "event_date": "2024-02-03",
            "posting_date": "2024-02-04", "amount": "-8123.45", "currency": "MXN",
            "counterparty": "Example Broker LLC", "fx_rate_to_cny": "0.3456789012",
            "bank_reference": "not-a-broker-id"}
    mapping = {"bank_account_code": "synthetic_bank", "masked_account": "12**9876",
               "broker_account_code": "synthetic_broker", "broker_account_id": "U_SYNTHETIC",
               "counterparty": "Example Broker LLC"}
    brokers = tool.read_broker_cash_evidence(report, hashlib.sha256(XML).hexdigest(),
                     account_id=mapping["broker_account_id"], account_code=mapping["broker_account_code"],
                     fx_rate_to_cny="0.3456789012")
    return bank, brokers, mapping, receipt, report


def plan(evidence, **kwargs):
    return tool.plan_funding_repair(*evidence[:3], **kwargs)["request"]


def approve(request):
    now = tool.utc_now()
    return {"purpose": tool.PURPOSE, "request_sha256": request["request_sha256"],
            "approved_by": "synthetic approver", "reference": "disposable test only, NOT production permission",
            "issued_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(), "accepted_risks": request["risks"]}


def existing(evidence):
    request = plan(evidence)
    bank, trade = deepcopy(request["bank_fact"]), deepcopy(request["trade_fact"])
    bank.update(cashflow_id=31, source_table="native_bank_import", source_id="raw-bank-id",
                source_uid="raw-bank-key", source_hash="b" * 64, cashflow_type="internal_transfer",
                transfer_group_id="preserve-user-group", description="keep business note",
                raw_json={**bank["raw_json"], "user_note": "preserve bank note"})
    trade.update(trade_id=72, cashflow_id=None, source_table="legacy_broker_import", source_id="raw-broker-id",
                 source_key="raw-broker-key", source_hash="c" * 64,
                 raw_json={**trade["raw_json"], "user_note": "preserve broker note"})
    return bank, trade


def test_default_preview_has_native_dates_and_non_income_semantics(evidence):
    result = tool.plan_funding_repair(*evidence[:3])
    assert result["mode"] == "PREVIEW" and not result["executable"]
    assert result["production_write_supported"] is False
    request = result["request"]
    bank, trade = request["bank_fact"], request["trade_fact"]
    assert (bank["cashflow_type"], bank["amount"]) == ("investment_funding", "-8123.45")
    assert not bank["affects_nav"] and not bank["affects_spending"] and bank["affects_investment_capital"]
    assert (trade["trade_type"], trade["side"], trade["net_cash"]) == ("deposit", "deposit", "8123.45")
    assert all(trade[k] is False for k in ("affects_position", "affects_cost_basis", "affects_realized_pnl"))
    assert trade["quantity"] is None and trade["realized_pnl"] is None
    assert bank["event_date"] == trade["trade_date"] == trade["settle_date"] == "2024-02-03"
    assert bank["posting_date"] == "2024-02-04"
    assert request["broker_evidence"]["report_date"] == "2024-02-05"
    assert trade["source_id"] == "TX-ALPHA" and trade["source_key"] == "U_SYNTHETIC:TX-ALPHA"
    assert bank["source_hash"] == evidence[0]["source_hash"]
    assert evidence[0]["bank_reference"] != trade["source_id"]
    assert plan(evidence) == request


@pytest.mark.parametrize("bank_present,trade_present", [(False, False), (True, False), (False, True), (True, True)])
def test_existing_sides_selected_before_insertion_and_sources_notes_preserved(evidence, bank_present, trade_present):
    bank, trade = existing(evidence)
    request = plan(evidence, cashflows=[bank] if bank_present else [], trades=[trade] if trade_present else [])
    desired_bank, desired_trade = tool._desired(request, approve(request), 31)
    if bank_present:
        for field in ("source_table", "source_id", "source_hash", "source_uid", "description"):
            assert desired_bank[field] == bank[field]
        assert desired_bank["raw_json"]["user_note"] == bank["raw_json"]["user_note"]
        assert desired_bank["transfer_group_id"] == "preserve-user-group"
    if trade_present:
        for field in ("source_table", "source_id", "source_hash", "source_key"):
            assert desired_trade[field] == trade[field]
        assert desired_trade["raw_json"]["user_note"] == trade["raw_json"]["user_note"]
    assert desired_bank["cashflow_type"] == "investment_funding"
    assert desired_trade["cashflow_id"] == 31
    assert tool.AUDIT_KEY in desired_bank["raw_json"] and tool.AUDIT_KEY in desired_trade["raw_json"]


@pytest.mark.parametrize("field,value", [("amount", "8123.45"), ("amount", None), ("amount", "NaN"),
    ("amount", "-1.12345"), ("amount", -8123.45), ("fx_rate_to_cny", None), ("fx_rate_to_cny", "0"),
    ("currency", "mxn"), ("masked_account", "guess-full-account"), ("counterparty", "Other Broker"),
    ("source_hash", "missing"), ("evidence_sha256", "missing")])
def test_incomplete_wrong_or_inexact_bank_evidence_rejected(evidence, field, value):
    evidence[0][field] = value
    with pytest.raises(ValueError):
        plan(evidence)


@pytest.mark.parametrize("field,value", [("account_id", "U_OTHER"), ("account_code", "other_local"),
    ("transaction_id", "bank-ref-not-id"), ("source_key", "bank-reference"), ("source_hash", "d" * 64),
    ("amount", "8123.46"), ("type", "Interest"), ("report_date", "2024-02-02"),
    ("settle_date", "2024-02-06"), ("currency", "USD")])
def test_broker_identity_semantics_and_dates_not_guessed(evidence, field, value):
    evidence[1][0][field] = value
    with pytest.raises(ValueError):
        plan(evidence)


def test_explicit_cross_day_evidence_preserves_both_business_dates(evidence):
    evidence[1][0]["trade_date"] = "2024-02-04"
    evidence[1][0]["settle_date"] = "2024-02-04"
    with pytest.raises(ValueError):
        plan(evidence)
    request = plan(evidence, broker_business_date="2024-02-04")
    assert request["bank_fact"]["event_date"] == "2024-02-03"
    assert request["trade_fact"]["trade_date"] == "2024-02-04"


def test_duplicate_authoritative_candidates_rejected_even_same_id(evidence):
    evidence[1].append(deepcopy(evidence[1][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        plan(evidence)


def test_duplicate_native_id_across_different_dates_not_a_new_deposit(evidence):
    evidence[1][1]["transaction_id"] = evidence[1][0]["transaction_id"]
    with pytest.raises(ValueError, match="Duplicate"):
        plan(evidence)


@pytest.mark.parametrize("side", ["bank", "broker"])
def test_duplicate_existing_economic_facts_rejected_even_distinct_sources(evidence, side):
    bank, trade = existing(evidence)
    rows = [bank, deepcopy(bank)] if side == "bank" else [trade, deepcopy(trade)]
    rows[1]["source_id"] += "-other"
    rows[1]["source_hash"] = "d" * 64
    with pytest.raises(ValueError, match="ambiguous"):
        plan(evidence, **({"cashflows": rows} if side == "bank" else {"trades": rows}))


@pytest.mark.parametrize("side,field,value", [("bank", "counter_account_code", "other_broker"),
    ("bank", "review_status", "rejected"), ("broker", "cashflow_id", 99),
    ("broker", "quantity", "2"), ("broker", "fee_amount", "1"), ("broker", "realized_pnl", "2"),
    ("broker", "trade_date", "2024-02-05"), ("broker", "settle_date", None),
    ("broker", "trade_type", "interest"), ("broker", "affects_cost_basis", True),
    ("broker", "currency", "USD"),
    ("bank", "fx_rate_to_cny", "0.1"), ("bank", "amount_cny", "-1"),
    ("broker", "fx_rate_to_cny", "0.2"), ("broker", "net_cash_cny", "1")])
def test_existing_conflicts_are_not_overwritten_or_duplicated(evidence, side, field, value):
    bank, trade = existing(evidence)
    (bank if side == "bank" else trade)[field] = value
    with pytest.raises(ValueError):
        plan(evidence, cashflows=[bank], trades=[trade])


def test_narrow_xml_reader_and_file_binding_reject_tampering_or_omitted_rows(evidence):
    bank, brokers, mapping, receipt, report = evidence
    assert len(brokers) == 2  # Interest is not a deposit.
    tool._verify_files(bank, brokers, mapping, receipt, report)
    with pytest.raises(ValueError, match="complete native"):
        tool._verify_files(bank, brokers[:1], mapping, receipt, report)
    receipt.write_bytes(b"changed")
    with pytest.raises(ValueError, match="receipt bytes"):
        tool._verify_files(bank, brokers, mapping, receipt, report)
    report.write_bytes(XML + b" ")
    with pytest.raises(ValueError, match="report bytes"):
        tool.read_broker_cash_evidence(report, brokers[0]["report_sha256"],
                        account_id="U_SYNTHETIC", account_code="synthetic_broker", fx_rate_to_cny="1")


def test_fx_by_business_date_currency_not_report_base_or_unrelated_day(evidence):
    bank, _, mapping, receipt, report = evidence
    brokers = tool.read_broker_cash_evidence(report, hashlib.sha256(XML).hexdigest(),
                account_id=mapping["broker_account_id"], account_code=mapping["broker_account_code"],
                fx_rate_to_cny={("2024-02-03", "MXN"): "0.3456789012"})
    assert brokers[1]["fx_rate_to_cny"] is None
    tool._verify_files(bank, brokers, mapping, receipt, report)
    request = tool.plan_funding_repair(bank, brokers, mapping)["request"]
    assert request["broker_evidence"]["fx_rate_to_cny"] == "0.3456789012"


def test_two_distinct_native_ids_with_same_financial_match_are_ambiguous(evidence):
    other = deepcopy(evidence[1][0])
    other["transaction_id"] = "TX-DISTINCT"
    evidence[1].append(other)
    with pytest.raises(ValueError, match="Unique"):
        plan(evidence)


@pytest.mark.parametrize("change", ["missing", "hash", "purpose", "approver", "reference", "expired", "future",
                                    "naive", "long_ttl", "risks", "extra", "request"])
def test_approval_binds_entire_request_before_state_risks_and_time(evidence, change):
    request = plan(evidence)
    approval = approve(request)
    if change == "missing":
        approval = {}
    elif change == "hash":
        approval["request_sha256"] = "0" * 64
    elif change == "purpose":
        approval["purpose"] = "production"
    elif change in {"approver", "reference"}:
        approval["approved_by" if change == "approver" else "reference"] = " "
    elif change in {"expired", "future", "naive", "long_ttl"}:
        approval[{"expired": "expires_at", "future": "issued_at", "naive": "issued_at", "long_ttl": "expires_at"}[change]] = {
            "expired": (tool.utc_now() - timedelta(seconds=1)).isoformat(),
            "future": (tool.utc_now() + timedelta(seconds=30)).isoformat(), "naive": "2024-01-01T00:00:00",
            "long_ttl": (tool.utc_now() + timedelta(days=2)).isoformat()}[change]
    elif change == "risks":
        approval["accepted_risks"] = []
    elif change == "extra":
        approval["force"] = True
    else:
        request["before_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        tool.validate_approval(request, approval)


def test_low_decimal_context_does_not_round_native_amounts(evidence):
    expected = plan(evidence)
    with localcontext() as ctx:
        ctx.prec = 4
        actual = plan(evidence)
    assert actual == expected


def test_existing_native_identity_hash_drift_is_not_retagged(evidence):
    bank, trade = existing(evidence)
    bank.update(source_table=evidence[0]["source_table"], source_id=evidence[0]["source_id"],
                source_uid=evidence[0]["source_key"])
    with pytest.raises(ValueError, match="Native source"):
        plan(evidence, cashflows=[bank], trades=[trade])


@pytest.mark.parametrize("wrapper", [(), ("raw_json",), ("raw_json", "source_row"),
    ("raw_json", "statement_row"), ("raw_json", "raw"), ("raw_json", "source_record"),
    ("raw_json", "source_payload"), ("raw_json", "source_row", "raw_json"),
    ("raw_json", "source_row", "raw"), ("raw_json", "source_row", "statement_row"),
    ("raw_json", "source_row", "raw_json", "source_record"), ("broker_identity",)])
@pytest.mark.parametrize("field", ["transactionID", "transaction_id", "accountId", "acctId", "account_id", "source_account_id", "currency"])
def test_conflicting_raw_identity_in_any_source_wrapper_blocks(evidence, wrapper, field):
    _, trade = existing(evidence)
    node = trade
    for key in wrapper:
        node = node.setdefault(key, {})
    node[field] = "EXPLICIT-CONFLICT"
    with pytest.raises(ValueError, match="identity"):
        plan(evidence, trades=[trade])


@pytest.mark.parametrize("side", ["bank", "broker"])
@pytest.mark.parametrize("wrapper", [(), ("raw_json",), ("raw_json", "source_row"),
                                     ("raw_json", "source_row", "raw_json")])
@pytest.mark.parametrize("change", ["id", "key", "uid", "hash"])
def test_same_source_key_or_identity_never_hides_other_provenance_conflicts(evidence, side, wrapper, change):
    bank, trade = existing(evidence)
    source = evidence[0] if side == "bank" else evidence[1][0]
    row = bank if side == "bank" else trade
    node = row
    for key in wrapper:
        node = node.setdefault(key, {})
    node.update({key: source[key] for key in ("source_table", "source_id", "source_key", "source_hash")})
    if side == "bank": node["source_uid"] = source["source_key"]
    if change == "id": node["source_id"] = "DIFFERENT-NATIVE-ID"
    if change == "key": node["source_key"] = "DIFFERENT-NATIVE-KEY"
    if change == "uid": node["source_uid"] = "DIFFERENT-NATIVE-UID"
    if change == "hash": node["source_hash"] = "d" * 64
    with pytest.raises(ValueError, match="Native source"):
        plan(evidence, **({"cashflows": [row]} if side == "bank" else {"trades": [row]}))


@pytest.mark.parametrize("side", ["bank", "broker"])
def test_economic_match_without_identified_provenance_is_not_accepted(evidence, side):
    bank, trade = existing(evidence)
    unknown = bank if side == "bank" else trade
    unknown["raw_json"] = {"user_note": "unidentified manual item"}
    with pytest.raises(ValueError, match="Unidentified economic"):
        plan(evidence, **({"cashflows": [unknown]} if side == "bank" else {"trades": [unknown]}))


def test_unrelated_unidentified_facts_and_user_note_ids_do_not_block(evidence):
    bank, trade = existing(evidence)
    bank.update(event_date="2024-01-01", raw_json={"user_note": {"transactionID": "not-native"}})
    trade.update(trade_date="2024-01-01", raw_json={"user_note": {"transactionID": "not-native"}})
    request = plan(evidence, cashflows=[bank], trades=[trade])
    assert request["bank_before"] is None and request["trade_before"] is None
    assert request["protected_sha256"] == tool.fingerprint(tool._state([bank], [trade]))


def test_unrelated_stock_purchase_with_same_gross_is_not_treated_as_unidentified_deposit(evidence):
    _, trade = existing(evidence)
    trade.update(trade_type="buy", side="buy", net_cash="-8123.45", raw_json={"user_note": "manual stock purchase"})
    request = plan(evidence, trades=[trade])
    assert request["trade_before"] is None
    assert request["protected_sha256"] == tool.fingerprint(tool._state([], [trade]))


def test_correct_nested_native_identity_can_preserve_legacy_sources(evidence):
    bank, trade = existing(evidence)
    trade["raw_json"] = {"source_row": {"raw_json": {"raw": {
        "transactionID": evidence[1][0]["transaction_id"], "accountId": evidence[2]["broker_account_id"]}}}}
    request = plan(evidence, cashflows=[bank], trades=[trade])
    assert request["trade_before"]["source_id"] == trade["source_id"]


@pytest.mark.parametrize("field", ["source_key", "source_id"])
def test_native_fact_same_key_hash_does_not_mask_other_identity_conflicts(evidence, field):
    request = plan(evidence)
    native = {**request["trade_fact"], "trade_id": 72}
    assert native["source_hash"] == evidence[1][0]["source_hash"]
    assert plan(evidence, trades=[native])["trade_before"]["trade_id"] == 72
    native[field] = "changed"
    with pytest.raises(ValueError, match="Native source"):
        plan(evidence, trades=[native])


def test_embedded_uid_alone_discovers_conflicting_identity_without_economic_match(evidence):
    _, trade = existing(evidence)
    source = evidence[1][0]
    trade.update(currency="USD", raw_json={"source_record": {
        "source_table": source["source_table"], "source_uid": source["source_key"],
        "source_id": "OTHER", "source_hash": "d" * 64}})
    with pytest.raises(ValueError, match="Native source|currency identity"):
        plan(evidence, trades=[trade])


@pytest.mark.parametrize("change", ["outside", "missing_start", "missing_end", "reversed", "ancestor_period",
    "ancestor_account", "duplicate_statement", "orphan", "unbound", "missing_statement_account", "conflicting_leaf_account"])
def test_xml_report_date_must_belong_to_unique_account_statement_and_ancestors(evidence, change):
    root = ET.fromstring(XML)
    statement = root.find(".//FlexStatement")
    first = statement.find("CashTransaction")
    if change == "outside": statement.set("fromDate", "20250101"); statement.set("toDate", "20250101")
    if change == "missing_start": statement.attrib.pop("fromDate")
    if change == "missing_end": statement.attrib.pop("toDate")
    if change == "reversed": statement.set("fromDate", "20240401")
    if change == "ancestor_period": root.set("fromDate", "20250101"); root.set("toDate", "20250101")
    if change == "ancestor_account": root.set("accountId", "OTHER")
    if change == "duplicate_statement": root.find("FlexStatements").append(deepcopy(statement))
    if change == "orphan": statement.remove(first); root.append(first)
    if change == "unbound": statement.attrib.pop("accountId"); first.attrib.pop("accountId")
    if change == "missing_statement_account": statement.attrib.pop("accountId")
    if change == "conflicting_leaf_account": first.set("acctId", "OTHER")
    raw = ET.tostring(root)
    evidence[4].write_bytes(raw)
    with pytest.raises(ValueError):
        tool.read_broker_cash_evidence(evidence[4], hashlib.sha256(raw).hexdigest(),
                account_id=evidence[2]["broker_account_id"], account_code=evidence[2]["broker_account_code"],
                fx_rate_to_cny="0.3456789012")


def test_report_day_scope_does_not_relabel_earlier_business_or_settlement_day(evidence):
    root = ET.fromstring(XML)
    statement = root.find(".//FlexStatement")
    for child in list(statement)[1:]: statement.remove(child)
    statement.set("fromDate", "20240205")
    statement.set("toDate", "20240205")
    raw = ET.tostring(root)
    evidence[4].write_bytes(raw)
    brokers = tool.read_broker_cash_evidence(evidence[4], hashlib.sha256(raw).hexdigest(),
                account_id=evidence[2]["broker_account_id"], account_code=evidence[2]["broker_account_code"],
                fx_rate_to_cny="0.3456789012")
    request = tool.plan_funding_repair(evidence[0], brokers, evidence[2])["request"]
    assert request["trade_fact"]["trade_date"] == request["trade_fact"]["settle_date"] == "2024-02-03"
    assert request["broker_evidence"]["report_date"] == "2024-02-05"


@pytest.mark.parametrize("rounding", [ROUND_FLOOR, ROUND_CEILING])
def test_full_decimal_context_is_independent_of_ambient_traps_exponents_and_rounding(evidence, rounding):
    expected = plan(evidence)
    assert Decimal(expected["bank_fact"]["amount_cny"]) == Decimal("-2808.1053")
    assert Decimal(expected["trade_fact"]["net_cash_cny"]) == Decimal("2808.1052699531")
    hostile = Context(prec=3, rounding=rounding, Emin=-2, Emax=2, capitals=0, clamp=1,
                      flags=[], traps=[Inexact, Rounded, Overflow, Underflow, Subnormal, Clamped])
    with localcontext(hostile) as outer:
        state = outer.copy()
        assert plan(evidence) == expected
        assert outer.prec == state.prec and outer.rounding == state.rounding
        assert outer.traps == state.traps and outer.flags == state.flags
        assert outer.Emax == state.Emax and outer.Emin == state.Emin and outer.clamp == state.clamp


class ObservedConnection(connection):
    fail_before_commit = False
    fail_after_commit = False

    def commit(self):
        if self.fail_before_commit:
            self.fail_before_commit = False
            raise RuntimeError("injected before commit")
        result = super().commit()
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("injected acknowledgement failure after server commit")
        return result


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PAMS_TEST_POSTGRES_DSN not configured; no application PG* fallback")
    yield lambda **kwargs: psycopg2.connect(dsn, **kwargs)


@pytest.fixture
def db(database):
    conn = database(connection_factory=ObservedConnection)
    conn.funding_schema = "pams_ibkr_funding_test_" + uuid4().hex
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(conn.funding_schema)))
        cur.execute(sql.SQL("COMMENT ON SCHEMA {} IS %s").format(sql.Identifier(conn.funding_schema)), (tool.MARKER,))
        cur.execute(sql.SQL("SET search_path = {}, pg_catalog").format(sql.Identifier(conn.funding_schema)))
        cur.execute("CREATE TABLE pams_asset_master(asset_id varchar(120) PRIMARY KEY)")
        cur.execute((ROOT / "database/postgres/schema/024_cashflow_and_trade_fact.sql").read_text(encoding="utf-8"))
        # Already inclusive snapshot sentinel. This module must not adjust it a second time.
        cur.execute("CREATE TABLE pams_holdings_snapshot (id int, market_value numeric, quantity numeric, cost numeric); "
                    "INSERT INTO pams_holdings_snapshot VALUES (1, 12345.67, 8, 345)")
    try:
        yield conn
    finally:
        conn.rollback()
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(conn.funding_schema)))
        conn.close()


def invoke(conn, evidence, **kwargs):
    return tool.reconcile_funding(conn, *evidence[:3], receipt_path=evidence[3], report_path=evidence[4],
                                  schema=conn.funding_schema, **kwargs)


def db_state(conn):
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        result = {}
        for name in ("pams_cashflow_fact", "pams_investment_trade_fact", "pams_holdings_snapshot"):
            cur.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(name)))
            result[name] = [dict(row) for row in cur.fetchall()]
        return result


def seed(conn, evidence, bank_present, trade_present):
    bank, trade = existing(evidence)
    bank.pop("cashflow_id")
    trade.pop("trade_id")
    with conn, conn.cursor() as cur:
        if bank_present:
            assert insert_cashflow_facts(cur, [bank]) == 1
        if trade_present:
            assert insert_investment_trade_facts(cur, [trade]) == 1


def test_pg_preview_readonly_and_deterministic(db, evidence, monkeypatch):
    before = db_state(db)
    original = tool.plan_funding_repair
    def checked(*args, **kwargs):
        with db.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
        return original(*args, **kwargs)
    monkeypatch.setattr(tool, "plan_funding_repair", checked)
    assert invoke(db, evidence) == invoke(db, evidence)
    assert db_state(db) == before


@pytest.mark.parametrize("bank_present,trade_present", [(False, False), (True, False), (False, True), (True, True)])
def test_pg_apply_links_only_missing_sides_repeat_never_adds_extra_deposit(db, evidence, bank_present, trade_present):
    seed(db, evidence, bank_present, trade_present)
    before = db_state(db)
    request = invoke(db, evidence)["request"]
    approval = approve(request)
    result = invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert result["inserted"] == int(not bank_present) + int(not trade_present)
    after = db_state(db)
    assert after["pams_holdings_snapshot"] == before["pams_holdings_snapshot"]
    bank, trade = after["pams_cashflow_fact"][0], after["pams_investment_trade_fact"][0]
    assert len(after["pams_cashflow_fact"]) == len(after["pams_investment_trade_fact"]) == 1
    assert trade["cashflow_id"] == bank["cashflow_id"]
    assert not bank["affects_spending"] and not bank["affects_nav"]
    assert trade["net_cash"] == -bank["amount"]
    for present, name in ((bank_present, "pams_cashflow_fact"), (trade_present, "pams_investment_trade_fact")):
        if present:
            for field in ("source_table", "source_id", "source_hash", "created_at", "updated_at"):
                assert after[name][0][field] == before[name][0][field]
            assert after[name][0]["raw_json"]["user_note"] == before[name][0]["raw_json"]["user_note"]
    retry = invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert retry["status"] == "already_applied_in_isolation" and retry["inserted"] == 0
    assert db_state(db) == after


@pytest.mark.parametrize("failure", ["broker_insert", "post_insert_verify", "expiry"])
def test_pg_failure_after_bank_insertion_rolls_back_both_sides(db, evidence, monkeypatch, failure):
    request = invoke(db, evidence)["request"]
    approval = approve(request)
    before = db_state(db)
    if failure == "broker_insert":
        def fail(*args):
            raise RuntimeError("injected broker insert failure")
        monkeypatch.setattr(tool, "insert_investment_trade_facts", fail)
    elif failure == "post_insert_verify":
        monkeypatch.setattr(tool, "_verify_after", lambda *a: (_ for _ in ()).throw(RuntimeError("injected verify failure")))
    else:
        original = tool._verify_after
        def expire(*args):
            result = original(*args)
            monkeypatch.setattr(tool, "utc_now", lambda: tool.datetime.fromisoformat(approval["expires_at"]))
            return result
        monkeypatch.setattr(tool, "_verify_after", expire)
    with pytest.raises((RuntimeError, ValueError)):
        invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert db_state(db) == before


@pytest.mark.parametrize("change", ["before", "projection", "file", "marker", "no_test_dsn", "approval"])
def test_pg_drift_forged_request_and_missing_guards_never_write(db, evidence, monkeypatch, change):
    request = invoke(db, evidence)["request"]
    if change == "before":
        seed(db, evidence, True, False)
    elif change == "projection":
        request["bank_fact"]["amount"] = "-1"
        request["request_sha256"] = tool.fingerprint({k: v for k, v in request.items() if k != "request_sha256"})
    elif change == "file":
        evidence[3].write_bytes(b"tampered")
    elif change == "marker":
        with db, db.cursor() as cur:
            cur.execute(sql.SQL("COMMENT ON SCHEMA {} IS 'not-approved'").format(sql.Identifier(db.funding_schema)))
    elif change == "no_test_dsn":
        monkeypatch.delenv("PAMS_TEST_POSTGRES_DSN")
    approval = approve(request)
    if change == "approval":
        approval["accepted_risks"] = []
    before = db_state(db)
    try:
        with pytest.raises(ValueError):
            invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
        assert db_state(db) == before
    finally:
        if change == "marker":
            with db, db.cursor() as cur:
                cur.execute(sql.SQL("COMMENT ON SCHEMA {} IS %s").format(sql.Identifier(db.funding_schema)), (tool.MARKER,))


def test_pg_retry_detects_changed_pair_or_unrelated_fact(db, evidence):
    request = invoke(db, evidence)["request"]
    approval = approve(request)
    invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    with db, db.cursor() as cur:
        cur.execute("UPDATE pams_investment_trade_fact SET net_cash=net_cash+1")
    before = db_state(db)
    with pytest.raises(ValueError):
        invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert db_state(db) == before


@pytest.mark.parametrize("history_count", [3, 9, 17])
def test_pg_one_missing_deposit_then_replay_never_adds_another(db, evidence, history_count):
    request = plan(evidence)
    with db, db.cursor() as cur:
        for i in range(history_count):
            historical = deepcopy(request["trade_fact"])
            key = "synthetic-history:" + str(i)
            historical.update(trade_date="2024-01-01", settle_date="2024-01-01", source_table="synthetic_history",
                              source_id=key, source_key=key,
                              source_hash=make_source_hash("synthetic_history", {"source_key": key}), raw_json={})
            assert insert_investment_trade_facts(cur, [historical]) == 1
    request = invoke(db, evidence)["request"]
    approval = approve(request)
    invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert len(db_state(db)["pams_investment_trade_fact"]) == history_count + 1
    invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert len(db_state(db)["pams_investment_trade_fact"]) == history_count + 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_pg_commit_failure_either_rolls_back_or_replays_committed_audit_without_duplicate(db, evidence, after_commit):
    request = invoke(db, evidence)["request"]
    approval = approve(request)
    before = db_state(db)
    db.fail_before_commit = not after_commit
    db.fail_after_commit = after_commit
    with pytest.raises(RuntimeError, match="injected"):
        invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    if not after_commit:
        assert db_state(db) == before
    result = invoke(db, evidence, mode="APPLY_ISOLATED", request=request, approval=approval)
    assert result["status"] == ("already_applied_in_isolation" if after_commit else "applied_in_isolation")
    state = db_state(db)
    assert len(state["pams_cashflow_fact"]) == len(state["pams_investment_trade_fact"]) == 1


@pytest.mark.parametrize("change", ["currency", "source_key", "raw_transaction", "unknown"])
def test_pg_conflicting_or_unidentified_existing_fact_never_writes_bank_side(db, evidence, change):
    _, trade = existing(evidence)
    trade.pop("trade_id")
    if change == "currency": trade["currency"] = "USD"
    if change == "source_key": trade["raw_json"]["source_row"]["source_key"] = "wrong-key"
    if change == "raw_transaction": trade["raw_json"]["source_row"]["raw_json"] = {"transactionID": "OTHER"}
    if change == "unknown": trade["raw_json"] = {}
    with db, db.cursor() as cur:
        assert insert_investment_trade_facts(cur, [trade]) == 1
    before = db_state(db)
    with pytest.raises(ValueError):
        invoke(db, evidence)
    assert db_state(db) == before


def _private_bundle():
    directory = os.environ.get("PAMS_IBKR_ALIGNMENT_PRIVATE_DIR")
    frozen_dsn = os.environ.get("PAMS_IBKR_FROZEN_READONLY_DSN")
    if not directory or not frozen_dsn:
        pytest.skip("Private replay requires explicit evidence directory and frozen read-only DSN")
    directory = Path(directory).resolve()
    comparison = json.loads((directory / "PAMS_READONLY_COMPARISON.json").read_text(encoding="utf-8"))
    xml_path = Path(comparison["inputs"]["audit_xml"]["path"])
    raw = xml_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == comparison["inputs"]["audit_xml"]["sha256"]
    return directory, comparison, xml_path, raw, frozen_dsn


def _private_artifact(directory, name, payload):
    output = os.environ.get("PAMS_IBKR_FUNDING_PRIVATE_OUTPUT")
    if not output:
        return
    destination = Path(output).resolve()
    assert destination.is_relative_to(directory.parent) and destination.is_dir()
    data = {"observed_at_utc": tool.utc_now().isoformat(), "production_write_supported": False,
            "funding_service_sha256": hashlib.sha256(Path(tool.__file__).read_bytes()).hexdigest(), **payload}
    (destination / name).write_text(json.dumps(data, default=str, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _frozen_columns(cur, table):
    cur.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (table,))
    return cur.fetchall()


def _insert_full_rows(cur, schema, table, columns, rows):
    fields = [column for column, _ in columns]
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(schema, table),
        sql.SQL(",").join(map(sql.Identifier, fields)), sql.SQL(",").join(sql.Placeholder() for _ in fields))
    for row in rows:
        cur.execute(statement, tuple(Json(row[field]) if datatype in ("json", "jsonb") and row[field] is not None
                                     else row[field] for field, datatype in columns))


def _serialized_rows(conn, schema, table):
    with conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT id,row_to_json(t)::text FROM {} t ORDER BY id").format(sql.Identifier(schema, table)))
        return dict(cur.fetchall())


def test_private_real_historical_delete_only_rehearsal_rollback_idempotence(db, monkeypatch):
    from domain.brokers.ibkr import ibkr_snapshot_alignment as alignment
    directory, comparison, _, raw, frozen_dsn = _private_bundle()
    name = "snapshot-private-delete-rehearsal.json"
    _private_artifact(directory, name, {"status": "started", "executed": False})
    started = time.perf_counter()
    original_schema = alignment.SCHEMA
    root = ET.fromstring(raw)
    evidence_days = sorted({item["as_of"] for item in comparison["history"]["extra_internal_positions"]})
    assert len(evidence_days) == 1
    day = evidence_days[0]
    account = comparison["scope"]["account_id"]
    with psycopg2.connect(frozen_dsn, connect_timeout=5) as frozen:
        frozen.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with frozen.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='15000ms'")
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
            cur.execute("SELECT DISTINCT account_code FROM public.pams_ibkr_position_history WHERE as_of=%s AND source_account_id=%s", (day, account))
            codes = cur.fetchall()
            assert len(codes) == 1
            account_code = codes[0][0]
            columns = {table: _frozen_columns(cur, table) for table in alignment.TABLES}
        with frozen.cursor(cursor_factory=RealDictCursor) as cur:
            frozen_rows = {}
            for table in alignment.TABLES:
                cur.execute(sql.SQL("SELECT * FROM public.{} WHERE account_code=%s AND as_of=%s ORDER BY id")
                            .format(sql.Identifier(table)), (account_code, day))
                frozen_rows[table] = [dict(row) for row in cur.fetchall()]
    frozen.close()
    with db, db.cursor() as cur:
        for table in alignment.TABLES:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(db.funding_schema, table)))
            definitions = sql.SQL(",").join(sql.SQL("{} {}").format(sql.Identifier(field), sql.SQL(datatype))
                                            for field, datatype in columns[table])
            cur.execute(sql.SQL("CREATE TABLE {} ({},PRIMARY KEY(id),UNIQUE(account_code,name,as_of))")
                        .format(sql.Identifier(db.funding_schema, table), definitions))
            _insert_full_rows(cur, db.funding_schema, table, columns[table], frozen_rows[table])
    monkeypatch.setattr(alignment, "SCHEMA", db.funding_schema)
    with db:
        before = alignment.read_snapshot_rows(db, account_code=account_code, as_of=day)
        scales = alignment.read_snapshot_numeric_scales(db)
    before_bytes = {table: _serialized_rows(db, db.funding_schema, table) for table in alignment.TABLES}
    setup_seconds = time.perf_counter() - started
    assert setup_seconds <= 60, "Bounded private setup exceeded; do not continue applying"
    source, desired = alignment.source_snapshot_from_xml(source_bytes=raw, account_code=account_code,
        source_account_id=account, as_of=day, source_key="isolated-complete-day:" + day,
        authority_reference="private fixed-acceptance full-account/day review; isolated rehearsal ONLY",
        numeric_scales=scales, authoritative=True, complete_account=True)
    conids = {}
    for node in root.iter("OpenPosition"):
        if node.get("accountId") == account:
            conids.setdefault((node.get("symbol"), node.get("currency")), set()).add("conid:" + node.get("conid"))
    desired_keys = {(row["resolved_identity"], row["currency"]) for row in desired}
    ownership, candidates, trade_proofs = [], [], []
    for table in alignment.TABLES:
        for row in before[table]:
            cash = row["symbol"] == "CASH_" + row["currency"]
            ids = {"cash:" + row["currency"]} if cash else conids[row["symbol"], row["currency"]]
            assert len(ids) == 1
            identity = next(iter(ids))
            proof = {"table": table, "id": row["id"], "row_sha256": alignment.fingerprint([row]),
                     "resolved_identity": identity, "kind": "cash" if cash else "position", "provenance": "unknown",
                     "reference": "frozen full-row identity binding only; no inferred ownership"}
            ownership.append(proof)
            if (identity, row["currency"]) not in desired_keys:
                trades = [node.attrib for node in root.iter("Trade") if node.get("accountId") == account
                          and "conid:" + node.get("conid", "") == identity and node.get("currency") == row["currency"]]
                assert len(trades) == 1 and trades[0]["buySell"] == "BUY"
                trade = trades[0]
                assert tool._xml_day(trade["tradeDate"]) > day
                assert Decimal(trade["quantity"]) == row["quantity"]
                trade_proof = {"source_sha256": source["source_sha256"], "official_complete_day_absence": True,
                               "resolved_identity": identity, "currency": row["currency"], "later_unique_buy": trade}
                trade_proofs.append(trade_proof)
                candidates.append({"table": table, "id": row["id"], "row_sha256": proof["row_sha256"],
                    "source_sha256": source["source_sha256"], "source_account_id": account,
                    "reference": "isolated complete-day absence + later unique buy: " + tool.fingerprint(trade_proof)})
    request = alignment.plan_snapshot_alignment(evidence=source, desired_rows=desired, before_rows=before,
        before_sha256={table: alignment.fingerprint(before[table]) for table in alignment.TABLES},
        ownership=ownership, candidate_proofs=candidates)
    assert len(request["operations"]) == 2 and all(op["action"] == "delete" for op in request["operations"])
    now = tool.utc_now()
    approval = {"purpose": "ibkr_historical_snapshot_alignment", "plan_sha256": request["plan_sha256"],
                "approved_by": "Codex_lead_isolated_rehearsal", "reference": "isolated rehearsal ONLY; NOT production permission",
                "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=15)).isoformat()}
    original_write = alignment._write
    def fail_second(cur, op, scope):
        original_write(cur, op, scope)
        if op["table"] == alignment.HOLDINGS:
            raise RuntimeError("isolated forced failure after second delete")
    monkeypatch.setattr(alignment, "_write", fail_second)
    with pytest.raises(RuntimeError, match="second delete"):
        alignment.apply_snapshot_alignment(db, request, source_bytes=raw, dry_run=False, authorization=approval)
    assert {table: _serialized_rows(db, db.funding_schema, table) for table in alignment.TABLES} == before_bytes
    monkeypatch.setattr(alignment, "_write", original_write)
    result = alignment.apply_snapshot_alignment(db, request, source_bytes=raw, dry_run=False, authorization=approval)
    assert result["changed"] == 2 and result["policy"] == "historical_delete_only"
    after_bytes = {table: _serialized_rows(db, db.funding_schema, table) for table in alignment.TABLES}
    for table in alignment.TABLES:
        deleted = {op["id"] for op in request["operations"] if op["table"] == table}
        assert set(before_bytes[table]) - set(after_bytes[table]) == deleted
        assert {key: value for key, value in before_bytes[table].items() if key not in deleted} == after_bytes[table]
    retry = alignment.apply_snapshot_alignment(db, request, source_bytes=raw, dry_run=False, authorization=approval)
    assert retry["status"] == "already_aligned" and retry["changed"] == 0
    assert {table: _serialized_rows(db, db.funding_schema, table) for table in alignment.TABLES} == after_bytes
    _private_artifact(directory, name, {"status": "passed", "executed": True, "production_approval": False,
        "source_sha256": source["source_sha256"], "source_date": day, "setup_seconds": setup_seconds,
        "total_seconds": time.perf_counter() - started, "database": db.info.dbname, "disposable_schema": db.funding_schema,
        "full_row_counts_before": {table: len(rows) for table, rows in before.items()},
        "full_row_counts_after": {table: len(rows) for table, rows in after_bytes.items()},
        "constraints_scope": "full original column types, primary key, legacy name uniqueness; no FK/generated expressions restored",
        "candidate_proofs": candidates, "trade_proofs": trade_proofs, "plan": request, "approval": approval,
        "apply": result, "retry": retry, "rollback_both_tables_verified": True,
        "all_remaining_row_to_json_bytes_identical": True, "matched_financial_updates": 0,
        "frozen_connection_read_only": True, "original_alignment_schema": original_schema})


def test_private_real_deposits_nine_to_ten_then_idempotent_replay(db):
    directory, comparison, xml_path, raw, frozen_dsn = _private_bundle()
    attestation = os.environ.get("PAMS_IBKR_PRIVATE_BANK_ATTESTATION")
    if not attestation:
        pytest.skip("Real receipt replay requires explicit private manually verified bank fields")
    bank = json.loads(attestation)
    name = "funding-private-ten-deposit-rehearsal.json"
    _private_artifact(directory, name, {"status": "started", "executed": False})
    started = time.perf_counter()
    receipt = directory / bank.pop("receipt_filename")
    assert receipt.resolve().is_relative_to(directory)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert receipt_sha256 == bank["evidence_sha256"]
    missing = comparison["funding"]["missing_broker_side_record_in_pams"]
    assert bank["currency"] == missing["currency"] and bank["event_date"] == missing["business_date"]
    assert Decimal(bank["amount"]).copy_negate() == Decimal(missing["amount"])
    with psycopg2.connect(frozen_dsn, connect_timeout=5) as frozen:
        frozen.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with frozen.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='15000ms'")
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
            columns = {table: _frozen_columns(cur, table) for table in ("pams_cashflow_fact", "pams_investment_trade_fact")}
        with frozen.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM public.pams_investment_trade_fact
                WHERE trade_type='deposit' AND source_table='pams_cashflow_fact' AND currency=%s ORDER BY trade_id""",
                (bank["currency"],))
            trades = [dict(row) for row in cur.fetchall()]
            assert len(trades) == comparison["funding"]["pams_deposit_count"]
            codes = {row["account_code"] for row in trades}
            assert len(codes) == 1
            broker_code = next(iter(codes))
            ids = [row["cashflow_id"] for row in trades if row["cashflow_id"] is not None]
            cur.execute("SELECT * FROM public.pams_cashflow_fact WHERE cashflow_id=ANY(%s) ORDER BY cashflow_id", (ids,))
            cashflows = [dict(row) for row in cur.fetchall()]
            # Preserve the repository's native-to-CNY orientation; do not use its
            # reciprocal row, the report base rate, or a latest-date fallback.
            cur.execute("""SELECT * FROM public.pams_fx_rates_daily WHERE as_of=%s
                AND base_currency='CNY' AND quote_currency=%s""", (bank["event_date"], bank["currency"]))
            fx_rows = [dict(row) for row in cur.fetchall()]
            assert len(fx_rows) == 1 and fx_rows[0]["rate"] > 0
            fx_evidence = fx_rows[0]
    frozen.close()
    with db, db.cursor() as cur:
        for table, rows in (("pams_cashflow_fact", cashflows), ("pams_investment_trade_fact", trades)):
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                        (db.funding_schema, table))
            present = {row[0] for row in cur.fetchall()}
            for field, datatype in columns[table]:
                if field not in present:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} {}")
                        .format(sql.Identifier(db.funding_schema, table), sql.Identifier(field), sql.SQL(datatype)))
            _insert_full_rows(cur, db.funding_schema, table, columns[table], rows)
            primary_key = "cashflow_id" if table == "pams_cashflow_fact" else "trade_id"
            maximum = max((row[primary_key] for row in rows), default=1)
            cur.execute("SELECT setval(pg_get_serial_sequence(%s,%s),%s,true)",
                        (db.funding_schema + "." + table, primary_key, maximum))
    bank.update(source_table="bank_receipt", source_id="receipt-sha256:" + receipt_sha256,
                source_key="receipt:" + receipt_sha256, fx_rate_to_cny=str(fx_evidence["rate"]))
    bank["source_hash"] = make_source_hash(bank["source_table"], {"source_key": bank["source_key"]})
    mapping = {"bank_account_code": bank["account_code"], "masked_account": bank["masked_account"],
               "broker_account_code": broker_code, "broker_account_id": comparison["scope"]["account_id"],
               "counterparty": bank["counterparty"]}
    brokers = tool.read_broker_cash_evidence(xml_path, hashlib.sha256(raw).hexdigest(),
                account_id=mapping["broker_account_id"], account_code=broker_code,
                fx_rate_to_cny={(bank["event_date"], bank["currency"]): bank["fx_rate_to_cny"]})
    official_deposits = [row for row in brokers if tool._number(row["amount"]) > 0]
    assert len(official_deposits) == comparison["funding"]["broker_deposit_count"]
    before = db_state(db)
    setup_seconds = time.perf_counter() - started
    assert setup_seconds <= 60, "Bounded private setup exceeded; do not continue applying"
    request = tool.reconcile_funding(db, bank, brokers, mapping, receipt_path=receipt, report_path=xml_path,
                                     schema=db.funding_schema)["request"]
    assert request["bank_before"] is None and request["trade_before"] is None
    approval = approve(request)
    approval.update(approved_by="Codex_lead_isolated_rehearsal",
                    reference="real receipt/native deposits, isolated rehearsal ONLY; NOT production permission")
    kwargs = {"receipt_path": receipt, "report_path": xml_path, "schema": db.funding_schema,
              "mode": "APPLY_ISOLATED", "request": request, "approval": approval}
    applied = tool.reconcile_funding(db, bank, brokers, mapping, **kwargs)
    assert applied["status"] == "applied_in_isolation" and applied["inserted"] == 2
    after = db_state(db)
    assert after["pams_holdings_snapshot"] == before["pams_holdings_snapshot"]
    deposits = after["pams_investment_trade_fact"]
    assert len(deposits) == len(official_deposits)
    assert sum(row["net_cash"] for row in deposits) == sum(tool._number(row["amount"]) for row in official_deposits)
    for table, primary_key in (("pams_cashflow_fact", "cashflow_id"), ("pams_investment_trade_fact", "trade_id")):
        by_id = {row[primary_key]: row for row in after[table]}
        assert all(tool.fingerprint(row) == tool.fingerprint(by_id[row[primary_key]]) for row in before[table])
    replay = tool.reconcile_funding(db, bank, brokers, mapping, **kwargs)
    assert replay["status"] == "already_applied_in_isolation" and replay["inserted"] == 0
    assert db_state(db) == after
    _private_artifact(directory, name, {"status": "passed", "executed": True, "production_approval": False,
        "source_sha256": hashlib.sha256(raw).hexdigest(), "receipt_sha256": receipt_sha256,
        "database": db.info.dbname, "disposable_schema": db.funding_schema, "setup_seconds": setup_seconds,
        "total_seconds": time.perf_counter() - started, "frozen_connection_read_only": True,
        "copied_cohort": "full original nine deposit facts and their linked cashflow rows; not a full production restore",
        "before_deposit_count": len(trades), "before_native_deposits": sum(row["net_cash"] for row in trades),
        "after_deposit_count": len(deposits), "after_native_deposits": sum(row["net_cash"] for row in deposits),
        "original_nine_and_linked_cashflows_unchanged": True, "holdings_sentinel_unchanged": True,
        "fx_evidence": fx_evidence, "fx_convention": "existing repository base CNY / quote native, multiply native by rate",
        "remaining_risks": request["risks"], "request_sha256": request["request_sha256"], "approval": approval,
        "apply": applied, "retry": replay})
