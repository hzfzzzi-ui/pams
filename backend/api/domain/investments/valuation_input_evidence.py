"""Isolated candidate for immutable valuation versions and recorded inputs.

No existing production path calls this module. Captured inputs are supplied by
the caller and are not independently authenticated against their source.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re
from typing import Any

from psycopg2.extras import Json


KINDS = {"holding", "trade", "quote", "fx", "source_item"}
QUALITIES = {"unknown", "pending", "estimated", "confirmed"}
FIELDS = {"kind", "role", "source_ref", "source_version", "business_date", "observed_at", "quality", "snapshot"}


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("observed timestamp requires timezone")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _normalize(item) for key, item in value.items()}
    raise ValueError("input must use stable JSON values, not floats")


def _canonical(value: Any) -> str:
    return json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _known_key(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, date) and not isinstance(value, datetime))


def normalize_inputs(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    evidence_keys: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) - FIELDS or item.get("kind") not in KINDS:
            raise ValueError("invalid input fields or kind")
        source_ref = item.get("source_ref")
        snapshot = item.get("snapshot")
        if source_ref is not None:
            if (not isinstance(source_ref, dict) or not isinstance(source_ref.get("table"), str)
                    or not source_ref["table"].strip() or not isinstance(source_ref.get("key"), dict)
                    or not source_ref["key"] or not all(_known_key(value) for value in source_ref["key"].values())):
                raise ValueError("source_ref requires a source table and nonempty known key")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object or unknown")
        quality = item.get("quality") or "unknown"
        if quality not in QUALITIES:
            raise ValueError("invalid quality")
        observed_at = item.get("observed_at")
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed_at is not None and (not isinstance(observed_at, datetime) or observed_at.utcoffset() is None):
            raise ValueError("observed timestamp requires timezone")
        day = item.get("business_date")
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if day is not None and (not isinstance(day, date) or isinstance(day, datetime)):
            raise ValueError("business_date must be a date")
        version = item.get("source_version")
        if version is not None and (not isinstance(version, str) or not version.strip()):
            raise ValueError("source_version must be nonempty or unknown")
        role = item.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ValueError("role must be nonempty or unknown")
        row = _normalize({"kind": item["kind"], "role": role, "source_ref": source_ref,
                          "source_version": version, "business_date": day,
                          "observed_at": observed_at, "quality": quality, "snapshot": snapshot})
        if source_ref is not None:
            identity = _canonical([row["kind"], row["source_ref"]])
            if identity in identities:
                raise ValueError("duplicate input identity")
            identities.add(identity)
        evidence_key = _canonical(row)
        if evidence_key in evidence_keys:
            raise ValueError("duplicate evidence")
        evidence_keys.add(evidence_key)
        normalized.append(row)
    return sorted(normalized, key=_canonical)


def recorded_input_freshness(recorded: list[dict[str, Any]], current: list[dict[str, Any]]) -> str:
    """Compare supplied snapshots only; never assert the source set is complete."""
    before, after = normalize_inputs(recorded), normalize_inputs(current)
    if not before or not after or any(row["source_ref"] is None for row in before + after):
        return "unknown"
    key = lambda row: _canonical([row["kind"], row["source_ref"]])
    old, new = {key(row): row for row in before}, {key(row): row for row in after}
    for identity in old.keys() & new.keys():
        left, right = old[identity], new[identity]
        for field in ("role", "source_version", "business_date", "observed_at", "snapshot"):
            if field == "snapshot" and (not left[field] or not right[field]):
                continue
            if left[field] is not None and right[field] is not None and left[field] != right[field]:
                return "changed"
        if left["quality"] != "unknown" and right["quality"] != "unknown" and left["quality"] != right["quality"]:
            return "changed"
    if old.keys() != new.keys():
        return "unknown"
    if any(old[identity]["role"] != new[identity]["role"] for identity in old):
        return "unknown"
    for row in before + after:
        if row["kind"] in {"holding", "trade"}:
            ref = row["source_ref"] or {}
            if not (ref.get("account_code") or (ref.get("key") or {}).get("account_code")
                    or (row["snapshot"] or {}).get("account_code")):
                return "unknown"
    if any(not row["snapshot"] or row["business_date"] is None or row["observed_at"] is None
           or row["quality"] in {"unknown", "pending"} for row in before + after):
        return "unknown"
    return "unchanged_for_recorded_inputs"


def record_result_version(cur: Any, *, result_kind: str, scope_key: str, business_date: date,
                          base_currency: str, calculation_version: str, result_hash: str,
                          inputs: list[dict[str, Any]], account_code: str | None = None) -> str:
    """Append one version atomically; caller owns commit/rollback and source proof."""
    if cur.connection.autocommit:
        raise ValueError("transaction required")
    if result_kind not in {"holding", "portfolio"} or not all(
        isinstance(value, str) and value.strip() for value in (scope_key, base_currency, calculation_version)
    ) or not isinstance(business_date, date) or isinstance(business_date, datetime):
        raise ValueError("invalid result identity")
    if not isinstance(result_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", result_hash):
        raise ValueError("caller must provide a result hash")
    if account_code is not None and (not isinstance(account_code, str) or not account_code.strip()):
        raise ValueError("account_code must be nonempty or unknown")
    if result_kind == "holding" and not account_code:
        raise ValueError("holding result requires an account")
    rows = normalize_inputs(inputs)
    if result_kind == "holding":
        for row in rows:
            if row["kind"] not in {"holding", "trade"}:
                continue
            ref = row["source_ref"] or {}
            for data in (ref, ref.get("key"), row["snapshot"]):
                if isinstance(data, dict) and data.get("account_code") not in (None, account_code):
                    raise ValueError("holding or trade input account conflicts with result")
    identity = {"result_kind": result_kind, "scope_key": scope_key, "business_date": business_date,
                "account_code": account_code, "base_currency": base_currency, "calculation_version": calculation_version,
                "result_hash": result_hash, "inputs": rows}
    version_key = _hash(identity)
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (version_key,))
    cur.execute("SELECT 1 FROM public.pams_valuation_result_version WHERE version_key = %s", (version_key,))
    if cur.fetchone():
        return version_key
    for row in rows:
        cur.execute("""INSERT INTO public.pams_valuation_input_evidence
            (version_key, input_hash, input_kind, evidence) VALUES (%s, %s, %s, %s)""",
                    (version_key, _hash(row), row["kind"], Json(row)))
    cur.execute("""INSERT INTO public.pams_valuation_result_version
        (version_key, result_kind, scope_key, account_code, business_date, base_currency, calculation_version, result_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (version_key, result_kind, scope_key, account_code, business_date, base_currency, calculation_version, result_hash))
    return version_key


