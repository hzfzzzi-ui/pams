"""Opt-in, post-cutoff IBKR CashTransaction facts. No balance mutations or commits."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
from typing import Any, Mapping

import psycopg2.extras

from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts
from domain.imports import isolation

SOURCE_TABLE = "ibkr_flex_cash_transaction"


def configured_cutoff() -> date | None:
    value = os.environ.get("PAMS_IBKR_CASH_EVENTS_AFTER", "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("PAMS_IBKR_CASH_EVENTS_AFTER must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _business_date(value: str) -> date:
    # Never fall back to reportDate, settleDate, the report end, or today's date.
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m%d;%H%M%S", "%Y-%m-%d;%H:%M:%S",
                "%Y%m%d;%H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("missing_or_invalid_business_date")


def normalize_cash_event(
    envelope: Mapping[str, Any], *, account_code: str, cutoff: date, statement_as_of: date,
) -> dict[str, Any] | None:
    raw = dict(envelope["raw"])
    if envelope.get("scope_error"):
        raise ValueError("conflicting_cash_event_scope")
    values = {key.lower(): str(value).strip() for key, value in raw.items()}
    if len(values) != len(raw):
        raise ValueError("ambiguous_attribute_names")
    event_date = _business_date(values.get("datetime", ""))
    if event_date <= cutoff:
        return None
    if event_date > statement_as_of:
        raise ValueError("future_business_date")
    accounts = {value for value in (values.get("accountid"), values.get("acctid"),
                                    envelope.get("statement_account_id")) if value}
    if len(accounts) != 1:
        raise ValueError("missing_or_conflicting_source_account")
    source_account_id = str(next(iter(accounts)))
    transaction_id = values.get("transactionid", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", transaction_id) or transaction_id.lower() in {"0", "null", "none", "unknown"}:
        raise ValueError("missing_or_invalid_transaction_id")
    if values.get("levelofdetail", "DETAIL").upper() != "DETAIL":
        raise ValueError("not_a_detail_event")
    currency = values.get("currency", "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("missing_or_invalid_currency")
    text = values.get("amount", "")
    if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", text):
        raise ValueError("missing_or_invalid_amount")
    amount = Decimal(text)
    if not amount.is_finite() or not amount or abs(amount) >= Decimal("1e18") or amount != amount.quantize(Decimal("0.0000000001")):
        raise ValueError("zero_or_unrepresentable_amount")
    kind = values.get("type", "").lower()
    if kind == "deposits & withdrawals":
        side = "deposit" if amount > 0 else "withdraw"
    else:
        types = {
            "dividends": ("dividend", 1), "payment in lieu of dividends": ("dividend", 1),
            "withholding tax": ("tax", -1), "broker interest received": ("interest", 1),
            "broker interest paid": ("interest", -1), "bond interest received": ("interest", 1),
            "other fees": ("fee", -1), "advisor fees": ("fee", -1),
        }
        if kind not in types:
            raise ValueError("unsupported_cash_transaction_type")
        side, sign = types[kind]
        if (amount > 0) != (sign > 0):
            raise ValueError("type_amount_sign_conflict")
    settle_date = _business_date(values["settledate"]) if values.get("settledate") else None
    symbol = values.get("symbol") or f"CASH_{currency}"
    name = values.get("description") or f"Cash {currency}"
    if len(symbol) > 120 or len(name) > 240:
        raise ValueError("unrepresentable_symbol_or_description")
    identity = json.dumps([account_code, source_account_id, transaction_id], separators=(",", ":"))
    source_key = "ibkr_cash:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    # Full official attributes are immutable evidence, not a reconstruction from DB row IDs.
    fingerprint = hashlib.sha256(json.dumps(
        {"account": source_account_id, "raw": raw}, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "source_table": SOURCE_TABLE, "source_id": source_key, "source_key": source_key,
        "source_account_id": source_account_id, "transaction_id": transaction_id,
        "trade_date": event_date, "settle_date": settle_date, "account_code": account_code,
        "broker": "ibkr", "currency": currency, "side": side,
        "symbol": symbol, "name": name,
        "gross_amount": abs(amount), "net_cash": amount,
        "cash_event_hash": fingerprint, "raw_json": raw,
        **({"source_evidence": envelope["source_evidence"]} if "source_evidence" in envelope else {}),
    }


def _funding_match(cur, event: Mapping[str, Any]) -> tuple[int | None, str]:
    if event["side"] not in {"deposit", "withdraw"}:
        return None, "not_funding"
    # A matching amount/date alone is not identity evidence. Reuse the existing confirmation envelope.
    cur.execute("""
        SELECT cashflow_id FROM pams_cashflow_fact cf
        WHERE counter_account_code = %(account_code)s AND account_code <> %(account_code)s
          AND currency = %(currency)s AND amount = -%(net_cash)s
          AND event_date BETWEEN %(trade_date)s::date - INTERVAL '7 days' AND %(trade_date)s::date
          AND cashflow_type = %(funding_type)s
          AND review_status = 'confirmed'
          AND raw_json #>> '{ibkr_flex_confirmation,raw,transactionID}' = %(transaction_id)s
          AND raw_json #>> '{ibkr_flex_confirmation,raw,accountId}' = %(source_account_id)s
          AND NOT EXISTS (SELECT 1 FROM pams_investment_trade_fact f
                          WHERE f.cashflow_id = cf.cashflow_id)
        FOR UPDATE OF cf
    """, {**event, "funding_type": "investment_funding" if event["side"] == "deposit" else "investment_withdrawal"})
    candidates = cur.fetchall()
    if len(candidates) != 1:
        return None, "bank_match_pending"
    return candidates[0]["cashflow_id"], "bank_match_confirmed"


def dated_fx_rate(cur, currency: str, event_date: date) -> Decimal | None:
    if currency == "CNY":
        return Decimal("1")
    cur.execute("""SELECT rate FROM pams_fx_rates_daily WHERE base_currency = 'CNY'
        AND quote_currency = %s AND as_of = %s
        AND NULLIF(BTRIM(source),'') IS NOT NULL""", (currency, event_date))
    rates = cur.fetchall()
    rate = Decimal(str(rates[0]["rate"])) if len(rates) == 1 and rates[0]["rate"] is not None else Decimal("NaN")
    if not rate.is_finite() or rate <= 0 or rate >= Decimal("1e10") or rate != rate.quantize(Decimal("0.0000000001")):
        return None
    return rate


def import_cash_events(
    conn, envelopes: list[dict[str, Any]], *, account_code: str, cutoff: date, statement_as_of: date,
    defer_source_link: bool = False,
) -> dict[str, Any]:
    if not defer_source_link:
        raise ValueError('IBKR cash events require same-transaction original report publication')
    result: dict[str, Any] = {"cutoff": cutoff.isoformat(), "inserted_count": 0,
                              "duplicate_count": 0, "historical_count": 0, "pending": []}
    deferred: list[dict[str, Any]] = []
    events: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    for envelope in envelopes:
        try:
            event = normalize_cash_event(envelope, account_code=account_code, cutoff=cutoff,
                                         statement_as_of=statement_as_of)
        except (ValueError, InvalidOperation) as exc:
            result["pending"].append({"transaction_id": envelope["raw"].get("transactionID"),
                                       "reason": str(exc), "retryable": False, "state": "waiting_evidence",
                                       "fact_imported": False, "blocking": True})
            continue
        if event is None:
            result["historical_count"] += 1
            continue
        previous = events.get(event["source_key"])
        if event["source_key"] in conflicts:
            continue
        if previous is not None:
            if previous != event:
                conflicts.add(event["source_key"])
                del events[event["source_key"]]
                result["pending"].append({"transaction_id": event["transaction_id"],
                    "reason": "conflicting_cash_transaction", "retryable": False, "state": "waiting_evidence",
                    "fact_imported": False, "blocking": True})
                continue
            result["duplicate_count"] += 1
        events[event["source_key"]] = event
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        isolated=isolation.source_enabled(cur)
        if not isolated or cur.connection.autocommit:
            raise ValueError('deferred IBKR cash source requires isolated transaction')
        for event in events.values():
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"{SOURCE_TABLE}:{event['source_key']}",))
            cur.execute("""SELECT raw_json,trade_id,source_item_id FROM pams_investment_trade_fact
                WHERE source_table = %s AND (source_key = %s OR source_id = %s) FOR UPDATE""",
                        (SOURCE_TABLE, event["source_key"], event["source_id"]))
            existing = cur.fetchall()
            if existing:
                if len(existing) != 1 or existing[0]["raw_json"].get("source_row", {}).get("cash_event_hash") != event["cash_event_hash"]:
                    result["pending"].append({"transaction_id": event["transaction_id"],
                        "reason": "conflicting_cash_transaction", "retryable": False, "state": "waiting_evidence",
                        "fact_imported": False, "blocking": True})
                    continue
                result["duplicate_count"] += 1
                deferred.append({'event':event,'fact_id':existing[0]['trade_id'],'created':False})
                if event["side"] in {"deposit", "withdraw"} and not existing[0]["raw_json"].get("source_row", {}).get("cashflow_id"):
                    result["pending"].append({"transaction_id": event["transaction_id"],
                        "reason": "bank_match_pending", "retryable": False, "state": "waiting_evidence",
                        "fact_imported": True, "blocking": False})
                continue
            rate = dated_fx_rate(cur, event["currency"], event["trade_date"])
            if rate is None:
                result["pending"].append({"transaction_id": event["transaction_id"],
                    "reason": "missing_or_invalid_dated_fx", "retryable": True, "state": "retryable",
                    "fact_imported": False, "blocking": True})
                continue
            event["fx_rate_to_cny"] = rate
            cashflow_id, match_state = _funding_match(cur, event)
            event["cashflow_id"] = cashflow_id
            if match_state == "bank_match_pending":
                result["pending"].append({"transaction_id": event["transaction_id"], "reason": match_state,
                                           "retryable": False, "state": "waiting_evidence",
                                           "fact_imported": True, "blocking": False})
            fact = build_investment_trade_fact(event)
            if event["side"] == "fee":
                fact["fee_amount"] = abs(event["net_cash"])
                fact["fee_currency"] = event["currency"]
            elif event["side"] == "tax":
                fact["tax_amount"] = abs(event["net_cash"])
            inserted = insert_investment_trade_facts(cur, [fact], strict_source=True)
            if inserted:
                cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_table=%s AND source_key=%s",(SOURCE_TABLE,event['source_key']))
                deferred.append({'event':event,'fact_id':cur.fetchone()['trade_id'],'created':True})
            result["inserted_count"] += inserted
    result["retryable_count"] = sum(item["retryable"] for item in result["pending"])
    result["waiting_evidence_count"] = len(result["pending"]) - result["retryable_count"]
    result["blocking_pending_count"] = sum(item["blocking"] for item in result["pending"])
    result["bank_match_pending_count"] = sum(item["reason"] == "bank_match_pending" for item in result["pending"])
    result['_deferred_source_events']=deferred
    return result
