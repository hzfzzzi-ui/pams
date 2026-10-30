"""Evidence-bound funding reconciliation; PREVIEW is the only production mode.

Integration (no endpoint, scheduler, collector or balance writer is registered):
* Manually attest a bank receipt's masked account, counterparty, signed amount,
  event/posting dates, evidence_sha256 and native source_table/id/key/hash.
* Supply an explicit account mapping (bank_account_code, masked_account,
  broker_account_code, broker_account_id, counterparty). A bank reference is NOT
  an IBKR account or transaction identity. Supply evidenced native-to-CNY FX;
  neither unknown FX nor missing financial evidence is replaced with 1 or zero.
* read_broker_cash_evidence reads ONLY native CashTransaction deposit evidence.
  plan_funding_repair is pure; reconcile_funding additionally re-reads the private
  receipt/XML bytes and database state in a server-enforced read-only preview.
* Inspect the returned request privately. An attributed, expiring approval binds
  its hash and residual risks. APPLY_ISOLATED additionally requires the project's
  explicit test DSN, a disposable random schema and marker. No production apply
  is supported. The approval is an attestation, not authentication.

Only missing facts are inserted through the established fact services. Existing
source identities and notes are retained. Audit/approval evidence lives in the
two facts' raw_json; no schema change or balance/state refresh is needed. A
deposit's affects_cash flag describes the historical event, NOT an instruction
to add it to an already inclusive broker cash/holdings snapshot.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET

from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, parse_dsn
from psycopg2.extras import Json, RealDictCursor

from domain.ledger.cashflow_fact import build_cashflow_fact, insert_cashflow_facts, make_source_hash
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts

PURPOSE = "isolated_investment_funding_repair"
MARKER = "pams-isolated-funding-repair"
BROKER_SOURCE = "ibkr_cash_transactions"
AUDIT_KEY = "funding_repair"
RISKS = ["bank_receipt_fields_manually_attested", "historical_fx_requires_independent_evidence",
         "no_balance_or_nav_recalculation", "production_write_not_authorized"]
DECIMAL_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                          capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])
SOURCE_WRAPPERS = ("raw_json", "source_row", "statement_row", "raw", "source_record", "source_payload",
                   AUDIT_KEY, "broker_identity")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ready(v) for v in value]
    if isinstance(value, Decimal):
        # Do not use Decimal.normalize(): its rounding depends on ambient context.
        text = format(value, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) if value else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(_ready(value), sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _text(row: Mapping, key: str, limit: int = 500) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Missing or invalid {key}")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("SHA256 evidence hash required")
    return value


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        raise ValueError("Explicit business date required, not converted timestamp")
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _number(value: Any, *, scale: int = 10) -> Decimal:
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("Explicit exact decimal required")
    try:
        number = Decimal(value)
        if not number.is_finite() or number.adjusted() > 15:
            raise ValueError("Invalid financial precision")
        with localcontext(DECIMAL_CONTEXT):
            if number != number.quantize(Decimal(1).scaleb(-scale)):
                raise ValueError("Financial value exceeds schema precision")
        return number
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid financial value") from exc


def _native_source(row: Mapping) -> dict:
    result = {key: _text(row, key, limit) for key, limit in (
        ("source_table", 120), ("source_id", 120), ("source_key", 180), ("source_hash", 64))}
    if not re.fullmatch("[0-9a-f]{32}|[0-9a-f]{64}", result["source_hash"]):
        raise ValueError("Native source hash required")
    return result


def read_broker_cash_evidence(report_path: Path, report_sha256: str, *, account_id: str,
                             account_code: str,
                             fx_rate_to_cny: str | Decimal | Mapping[tuple[str, str], Any]) -> list[dict]:
    """Narrow XML adapter; FX is separate evidence, NOT inferred from fxRateToBase.

    A rate map is keyed by (business ISO date, native currency). Missing rates on
    unrelated rows stay unknown; the selected funding must have an explicit rate.
    """
    content = Path(report_path).read_bytes()
    if hashlib.sha256(content).hexdigest() != _sha(report_sha256):
        raise ValueError("Official report bytes changed")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("XML entity declarations are not funding evidence")
    root = ET.fromstring(content)
    rows: list[dict] = []
    parents = {child: parent for parent in root.iter() for child in parent}
    scopes: dict[tuple[str, str], ET.Element] = {}
    for node in root.iter("CashTransaction"):
        if node.get("type") != "Deposits/Withdrawals":
            continue
        chain: list[ET.Element] = []
        ancestor: ET.Element | None = node
        while ancestor is not None:
            chain.append(ancestor)
            ancestor = parents.get(ancestor)
        accounts = {element.get(field) for element in chain for field in ("accountId", "acctId")
                    if element.get(field)}
        if accounts and account_id not in accounts:
            continue
        statements = [element for element in chain if element.tag == "FlexStatement"]
        if accounts != {account_id} or len(statements) != 1:
            raise ValueError("Conflicting or unbound broker account scope")
        statement = statements[0]
        if not any(statement.get(field) == account_id for field in ("accountId", "acctId")):
            raise ValueError("Unbound broker statement account")
        start, end = (_xml_day(_text(statement.attrib, field)) for field in ("fromDate", "toDate"))
        scope_key = start, end
        if scope_key in scopes and scopes[scope_key] is not statement:
            raise ValueError("Duplicate broker statement scope")
        scopes[scope_key] = statement
        attrs = node.attrib
        report_day = _xml_day(_text(attrs, "reportDate"))
        if not start <= report_day <= end:
            raise ValueError("Broker report date outside statement scope")
        for element in chain:
            if element.get("fromDate") or element.get("toDate"):
                lower, upper = (_xml_day(_text(element.attrib, field)) for field in ("fromDate", "toDate"))
                if not lower <= report_day <= upper:
                    raise ValueError("Broker report date outside ancestor scope")
        identity = _text(attrs, "transactionID", 120)
        key = account_id + ":" + identity
        business_date = _xml_day(_text(attrs, "dateTime"))
        fx_rate = (fx_rate_to_cny.get((business_date, attrs["currency"]))
                   if isinstance(fx_rate_to_cny, Mapping) else fx_rate_to_cny)
        rows.append({"source_table": BROKER_SOURCE, "source_id": identity, "source_key": key,
                     "source_hash": make_source_hash(BROKER_SOURCE, {"source_key": key}),
                     "transaction_id": identity, "account_id": account_id, "account_code": account_code,
                     "trade_date": business_date, "settle_date": _xml_day(_text(attrs, "settleDate")),
                     "report_date": report_day, "date_time": attrs["dateTime"],
                     "statement_from_date": start, "statement_to_date": end,
                     "currency": attrs["currency"], "amount": attrs["amount"],
                     "fx_rate_to_cny": fx_rate, "report_sha256": report_sha256,
                     "type": attrs["type"]})
    return rows


def _xml_day(value: str) -> str:
    text = value.split(";")[0].split(" ")[0]
    return (datetime.strptime(text, "%Y%m%d").date() if re.fullmatch(r"[0-9]{8}", text)
            else _day(text)).isoformat()


def _identity_match(row: Mapping, evidence: Mapping, *, bank: bool) -> bool:
    return (row.get("source_hash") == evidence["source_hash"] or
            row.get("source_table") == evidence["source_table"] and (
                str(row.get("source_id")) == evidence["source_id"] or
                row.get("source_uid" if bank else "source_key") == evidence["source_key"]))


def _source_nodes(row: Mapping) -> list[Mapping]:
    """Inspect only established source wrappers, never arbitrary user-note objects."""
    pending, nodes, seen = [row], [], set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes.append(node)
        if len(nodes) > 128:
            raise ValueError("Source provenance bound exceeded")
        pending.extend(node[key] for key in SOURCE_WRAPPERS if isinstance(node.get(key), Mapping))
    return nodes


def _source_match(nodes: Sequence[Mapping], evidence: Mapping, *, bank: bool) -> bool:
    return any(_identity_match(node, evidence, bank=bank) or
               node.get("source_table") == evidence["source_table"] and any(
                   node.get(key) == evidence["source_key"] for key in ("source_key", "source_uid")) or
               not bank and any(str(node.get(key)) == evidence["transaction_id"]
                                for key in ("transactionID", "transaction_id")) for node in nodes)


def _verify_provenance(nodes: Sequence[Mapping], evidence: Mapping, *, bank: bool) -> None:
    native = False
    transaction_ids: set[str] = set()
    account_ids: set[str] = set()
    for node in nodes:
        if any(node.get(key) is not None and not isinstance(node[key], Mapping) for key in SOURCE_WRAPPERS):
            raise ValueError("Malformed source provenance wrapper")
        if node.get("source_table") == evidence["source_table"]:
            key_fields = ("source_key", "source_uid")
            keys = [node[field] for field in key_fields if node.get(field) is not None]
            if (str(node.get("source_id")) != evidence["source_id"] or not keys or
                    any(key != evidence["source_key"] for key in keys) or node.get("source_hash") != evidence["source_hash"]):
                raise ValueError("Native source identity/key/hash drift")
            native = True
        if node.get("currency") is not None and node["currency"] != evidence["currency"]:
            raise ValueError("Conflicting raw source currency identity")
        if not bank:
            transaction_ids.update(str(node[key]) for key in ("transactionID", "transaction_id") if node.get(key) is not None)
            account_ids.update(str(node[key]) for key in ("accountId", "acctId", "account_id", "source_account_id")
                               if node.get(key) is not None)
    if not bank:
        if transaction_ids - {evidence["transaction_id"]} or account_ids - {evidence["account_id"]}:
            raise ValueError("Conflicting raw broker transaction/account identity")
        native = native or (transaction_ids == {evidence["transaction_id"]} and account_ids == {evidence["account_id"]})
    if not native:
        raise ValueError("Unidentified economic fact is not proven native funding")


def _state(cashflows: Sequence[Mapping], trades: Sequence[Mapping]) -> dict:
    return {"cashflows": sorted((_ready(row) for row in cashflows), key=fingerprint),
            "trades": sorted((_ready(row) for row in trades), key=fingerprint)}


def plan_funding_repair(bank: Mapping, broker_transactions: Sequence[Mapping], mapping: Mapping, *,
                        cashflows: Sequence[Mapping] = (), trades: Sequence[Mapping] = (),
                        broker_business_date: date | str | None = None,
                        database_scope: Mapping | None = None) -> dict:
    """Build a private approval request. Supplied rows must cover BOTH full fact tables.

    Bank fields: native source_table/id/key/hash, evidence_sha256, account_code,
    masked_account, event_date, posting_date (nullable), amount, currency,
    counterparty, fx_rate_to_cny. Untrusted/manual fields are never inferred from
    shared references. Cross-day settlement needs an explicit broker_business_date.
    """
    bank = deepcopy(dict(bank))
    mapping = dict(mapping)
    _native_source(bank)
    _sha(bank.get("evidence_sha256"))
    for field in ("bank_account_code", "masked_account", "broker_account_code", "broker_account_id", "counterparty"):
        _text(mapping, field, 80 if "account" in field else 500)
    if (bank.get("account_code") != mapping["bank_account_code"] or
            bank.get("masked_account") != mapping["masked_account"] or
            _text(bank, "counterparty").casefold() != mapping["counterparty"].casefold() or
            mapping["bank_account_code"] == mapping["broker_account_code"]):
        raise ValueError("Explicit bank/counterparty/broker account mapping required")
    amount = _number(bank.get("amount"), scale=4)
    deposit_amount = amount.copy_negate()
    currency = _text(bank, "currency", 10)
    if amount >= 0 or not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError("Bank funding must be negative native currency")
    event_date = _day(bank.get("event_date"))
    posting_date = _day(bank["posting_date"]) if bank.get("posting_date") else None
    broker_date = _day(broker_business_date) if broker_business_date is not None else event_date
    candidates = []
    identities = set()
    for raw in broker_transactions:
        if raw.get("account_id") != mapping["broker_account_id"]:
            continue
        identity = _text(raw, "transaction_id", 120)
        if identity in identities:
            raise ValueError("Duplicate native broker transaction identity")
        identities.add(identity)
        if raw.get("currency") == currency and _day(raw.get("trade_date")) == broker_date:
            if _number(raw.get("amount")) == deposit_amount:
                candidates.append(dict(raw))
    if len(candidates) != 1:
        raise ValueError("Unique authoritative broker deposit required")
    broker = candidates[0]
    _native_source(broker)
    _sha(broker.get("report_sha256"))
    native_key = mapping["broker_account_id"] + ":" + _text(broker, "transaction_id", 120)
    if (broker.get("account_code") != mapping["broker_account_code"] or
            broker.get("type") != "Deposits/Withdrawals" or broker["source_table"] != BROKER_SOURCE or
            broker["source_id"] != broker["transaction_id"] or broker["source_key"] != native_key or
            broker["source_hash"] != make_source_hash(BROKER_SOURCE, {"source_key": native_key})):
        raise ValueError("Broker native stable identity or deposit semantics conflict")
    report_date, settle_date = _day(broker.get("report_date")), _day(broker.get("settle_date"))
    if not _day(broker.get("statement_from_date")) <= report_date <= _day(broker.get("statement_to_date")):
        raise ValueError("Broker report date outside statement scope")
    if report_date < broker_date or report_date < settle_date:
        raise ValueError("Broker report precedes deposit business/settlement date")
    bank_fx, broker_fx = (_number(row.get("fx_rate_to_cny")) for row in (bank, broker))
    if (min(bank_fx, broker_fx) <= 0 or max(bank_fx, broker_fx) >= Decimal("1e10") or
            currency == "CNY" and (bank_fx != 1 or broker_fx != 1)):
        raise ValueError("Explicit native-to-CNY FX required")
    with localcontext(DECIMAL_CONTEXT):
        bank_cny = (amount * bank_fx).quantize(Decimal("0.0001"))
        trade_cny = (deposit_amount * broker_fx).quantize(Decimal("0.0000000001"))
        if bank_cny.copy_abs() >= Decimal("1e16") or trade_cny >= Decimal("1e18"):
            raise ValueError("CNY conversion exceeds fact schema precision")
    bank_rows = []
    for row in cashflows:
        nodes = _source_nodes(row)
        economic = (row.get("account_code") == bank["account_code"] and str(row.get("event_date")) == str(event_date)
                    and row.get("currency") == currency and _number(row.get("amount")) == amount)
        if _source_match(nodes, bank, bank=True) or economic:
            _verify_provenance(nodes, bank, bank=True)
            bank_rows.append(dict(row))
    if len(bank_rows) > 1:
        raise ValueError("Duplicate or ambiguous bank facts")
    bank_before = bank_rows[0] if bank_rows else None
    bank_id = bank_before["cashflow_id"] if bank_before else None
    trade_rows = []
    for row in trades:
        nodes = _source_nodes(row)
        raw = row.get("raw_json") or {}
        audit = raw.get(AUDIT_KEY) or {}
        audit_identity = audit.get("broker_identity") == {"account_id": broker["account_id"],
                                                    "transaction_id": broker["transaction_id"]}
        economic = (row.get("account_code") == broker["account_code"] and row.get("currency") == currency
                    and str(row.get("trade_date")) in {str(broker_date), str(report_date)}
                    and (row.get("trade_type") == "deposit" or row.get("side") == "deposit"
                         or not row.get("trade_type") and not row.get("side"))
                    and any(row.get(k) is not None and _number(row[k]) == deposit_amount
                            for k in ("net_cash", "gross_amount")))
        if (_source_match(nodes, broker, bank=False) or audit_identity or economic or
                bank_id is not None and row.get("cashflow_id") == bank_id):
            _verify_provenance(nodes, broker, bank=False)
            trade_rows.append(dict(row))
    if len(trade_rows) > 1:
        raise ValueError("Duplicate or ambiguous broker facts")
    trade_before = trade_rows[0] if trade_rows else None
    for before_row, fields in ((bank_before, {"event_date": event_date, "posting_date": posting_date,
                                     "account_code": bank["account_code"], "amount": amount, "currency": currency,
                                     "fx_rate_to_cny": bank_fx, "amount_cny": bank_cny}),
                        (trade_before, {"trade_date": broker_date, "settle_date": settle_date,
                                        "account_code": broker["account_code"], "currency": currency, "net_cash": deposit_amount,
                                        "gross_amount": deposit_amount, "trade_type": "deposit", "side": "deposit",
                                        "fx_rate_to_cny": broker_fx, "net_cash_cny": trade_cny,
                                        "affects_position": False, "affects_cost_basis": False,
                                        "affects_realized_pnl": False, "affects_cash": True})):
        if before_row is not None:
            if any(_ready(before_row.get(k)) != _ready(v) for k, v in fields.items()):
                raise ValueError("Existing fact conflicts with native evidence; not a missing-side repair")
            if str(before_row.get("review_status") or "").strip().lower() in {"rejected", "cancelled", "canceled", "void"}:
                raise ValueError("Inactive or rejected fact")
    if bank_before and bank_before.get("counter_account_code") not in (None, "", broker["account_code"]):
        raise ValueError("Bank fact already belongs to another counter account")
    if trade_before:
        if trade_before.get("cashflow_id") not in (None, bank_id):
            raise ValueError("Broker deposit already linked elsewhere")
        if any(_number(trade_before.get(k, 0) or 0) != 0 for k in (
                "quantity", "price", "fee_amount", "tax_amount", "realized_pnl", "realized_pnl_cny")):
            raise ValueError("Funding is not a position, cost, fee or income correction")
    for before_row in (bank_before, trade_before):
        if before_row and AUDIT_KEY in (before_row.get("raw_json") or {}):
            raise ValueError("Previously audited funding requires exact-request replay")
    group = (bank_before or {}).get("transfer_group_id") or "funding:" + fingerprint(native_key)
    bank_input = {**bank, "id": bank["source_id"], "source_uid": bank["source_key"],
                  "transaction_date": event_date, "posting_date": posting_date,
                  "amount": amount, "amount_cny": bank_cny, "fx_rate_to_cny": bank_fx}
    with localcontext(DECIMAL_CONTEXT):
        bank_fact = build_cashflow_fact(bank_input, {"match_type": "investment_funding", "match_key": group,
                                        "from_account_code": bank["account_code"],
                                        "to_account_code": broker["account_code"], "confidence": "1"})
    bank_fact.update({k: bank[k] for k in ("source_table", "source_id", "source_hash")})
    bank_fact["source_uid"] = bank["source_key"]
    bank_fact["review_status"] = "reviewed"
    with localcontext(DECIMAL_CONTEXT):
        trade_fact = build_investment_trade_fact({**broker, "trade_date": broker_date,
            "settle_date": settle_date, "broker": "IBKR", "side": "deposit", "quantity": None,
            "gross_amount": deposit_amount, "net_cash": deposit_amount, "fx_rate_to_cny": broker_fx})
    trade_fact["net_cash_cny"] = trade_cny
    trade_fact["review_status"] = "reviewed"
    bank_patch = {key: bank_fact[key] for key in ("counter_account_code", "cashflow_type", "affects_spending",
                  "affects_nav", "affects_investment_capital", "is_internal_transfer", "transfer_group_id")}
    protected = _state([row for row in cashflows if row.get("cashflow_id") != bank_id],
                       [row for row in trades if not trade_before or row["trade_id"] != trade_before["trade_id"]])
    payload = {"version": 1, "purpose": PURPOSE, "production_write_supported": False,
               "database_scope": dict(database_scope or {}), "bank_evidence": bank, "broker_evidence": broker,
               "account_mapping": mapping, "broker_business_date": broker_date,
               "before_sha256": fingerprint(_state(cashflows, trades)), "protected_sha256": fingerprint(protected),
               "bank_before": bank_before, "trade_before": trade_before, "bank_fact": bank_fact,
               "trade_fact": trade_fact, "bank_patch": bank_patch, "transfer_group_id": group, "risks": RISKS}
    payload = _ready(payload)
    return {"status": "approval_required", "mode": "PREVIEW", "executable": False,
            "production_write_supported": False, "request": {**payload, "request_sha256": fingerprint(payload)}}


def validate_approval(request: Mapping, approval: Mapping) -> str:
    if set(approval) != {"purpose", "request_sha256", "approved_by", "reference", "issued_at",
                         "expires_at", "accepted_risks"}:
        raise ValueError("Exact attributed approval required")
    if (approval["purpose"] != PURPOSE or approval["request_sha256"] != request.get("request_sha256") or
            approval["accepted_risks"] != request.get("risks")):
        raise ValueError("Approval does not bind this request and its residual risks")
    for key in ("approved_by", "reference"):
        _text(approval, key)
    issued, expires = (datetime.fromisoformat(approval[key]) for key in ("issued_at", "expires_at"))
    if (issued.tzinfo is None or expires.tzinfo is None or not issued <= utc_now() < expires or
            not timedelta(0) < expires - issued <= timedelta(hours=24)):
        raise ValueError("Approval expired or outside bounded validity")
    payload = {k: v for k, v in request.items() if k != "request_sha256"}
    if (request.get("purpose") != PURPOSE or request.get("production_write_supported") is not False or
            fingerprint(payload) != request.get("request_sha256")):
        raise ValueError("Request hash or execution purpose changed")
    return fingerprint(approval)


def _idle(conn) -> None:
    if conn.autocommit or conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        raise ValueError("Fresh non-autocommit connection required")


def _isolated(conn, schema: str) -> None:
    _idle(conn)
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    expected = parse_dsn(dsn) if dsn else {}
    actual = conn.info.dsn_parameters
    if (not expected.get("host") or not expected.get("user") or not expected.get("dbname") or
            any(actual.get(k) != expected.get(k, default) for k, default in
                (("host", ""), ("port", "5432"), ("user", ""), ("dbname", ""))) or
            not re.fullmatch(r"pams_ibkr_funding_test_[0-9a-f]{32}", schema)):
        raise ValueError("Explicit disposable funding test schema required; production apply forbidden")
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            cur.execute("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=%s", (schema,))
            row = cur.fetchone()
            if not row or row[0] != MARKER:
                raise ValueError("Disposable schema marker required")
    finally:
        conn.rollback()


def _inputs(cur, schema: str) -> tuple[list[dict], list[dict], dict]:
    cur.execute("SELECT * FROM pams_cashflow_fact")
    cashflows = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM pams_investment_trade_fact")
    trades = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT current_database() AS database, session_user AS operator, "
                "inet_server_addr()::text AS server, inet_server_port() AS port, "
                "nspname AS schema, oid AS schema_oid FROM pg_namespace WHERE nspname=%s", (schema,))
    return cashflows, trades, dict(cur.fetchone())


def _verify_files(bank, broker_rows, mapping, receipt_path, report_path) -> None:
    if hashlib.sha256(Path(receipt_path).read_bytes()).hexdigest() != _sha(bank.get("evidence_sha256")):
        raise ValueError("Bank receipt bytes changed")
    if not broker_rows:
        raise ValueError("Authoritative broker evidence missing")
    hashes = {row["report_sha256"] for row in broker_rows}
    if len(hashes) != 1:
        raise ValueError("Single authoritative report required")
    actual = read_broker_cash_evidence(report_path, next(iter(hashes)), account_id=mapping["broker_account_id"],
                    account_code=mapping["broker_account_code"], fx_rate_to_cny={})
    def native(rows):
        return sorted(({k: v for k, v in row.items() if k != "fx_rate_to_cny"} for row in rows), key=fingerprint)
    if fingerprint(native(actual)) != fingerprint(native(broker_rows)):
        raise ValueError("Broker evidence differs from complete native report deposit set")


def _audit(request, approval) -> dict:
    broker = request["broker_evidence"]
    return {"request": request, "approval": approval,
            "broker_identity": {"account_id": broker["account_id"], "transaction_id": broker["transaction_id"]},
            "transfer_group_id": request["transfer_group_id"]}


def _desired(request, approval, bank_id) -> tuple[dict, dict]:
    bank = deepcopy(request["bank_before"] or request["bank_fact"])
    trade = deepcopy(request["trade_before"] or request["trade_fact"])
    bank.update(request["bank_patch"])
    trade["cashflow_id"] = bank_id
    for row in (bank, trade):
        row["raw_json"] = {**(row.get("raw_json") or {}), AUDIT_KEY: _audit(request, approval)}
    return bank, trade


def _verify_after(request, approval, cashflows, trades) -> tuple[int, int]:
    bank_rows = [row for row in cashflows if (row.get("raw_json") or {}).get(AUDIT_KEY) == _audit(request, approval)]
    trade_rows = [row for row in trades if (row.get("raw_json") or {}).get(AUDIT_KEY) == _audit(request, approval)]
    if len(bank_rows) != 1 or len(trade_rows) != 1:
        raise ValueError("Applied pair or matching approval audit missing/duplicated")
    bank, trade = bank_rows[0], trade_rows[0]
    for current, desired in zip((bank, trade), _desired(request, approval, bank["cashflow_id"])):
        if any(_ready(current.get(k)) != _ready(v) for k, v in desired.items()):
            raise ValueError("Applied fact differs from approved projection")
    protected = _state([r for r in cashflows if r["cashflow_id"] != bank["cashflow_id"]],
                       [r for r in trades if r["trade_id"] != trade["trade_id"]])
    if fingerprint(protected) != request["protected_sha256"]:
        raise ValueError("Unrelated facts drifted")
    return bank["cashflow_id"], trade["trade_id"]


def reconcile_funding(conn, bank: Mapping, broker_transactions: Sequence[Mapping], mapping: Mapping, *,
                      receipt_path: Path, report_path: Path, broker_business_date=None, mode: str = "PREVIEW",
                      request: Mapping | None = None, approval: Mapping | None = None, schema: str = "public") -> dict:
    """Own a fresh transaction. Never invoke on production in APPLY_ISOLATED mode."""
    _idle(conn)
    if mode not in {"PREVIEW", "APPLY_ISOLATED"}:
        raise ValueError("Only PREVIEW or APPLY_ISOLATED supported")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("Invalid schema scope")
    if mode == "PREVIEW" and (request is not None or approval is not None):
        raise ValueError("Preview accepts evidence only, not implicit execution approval")
    if mode == "APPLY_ISOLATED":
        if request is None or approval is None:
            raise ValueError("Explicit request and approval required")
        validate_approval(request, approval)
        _isolated(conn, schema)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ" + (" READ ONLY" if mode == "PREVIEW" else ""))
            cur.execute(sql.SQL("SET LOCAL search_path = pg_catalog, {}").format(sql.Identifier(schema)))
            cur.execute("SET LOCAL lock_timeout = '2000ms'")
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            if mode != "PREVIEW":
                cur.execute("LOCK TABLE pams_cashflow_fact, pams_investment_trade_fact "
                            "IN SHARE ROW EXCLUSIVE MODE")
            _verify_files(bank, broker_transactions, mapping, receipt_path, report_path)
            cashflows, trades, scope = _inputs(cur, schema)
            if mode == "PREVIEW":
                return plan_funding_repair(bank, broker_transactions, mapping, cashflows=cashflows, trades=trades,
                                          broker_business_date=broker_business_date, database_scope=scope)
            assert request is not None and approval is not None
            if request["database_scope"] != scope:
                raise ValueError("Approved database scope changed")
            if fingerprint(_state(cashflows, trades)) != request["before_sha256"]:
                # Retry verifies the complete resulting pair and all protected facts, never re-inserts.
                if (fingerprint(bank) != fingerprint(request["bank_evidence"]) or
                        fingerprint(mapping) != fingerprint(request["account_mapping"]) or
                        not any(fingerprint(row) == fingerprint(request["broker_evidence"]) for row in broker_transactions)
                        or str(_day(broker_business_date or bank["event_date"])) != request["broker_business_date"]):
                    raise ValueError("Retry evidence changed")
                applied_bank_id, applied_trade_id = _verify_after(request, approval, cashflows, trades)
                validate_approval(request, approval)
                return {"status": "already_applied_in_isolation", "inserted": 0,
                        "cashflow_id": applied_bank_id, "trade_id": applied_trade_id, "production_write_supported": False}
            current = plan_funding_repair(bank, broker_transactions, mapping, cashflows=cashflows, trades=trades,
                                         broker_business_date=broker_business_date, database_scope=scope)["request"]
            if fingerprint(current) != fingerprint(request):
                raise ValueError("Approved before-state/evidence/projection changed")
            validate_approval(request, approval)
            bank_before, trade_before = request["bank_before"], request["trade_before"]
            bank_id = bank_before["cashflow_id"] if bank_before else None
            bank_desired, _ = _desired(request, approval, bank_id)
            if bank_before:
                cur.execute("""UPDATE pams_cashflow_fact SET counter_account_code=%s, cashflow_type=%s,
                    affects_spending=%s, affects_nav=%s, affects_investment_capital=%s, is_internal_transfer=%s,
                    transfer_group_id=%s, raw_json=%s WHERE cashflow_id=%s""",
                    tuple(bank_desired[k] for k in ("counter_account_code", "cashflow_type", "affects_spending",
                          "affects_nav", "affects_investment_capital", "is_internal_transfer", "transfer_group_id")) +
                    (Json(bank_desired["raw_json"]), bank_id))
                if cur.rowcount != 1:
                    raise ValueError("Bank update lost")
            else:
                if insert_cashflow_facts(cur, [bank_desired]) != 1:
                    raise ValueError("Bank fact insert conflict")
                cur.execute("SELECT cashflow_id FROM pams_cashflow_fact WHERE source_table=%s AND source_id=%s",
                            (bank_desired["source_table"], bank_desired["source_id"]))
                bank_id = cur.fetchone()["cashflow_id"]
            _, trade_desired = _desired(request, approval, bank_id)
            if trade_before:
                cur.execute("UPDATE pams_investment_trade_fact SET cashflow_id=%s, raw_json=%s WHERE trade_id=%s",
                            (bank_id, Json(trade_desired["raw_json"]), trade_before["trade_id"]))
                if cur.rowcount != 1:
                    raise ValueError("Broker link update lost")
            elif insert_investment_trade_facts(cur, [trade_desired]) != 1:
                raise ValueError("Broker fact insert conflict")
            cashflows, trades, _ = _inputs(cur, schema)
            bank_id, trade_id = _verify_after(request, approval, cashflows, trades)
            _verify_files(bank, broker_transactions, mapping, receipt_path, report_path)
            validate_approval(request, approval)
        conn.commit()
        return {"status": "applied_in_isolation", "inserted": int(bank_before is None) + int(trade_before is None),
                "cashflow_id": bank_id, "trade_id": trade_id, "production_write_supported": False}
    finally:
        conn.rollback()