def verify_recorded_version(cur: Any, version_key: str) -> bool:
    """Verify internal fingerprints; this does not authenticate external sources."""
    cur.execute("""SELECT result_kind, scope_key, business_date, account_code, base_currency, calculation_version, result_hash
        FROM public.pams_valuation_result_version WHERE version_key = %s""", (version_key,))
    row = cur.fetchone()
    if not row:
        return False
    names = ("result_kind", "scope_key", "business_date", "account_code", "base_currency", "calculation_version", "result_hash")
    identity = {key: row[key] if isinstance(row, dict) else row[index] for index, key in enumerate(names)}
    cur.execute("SELECT input_kind, input_hash, evidence FROM public.pams_valuation_input_evidence WHERE version_key = %s", (version_key,))
    inputs = []
    for item in cur.fetchall():
        kind, input_hash, evidence = ((item["input_kind"], item["input_hash"], item["evidence"])
                                      if isinstance(item, dict) else item)
        if not isinstance(evidence, dict) or evidence.get("kind") != kind or _hash(evidence) != input_hash:
            return False
        inputs.append(evidence)
    try:
        identity["inputs"] = normalize_inputs(inputs)
        return _hash(identity) == version_key
    except (TypeError, ValueError):
        return False


# Fixed SQL only; a caller may name known source keys but cannot supply values,
# source timestamps, quality, or the result hash in this adapter.
_BOUND_SOURCES = {
    "holding": ("pams_holdings_snapshot", ("id",), "as_of", "SELECT row_to_json(s)::text FROM public.pams_holdings_snapshot s WHERE id = %s FOR SHARE"),
    "trade": ("pams_investment_trade_fact", ("trade_id",), "trade_date", "SELECT row_to_json(s)::text FROM public.pams_investment_trade_fact s WHERE trade_id = %s FOR SHARE"),
    "quote": ("pams_market_quote_snapshot", ("quote_id",), "quote_date", "SELECT row_to_json(s)::text FROM public.pams_market_quote_snapshot s WHERE quote_id = %s FOR SHARE"),
    "fx": ("pams_fx_rates_daily", ("as_of", "base_currency", "quote_currency"), "as_of", "SELECT row_to_json(s)::text FROM public.pams_fx_rates_daily s WHERE as_of = %s AND base_currency = %s AND quote_currency = %s FOR SHARE"),
}


