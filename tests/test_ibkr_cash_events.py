"""Fixed synthetic official cash events; no application DB credentials or network."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))

from collectors import ibkr_flex as collector  # noqa: E402
from services import ibkr_cash_events as cash  # noqa: E402
from services.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts  # noqa: E402

CUTOFF = date(2026, 10, 21)
AS_OF = date(2026, 10, 22)


def envelope(**changes):
    raw = {"accountId": "U_TEST", "transactionID": "C1", "currency": "USD",
           "dateTime": "20261022;120000", "amount": "100", "type": "Deposits & Withdrawals"}
    raw.update(changes)
    return {"raw": raw, "statement_account_id": "U_TEST"}


def normalize(item):
    return cash.normalize_cash_event(item, account_code="ibkr", cutoff=CUTOFF, statement_as_of=AS_OF)


@pytest.mark.parametrize("kind,amount,side", [
    ("Deposits & Withdrawals", "100", "deposit"),
    ("Deposits & Withdrawals", "-50.25", "withdraw"),
    ("Dividends", "5.5", "dividend"),
    ("Payment In Lieu Of Dividends", "2", "dividend"),
    ("Withholding Tax", "-1.25", "tax"),
    ("Broker Interest Received", "1.5", "interest"),
    ("Broker Interest Paid", "-3", "interest"),
    ("Bond Interest Received", "4", "interest"),
    ("Other Fees", "-2", "fee"), ("Advisor Fees", "-4", "fee"),
])
def test_explicit_signed_cash_types(kind, amount, side):
    event = normalize(envelope(type=kind, amount=amount))
    assert event["side"] == side
    fact = build_investment_trade_fact({**event, "fx_rate_to_cny": Decimal("7")})
    assert fact["net_cash"] == Decimal(amount)
    assert fact["net_cash_cny"] == Decimal(amount) * 7
    assert not fact["affects_position"] and not fact["affects_cost_basis"]
    assert fact["quantity"] is None and fact["price"] is None
    assert fact["affects_realized_pnl"] == (side not in {"deposit", "withdraw"})


@pytest.mark.parametrize("changes,reason", [
    ({"transactionID": ""}, "transaction_id"), ({"transactionID": "0"}, "transaction_id"),
    ({"accountId": "U_OTHER"}, "source_account"), ({"acctId": "U_OTHER"}, "source_account"),
    ({"currency": ""}, "currency"), ({"currency": "BASE_SUMMARY"}, "currency"),
    ({"amount": ""}, "amount"), ({"amount": "NaN"}, "amount"),
    ({"amount": "Infinity"}, "amount"), ({"amount": "1e3"}, "amount"),
    ({"amount": "1,000"}, "amount"), ({"amount": "(100)"}, "amount"),
    ({"amount": "0"}, "amount"), ({"amount": "0.00000000001"}, "amount"),
    ({"amount": "1000000000000000000"}, "amount"),
    ({"dateTime": "", "reportDate": "20261022"}, "business_date"),
    ({"dateTime": "20260230"}, "business_date"),
    ({"dateTime": "20261023"}, "future_business_date"),
    ({"type": "Unknown"}, "unsupported"), ({"type": ""}, "unsupported"),
    ({"type": "Withholding Tax", "amount": "1"}, "sign_conflict"),
    ({"type": "Dividends", "amount": "-1"}, "sign_conflict"),
    ({"type": "Other Fees", "amount": "1"}, "sign_conflict"),
    ({"type": "Broker Interest Paid", "amount": "1"}, "sign_conflict"),
    ({"levelOfDetail": "SUMMARY"}, "detail_event"),
    ({"Amount": "200"}, "ambiguous_attribute"),
    ({"symbol": "A" * 121}, "unrepresentable"),
])
def test_missing_conflicting_or_unknown_evidence_is_not_guessed(changes, reason):
    with pytest.raises(ValueError, match=reason):
        normalize(envelope(**changes))


def test_missing_account_rejected_but_official_statement_account_is_valid():
    item = envelope(accountId="")
    assert normalize(item)["source_account_id"] == "U_TEST"
    item["statement_account_id"] = None
    with pytest.raises(ValueError, match="source_account"):
        normalize(item)


@pytest.mark.parametrize("value", ["20261020", "20261021;235959"])
def test_cutoff_is_strict_and_does_not_import_historical_65000(value):
    assert normalize(envelope(amount="65000", dateTime=value)) is None


def test_stable_identity_excludes_physical_id_report_position_date_and_amount():
    first = normalize(envelope())
    revised = normalize(envelope(amount="101"))
    assert first["source_key"] == revised["source_key"] == first["source_id"]
    assert first["cash_event_hash"] != revised["cash_event_hash"]
    assert len(first["source_key"]) <= 120
    assert normalize(envelope(transactionID="C2"))["source_key"] != first["source_key"]
    other = envelope(accountId="U_OTHER")
    other["statement_account_id"] = "U_OTHER"
    assert normalize(other)["source_key"] != first["source_key"]


def test_default_disabled_explicit_cutoff_and_invalid_config(monkeypatch):
    monkeypatch.delenv("PAMS_IBKR_CASH_EVENTS_AFTER", raising=False)
    assert cash.configured_cutoff() is None
    monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", "2026-10-21")
    assert cash.configured_cutoff() == CUTOFF
    for invalid in ("true", "20261021", "2026-02-30"):
        monkeypatch.setenv("PAMS_IBKR_CASH_EVENTS_AFTER", invalid)
        with pytest.raises(ValueError):
            cash.configured_cutoff()


class CashCursor:
    def __init__(self):
        self.facts = {}
        self.rates = [{"rate": Decimal("7")}]
        self.candidates = []
        self.statements = []
        self.result = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.statements.append(query)
        self.result = []
        self.rowcount = 0
        if "SELECT raw_json FROM" in query:
            self.result = [{"raw_json": fact["raw_json"]} for key, fact in self.facts.items() if key == params[1]]
        elif "SELECT rate FROM" in query:
            self.result = self.rates
        elif "SELECT cashflow_id FROM" in query:
            self.result = self.candidates
        elif "pg_catalog.pg_attribute" in query:
            self.result = [{"exists": True}]
        elif "AS same FROM" in query:
            previous = self.facts.get(params["source_key"])
            if previous:
                expected = {**params, "raw_json": params["raw_json"].adapted}
                self.result = [{"same": previous == expected}]
        elif "INSERT INTO pams_investment_trade_fact" in query:
            key = params["source_key"]
            if key not in self.facts:
                self.facts[key] = {**params, "raw_json": params["raw_json"].adapted}
                self.rowcount = 1
        elif "pg_advisory_xact_lock" not in query:
            pytest.fail(f"Unexpected SQL: {query}")

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None

    def cursor(self, **kwargs):
        return self


def import_events(conn, events):
    return cash.import_cash_events(conn, events, account_code="ibkr", cutoff=CUTOFF, statement_as_of=AS_OF)


def test_import_retry_and_source_key_survive_reconstructed_fact_id():
    conn = CashCursor()
    result = import_events(conn, [envelope(), envelope()])
    assert (result["inserted_count"], result["duplicate_count"]) == (1, 1)
    assert result["pending"] == [{"transaction_id": "C1", "reason": "bank_match_pending",
                                   "retryable": False, "state": "waiting_evidence",
                                   "fact_imported": True, "blocking": False}]
    saved = deepcopy(conn.facts)
    assert import_events(conn, [envelope()])["duplicate_count"] == 1
    assert saved == conn.facts
    assert not any("pams_holdings_snapshot" in query for query in conn.statements)
    revised = import_events(conn, [envelope(amount="101")])
    assert revised["pending"][0]["reason"] == "conflicting_cash_transaction"
    assert revised["retryable_count"] == 0
    assert saved == conn.facts


def test_conflicting_duplicates_reject_before_any_fact_write():
    conn = CashCursor()
    result = import_events(conn, [envelope(), envelope(amount="101")])
    assert result["pending"][0]["reason"] == "conflicting_cash_transaction"
    assert not conn.facts


@pytest.mark.parametrize("rates", [[], [{"rate": None}], [{"rate": Decimal("0")}],
                                  [{"rate": Decimal("NaN")}], [{"rate": -1}],
                                  [{"rate": 7}, {"rate": 7}]])
def test_no_fallback_fx_or_zero_when_dated_rate_missing_or_ambiguous(rates):
    conn = CashCursor()
    conn.rates = rates
    result = import_events(conn, [envelope()])
    assert result["inserted_count"] == 0 and not conn.facts
    assert result["pending"][0]["reason"] == "missing_or_invalid_dated_fx"


def test_invalid_events_pending_cny_needs_no_fx_and_tax_fee_not_double_deducted():
    conn = CashCursor()
    conn.rates = []
    result = import_events(conn, [envelope(transactionID="BAD", amount="NaN"),
                                 envelope(currency="CNY", type="Other Fees", amount="-2"),
                                 envelope(transactionID="TAX", currency="CNY", type="Withholding Tax", amount="-1")])
    assert result["inserted_count"] == 2 and len(result["pending"]) == 1
    facts = {fact["trade_type"]: fact for fact in conn.facts.values()}
    assert (facts["fee"]["net_cash"], facts["fee"]["fee_amount"], facts["fee"]["fee_currency"]) == (Decimal("-2"), Decimal("2"), "CNY")
    assert (facts["tax"]["net_cash"], facts["tax"]["tax_amount"]) == (Decimal("-1"), Decimal("1"))
    assert sum(f["realized_pnl"] for f in facts.values()) == Decimal("-3")


@pytest.mark.parametrize("candidates,expected", [([], None), ([{"cashflow_id": 12}], 12),
                                               ([{"cashflow_id": 12}, {"cashflow_id": 13}], None)])
def test_bank_link_requires_unique_explicit_identity_not_limit_one(candidates, expected):
    conn = CashCursor()
    conn.candidates = candidates
    import_events(conn, [envelope()])
    assert next(iter(conn.facts.values()))["cashflow_id"] == expected
    query = next(q for q in conn.statements if "SELECT cashflow_id FROM" in q)
    assert "LIMIT" not in query and "transactionID" in query and "accountId" in query
    assert "review_status = 'confirmed'" in query and "amount = -%(net_cash)s" in query


def test_strict_writer_rejects_source_collision_but_default_keeps_skip_contract():
    conn = CashCursor()
    fact = build_investment_trade_fact({**normalize(envelope()), "fx_rate_to_cny": Decimal("7")})
    assert insert_investment_trade_facts(conn, [fact], strict_source=True) == 1
    assert insert_investment_trade_facts(conn, [fact], strict_source=True) == 0
    revised = {**fact, "net_cash": Decimal("101")}
    with pytest.raises(ValueError, match="Conflicting immutable"):
        insert_investment_trade_facts(conn, [revised], strict_source=True)
    assert insert_investment_trade_facts(conn, [revised]) == 0


@pytest.mark.parametrize("exists", [False, True])
def test_latest_status_is_read_only_and_missing_table_is_none(monkeypatch, exists):
    class StatusConnection:
        def cursor(self, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            assert query.strip().startswith("SELECT")
            self.capability = "to_regclass" in query

        def fetchone(self):
            return (exists,) if self.capability else None

    def unexpected(*args):
        pytest.fail("A status read must not issue DDL")
    monkeypatch.setattr(collector, "ensure_sync_table", unexpected)
    assert collector.latest_sync_status(StatusConnection()) is None


def publish_cash_days(tmp_path, monkeypatch, by_day, *, missing_sections=()):
    """Synthetic published daily-close evidence consumed through the actual reader."""
    from datetime import timedelta
    import hashlib
    import json
    import xml.etree.ElementTree as ET
    from services import ibkr_daily_close as pure
    from services import ibkr_daily_close_read as daily

    root = ET.Element("FlexQueryResponse")
    for day, records in sorted(by_day.items()):
        statement = ET.SubElement(root, "FlexStatement", accountId="U_TEST", fromDate=day, toDate=day)
        ET.SubElement(statement, "AccountInformation", accountId="U_TEST", currency="USD")
        equity = ET.SubElement(statement, "EquitySummaryInBase")
        for when in ((date.fromisoformat(day) - timedelta(days=1)).isoformat(), day):
            values = {field: "0" for field in pure.COMPONENT_FIELDS}
            values.update(cash="100", total="100")
            for field, value in list(values.items()):
                values[field + "Long"] = value
                values[field + "Short"] = "0"
            ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId="U_TEST",
                          currency="USD", reportDate=when, **values)
        ET.SubElement(statement, "OpenPositions")
        flow = {field: "0" for field in pure.FLOW_FIELDS}
        flow.update(startingValue="100", endingValue="100")
        ET.SubElement(statement, "ChangeInNAV", accountId="U_TEST", currency="USD", fromDate=day, toDate=day, **flow)
        cash_section = statement if day in missing_sections else ET.SubElement(statement, "CashTransactions")
        for changes in records:
            attrs = envelope(currency="CNY", dateTime=day)["raw"]
            attrs.update(changes)
            ET.SubElement(cash_section, "CashTransaction", **attrs)
    content = ET.tostring(root, encoding="utf-8")
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / (digest + ".xml")
    source.write_bytes(content)
    manifest = tmp_path / "daily-close.json"
    manifest.write_text(json.dumps({"version": 1, "purpose": "ibkr_daily_close", "account_code": "ibkr",
        "source_account_id": "U_TEST", "source_file": source.name, "source_sha256": digest,
        "as_of": max(by_day), "period_start": min(by_day)}), encoding="utf-8")
    monkeypatch.setenv(daily.MANIFEST_ENV, str(manifest))
    loaded = daily.load_evidence()
    assert loaded["status"] == "ok", loaded
    return manifest, source, digest


@pytest.mark.parametrize("attribute", ["acctId", "ACCTID", "accountId", "model"])
def test_cash_scope_rejects_conflicting_ancestor_aliases(attribute):
    import xml.etree.ElementTree as ET
    from services.ibkr_cash_event_sync import _cash_scope_ok
    root = ET.Element("FlexStatement", accountId="U_TEST")
    section = ET.SubElement(root, "CashTransactions", **{attribute: "OTHER_ACCOUNT"})
    leaf = ET.SubElement(section, "CashTransaction", accountId="U_TEST")
    parents = {child: parent for parent in root.iter() for child in parent}
    assert not _cash_scope_ok(section, parents, "U_TEST")
    assert not _cash_scope_ok(leaf, parents, "U_TEST")


def test_cash_scope_accepts_consistent_aliases_and_explicit_empty_section():
    import xml.etree.ElementTree as ET
    from services.ibkr_cash_event_sync import _cash_scope_ok
    root = ET.Element("FlexStatement", accountId="U_TEST")
    section = ET.SubElement(root, "CashTransactions", acctId="U_TEST")
    assert _cash_scope_ok(section, {section: root}, "U_TEST")


def test_independent_service_disabled_before_loading_evidence_or_db(monkeypatch):
    from services import ibkr_cash_event_sync as service
    monkeypatch.delenv("PAMS_IBKR_CASH_EVENTS_AFTER", raising=False)
    monkeypatch.setattr(service.daily, "load_evidence", lambda: pytest.fail("disabled must not read evidence"))
    assert service.status(None)["status"] == service.sync(None)["status"] == "disabled"
    assert service.status(None, broker_source="other")["reasons"] == ["unsupported_broker_source"]
