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


KINDS = {"holding", "trade", "quote", "fx"}
QUALITIES = {"unknown", "pending", "estimated", "confirmed"}
FIELDS = {"kind", "source_ref", "source_version", "business_date", "observed_at", "quality", "snapshot"}


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
        row = _normalize({"kind": item["kind"], "source_ref": source_ref,
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
        for field in ("source_version", "business_date", "observed_at", "snapshot"):
            if field == "snapshot" and (not left[field] or not right[field]):
                continue
            if left[field] is not None and right[field] is not None and left[field] != right[field]:
                return "changed"
        if left["quality"] != "unknown" and right["quality"] != "unknown" and left["quality"] != right["quality"]:
            return "changed"
    if old.keys() != new.keys():
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