def _bound_input(cur: Any, kind: str, key: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if kind not in _BOUND_SOURCES or not isinstance(key, dict):
        raise ValueError("unsupported source reference")
    table, fields, date_field, statement = _BOUND_SOURCES[kind]
    if set(key) != set(fields) or not all(_known_key(key[field]) for field in fields):
        raise ValueError("invalid source key")
    if kind in {"holding", "trade", "quote"}:
        value = key[fields[0]]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("invalid source id")
    if kind == "fx":
        try:
            day = key["as_of"]
            if isinstance(day, str):
                date.fromisoformat(day)
            elif not isinstance(day, date) or isinstance(day, datetime):
                raise ValueError("invalid fx date")
        except ValueError as exc:
            raise ValueError("invalid fx date") from exc
        if any(not isinstance(key[field], str) or not key[field].strip() for field in ("base_currency", "quote_currency")):
            raise ValueError("invalid fx currency")
    # Normalize dates for stable JSON, while SQL remains parameterized.
    source_key = _normalize(key)
    ref: dict[str, Any] = {"table": table, "key": source_key}
    cur.execute(statement, tuple(key[field] for field in fields))
    result = cur.fetchone()
    raw = json.loads((next(iter(result.values())) if isinstance(result, dict) else result[0]), parse_float=Decimal) if result else None
    if raw is None:
        return ({"kind": kind, "source_ref": ref, "source_version": None, "business_date": None,
                 "observed_at": None, "quality": "unknown", "snapshot": None}, None)
    if kind in {"holding", "trade"} and raw.get("account_code"):
        ref["account_code"] = raw["account_code"]
    quality = raw.get("return_status") if kind == "quote" else "unknown"
    if quality not in QUALITIES:
        quality = "unknown"
    # Only an actual quote price_time is an observation time; fetch/update times
    # and timezone-naive trade times are not silently converted into one.
    observed = raw.get("price_time") if kind == "quote" else None
    if observed:
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            observed = parsed.isoformat() if parsed.utcoffset() is not None else None
        except (TypeError, ValueError):
            observed = None
    evidence = {"kind": kind, "source_ref": ref, "source_version": None,
                "business_date": raw.get(date_field), "observed_at": observed,
                "quality": quality, "snapshot": {"content_hash": _hash(raw)}}
    return evidence, raw


def _bound_consistent(evidence: dict[str, Any], *, account_code: str, business_date: str) -> bool:
    if evidence["snapshot"] is None:
        return True  # Missing data is unknown, not a fabricated mismatch.
    kind = evidence["kind"]
    if kind in {"holding", "trade"} and evidence["source_ref"].get("account_code") != account_code:
        return False
    source_date = evidence["business_date"]
    if not source_date:
        return False
    # Historical trades may contribute to a later holding; future trades cannot.
    return source_date <= business_date if kind == "trade" else source_date == business_date


def record_bound_holding_version(cur: Any, *, holding_id: int, calculation_version: str,
                                 input_refs: list[dict[str, Any]]) -> str:
    """Record explicitly selected current DB inputs; never infer old relationships."""
    if cur.connection.autocommit:
        raise ValueError("transaction required")
    if not isinstance(holding_id, int) or isinstance(holding_id, bool) or holding_id <= 0:
        raise ValueError("invalid holding id")
    holding, raw = _bound_input(cur, "holding", {"id": holding_id})
    if raw is None or not raw.get("account_code") or not raw.get("as_of"):
        raise ValueError("holding result source is missing")
    account, day = raw["account_code"], raw["as_of"]
    inputs = [holding]
    for ref in input_refs:
        if not isinstance(ref, dict) or set(ref) - {"kind", "key", "role"} or ref.get("kind") not in KINDS:
            raise ValueError("only source kind, key and role may be supplied")
        role = ref.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ValueError("role must be nonempty")
        if "key" not in ref or ref["key"] is None:
            if role is None:
                raise ValueError("unknown input requires a role")
            item = {"kind": ref["kind"], "role": role, "source_ref": None, "source_version": None,
                    "business_date": None, "observed_at": None, "quality": "unknown", "snapshot": None}
        else:
            item, _ = _bound_input(cur, ref["kind"], ref["key"])
            item["role"] = role
        if not _bound_consistent(item, account_code=account, business_date=day):
            raise ValueError("input account or business date conflicts with holding")
        inputs.append(item)
    return record_result_version(cur, result_kind="holding", scope_key=f"holding:{holding_id}",
        account_code=account, business_date=date.fromisoformat(day), base_currency=raw["currency"],
        calculation_version=calculation_version, result_hash=_hash(raw), inputs=inputs)


def bound_holding_freshness(cur: Any, version_key: str) -> str:
    """Re-read captured references; unknown never asserts a complete input set."""
    if not verify_recorded_version(cur, version_key):
        return "invalid"
    cur.execute("SELECT account_code, business_date FROM public.pams_valuation_result_version WHERE version_key=%s AND result_kind='holding'", (version_key,))
    result = cur.fetchone()
    if not result:
        return "invalid"
    account, day = ((result["account_code"], result["business_date"]) if isinstance(result, dict) else result)
    cur.execute("SELECT evidence FROM public.pams_valuation_input_evidence WHERE version_key=%s", (version_key,))
    stored = [(row["evidence"] if isinstance(row, dict) else row[0]) for row in cur.fetchall()]
    if not stored:
        return "unknown"
    current, known = [], []
    unknown, missing = False, False
    for original in stored:
        ref = original.get("source_ref") or {}
        kind = original.get("kind")
        if not ref:
            unknown = True
            continue
        if kind not in _BOUND_SOURCES or ref.get("table") != _BOUND_SOURCES[kind][0]:
            unknown = True
            continue
        try:
            item, raw = _bound_input(cur, kind, ref.get("key"))
        except (TypeError, ValueError):
            unknown = True
            continue
        if raw is None:
            if original.get("snapshot") is not None:
                missing = True
            else:
                unknown = True
            continue
        if not _bound_consistent(item, account_code=account, business_date=day.isoformat()):
            return "inconsistent"
        item["role"] = original.get("role")
        known.append(original)
        current.append(item)
    freshness = recorded_input_freshness(known, current)
    if freshness == "changed":
        return "changed"
    if missing:
        return "missing"
    return "unknown" if unknown else freshness
