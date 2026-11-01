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
from uuid import UUID

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
            identity = _canonical([row["kind"], row["source_ref"], row["role"]])
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
    key = lambda row: _canonical([row["kind"], row["source_ref"], row["role"]])
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
    from domain.imports.isolation import source_enabled
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError("valuation evidence requires an isolated transaction")
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
    cur.execute("SELECT 1 FROM public.valuation_evidence WHERE version_key = %s", (version_key,))
    if cur.fetchone():
        return version_key
    packed = [{"input_hash": _hash(row), "input_kind": row["kind"], "evidence": row} for row in rows]
    cur.execute("""INSERT INTO public.valuation_evidence
        (version_key, result_kind, scope_key, account_code, business_date, base_currency, calculation_version, result_hash, inputs)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (version_key, result_kind, scope_key, account_code, business_date, base_currency, calculation_version, result_hash, Json(packed)))
    return version_key


def verify_recorded_version(cur: Any, version_key: str) -> bool:
    """Verify internal fingerprints; this does not authenticate external sources."""
    cur.execute("""SELECT result_kind, scope_key, business_date, account_code, base_currency, calculation_version, result_hash
        FROM public.valuation_evidence WHERE version_key = %s""", (version_key,))
    row = cur.fetchone()
    if not row:
        return False
    names = ("result_kind", "scope_key", "business_date", "account_code", "base_currency", "calculation_version", "result_hash")
    identity = {key: row[key] if isinstance(row, dict) else row[index] for index, key in enumerate(names)}
    cur.execute("""SELECT e.input_kind,e.input_hash,e.evidence FROM public.valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
        WHERE v.version_key = %s""", (version_key,))
    inputs = []
    for item in cur.fetchall():
        kind, input_hash, evidence = ((item["input_kind"], item["input_hash"], item["evidence"])
                                      if isinstance(item, dict) else item)
        if not isinstance(evidence, dict) or evidence.get("kind") != kind or _hash(evidence) != input_hash:
            return False
        snapshot = evidence.get("snapshot")
        if isinstance(snapshot, dict) and "content" in snapshot and _hash(snapshot["content"]) != snapshot.get("content_hash"):
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
    "quote": ("market_observations", ("quote_id",), "quote_date", "SELECT row_to_json(s)::text FROM public.market_observations s WHERE quote_id = %s AND observation_type = 'quote' FOR SHARE"),
    "fx": ("pams_fx_rates_daily", ("as_of", "base_currency", "quote_currency"), "as_of", "SELECT row_to_json(s)::text FROM public.pams_fx_rates_daily s WHERE as_of = %s AND base_currency = %s AND quote_currency = %s FOR SHARE"),
    "source_item": ("source_items", ("source_item_id",), None, "SELECT json_build_object('item', row_to_json(i), 'batch', row_to_json(b))::text FROM public.source_items i JOIN public.source_batches b ON b.batch_id=i.batch_id WHERE i.source_item_id = %s FOR SHARE OF i,b"),
}


def _bound_input(cur: Any, kind: str, key: dict[str, Any], *, locking: bool = True,
                 snapshot_cache: dict | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if kind not in _BOUND_SOURCES or not isinstance(key, dict):
        raise ValueError("unsupported source reference")
    table, key_fields, date_field, statement = _BOUND_SOURCES[kind]
    if set(key) != set(key_fields) or not all(_known_key(key[field]) for field in key_fields):
        raise ValueError("invalid source key")
    if kind in {"holding", "trade", "quote"}:
        value = key[key_fields[0]]
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
    if kind == "source_item":
        try:
            canonical_id = str(UUID(str(key["source_item_id"])))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid source item id") from exc
        key = {"source_item_id": canonical_id}
    # Normalize dates for stable JSON, while SQL remains parameterized.
    source_key = _normalize(key)
    ref: dict[str, Any] = {"table": table, "key": source_key}
    if not locking:
        cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
        mode = cur.fetchone()
        readonly = mode['readonly'] if isinstance(mode, dict) else mode[0]
        level = mode['level'] if isinstance(mode, dict) else mode[1]
        if not readonly or level not in {'repeatable read', 'serializable'}:
            raise ValueError('unlocked evidence reads require a read-only repeatable-read transaction')
        statement = statement.split(' FOR SHARE', 1)[0]
    source_content_hash = None
    if kind == 'source_item' and locking and snapshot_cache is not None:
        # Avoid transferring a large published_result once per item. The same
        # locked MVCC identity guard also protects reuse after savepoint rollback.
        cur.execute("""SELECT row_to_json(i)::text AS item_json,b.batch_id AS batch_id
            FROM source_items i JOIN source_batches b ON b.batch_id=i.batch_id
            WHERE i.source_item_id=%s FOR SHARE OF i,b""", (key['source_item_id'],))
        result = cur.fetchone()
        if result:
            item_json, batch_id = ((result['item_json'], result['batch_id']) if isinstance(result, dict) else result)
            batch_id = str(batch_id)
            identity = _locked_snapshot_identity(cur, batch_id)
            cache_key = ('holding_source_batch', batch_id)
            cached = snapshot_cache.get(cache_key)
            if not cached or cached[0] != identity:
                cur.execute('SELECT row_to_json(b)::text AS batch_json FROM source_batches b WHERE b.batch_id=%s FOR SHARE', (batch_id,))
                batch_row = cur.fetchone()
                if not batch_row:
                    raise ValueError('source batch is missing')
                batch_json = batch_row['batch_json'] if isinstance(batch_row, dict) else batch_row[0]
                batch_raw = json.loads(batch_json, parse_float=Decimal)
                # Preserve the original canonical hash exactly while avoiding
                # re-serializing an entire 2000-row published result per item.
                prefix = hashlib.sha256(b'{"batch":' + _canonical(batch_raw).encode('utf8') + b',"item":')
                cached = (identity, batch_raw, prefix)
                snapshot_cache[cache_key] = cached
            item_raw = json.loads(item_json, parse_float=Decimal)
            raw = {'item': item_raw, 'batch': cached[1]}
            digest = cached[2].copy()
            digest.update(_canonical(item_raw).encode('utf8') + b'}')
            source_content_hash = digest.hexdigest()
        else:
            raw = None
    else:
        cur.execute(statement, tuple(key[field] for field in key_fields))
        result = cur.fetchone()
        raw = json.loads((next(iter(result.values())) if isinstance(result, dict) else result[0]), parse_float=Decimal) if result else None
    if raw is None:
        return ({"kind": kind, "source_ref": ref, "source_version": None, "business_date": None,
                 "observed_at": None, "quality": "unknown", "snapshot": None}, None)
    if kind == "source_item":
        batch, source_item = raw["batch"], raw["item"]
        ref["account_code"] = batch.get("account_code")
        parsed = source_item.get("parsed_fields")
        fields = parsed.get("fields") if isinstance(parsed, dict) else None
        resolved_assets: list[str] = []
        if isinstance(fields, dict) and batch.get("business_type") == "holding" and fields.get("asset_type") != "cash":
            from domain.assets.alias_policy import is_identity_alias
            identifier, symbol = fields.get("asset_id"), fields.get("symbol")
            if identifier:
                cur.execute("SELECT asset_id FROM pams_asset_master WHERE asset_id=%s AND status='active' AND currency='CNY'", (identifier,))
                resolved_assets = [row["asset_id"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
            elif symbol:
                cur.execute("SELECT asset_id FROM pams_asset_master WHERE primary_symbol=%s AND status='active' AND currency='CNY'", (symbol,))
                resolved_assets = [row["asset_id"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
                cur.execute("""SELECT a.asset_id,a.alias_type,a.alias_value FROM pams_asset_alias a
                    JOIN pams_asset_master m ON m.asset_id=a.asset_id
                    WHERE a.alias_value=%s AND m.status='active' AND m.currency='CNY'""", (symbol,))
                for alias in cur.fetchall():
                    asset, alias_type, value = ((alias["asset_id"], alias["alias_type"], alias["alias_value"])
                                                if isinstance(alias, dict) else alias)
                    if is_identity_alias(asset, alias_type, value):
                        resolved_assets.append(asset)
                resolved_assets = sorted(set(resolved_assets))
        snapshot = {"content_hash": source_content_hash or _hash(raw), "batch_status": batch.get("status"),
                    "review_status": source_item.get("review_status"), "business_type": batch.get("business_type"),
                    "date_from": batch.get("date_from"), "date_to": batch.get("date_to"),
                    "source_action": fields.get("action") if isinstance(fields, dict) else None,
                    "source_fields": _normalize(fields) if isinstance(fields, dict) else None,
                    "resolved_assets": resolved_assets}
        return ({"kind": kind, "source_ref": ref, "source_version": None, "business_date": None,
                 "observed_at": None, "quality": "unknown", "snapshot": snapshot}, raw)
    if kind in {"holding", "trade"} and raw.get("account_code"):
        ref["account_code"] = raw["account_code"]
    if kind == "holding":
        ref["logical_key"] = {"account_code": raw.get("account_code"), "name": raw.get("name"), "as_of": raw.get("as_of")}
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
    snapshot = {"content_hash": _hash(raw)}
    if kind == "holding":
        snapshot["content"] = _normalize(raw)
    evidence = {"kind": kind, "source_ref": ref, "source_version": None,
                "business_date": raw.get(date_field) if date_field else None, "observed_at": observed,
                "quality": quality, "snapshot": snapshot}
    return evidence, raw


def _current_source_matches_holding(snapshot: dict[str, Any], holding_raw: dict[str, Any], business_date: str) -> bool:
    """A reviewed row can describe a holding only when its values actually match.

    This verifies an association, not the authenticity of an external report or
    an IBKR contract identity. Derived trade inputs are not holding statements.
    """
    fields = snapshot.get("source_fields")
    if not isinstance(fields, dict) or fields.get("business_date") != business_date:
        return False
    if not fields.get("currency") or str(fields["currency"]).upper() != str(holding_raw.get("currency") or "").upper():
        return False
    cash = snapshot.get("business_type") == "cash" and fields.get("action") == "balance"
    cash |= snapshot.get("business_type") == "holding" and fields.get("asset_type") == "cash"
    pairs: tuple[tuple[str, str], ...]
    if cash:
        if holding_raw.get("major_category") != "现金":
            return False
        pairs = (("net_cash", "quantity"), ("net_cash", "cost"), ("net_cash", "market_value"))
    elif snapshot.get("business_type") == "holding":
        exact_symbol = fields.get("symbol") and str(fields["symbol"]).strip().upper() == str(holding_raw.get("symbol") or "").strip().upper()
        resolved = snapshot.get("resolved_assets") or []
        resolved_identity = len(resolved) == 1 and resolved[0] == holding_raw.get("asset_id")
        if len(resolved) > 1 or ((resolved or holding_raw.get("asset_id")) and not resolved_identity):
            return False
        if not exact_symbol and not resolved_identity:
            return False
        if fields.get("cost_basis_type") != "holding_cost":
            return False
        pairs = (("quantity", "quantity"), ("price", "current_price"),
                 ("cost", "cost"), ("market_value", "market_value"))
    else:
        return False
    try:
        for source_key, holding_key in pairs:
            if fields.get(source_key) in (None, "") or holding_raw.get(holding_key) is None:
                return False
            source_value, holding_value = Decimal(str(fields[source_key])), Decimal(str(holding_raw[holding_key]))
            if not source_value.is_finite() or not holding_value.is_finite() or source_value != holding_value:
                return False
    except (ValueError, ArithmeticError):
        return False
    return True


def _locked_snapshot_identity(cur: Any, batch_id: str) -> tuple:
    """Re-lock every tuple and fingerprint its visible MVCC identity."""
    cur.execute("""WITH b AS MATERIALIZED (
        SELECT batch_id,xmin::text AS x,ctid::text AS p FROM source_batches WHERE batch_id=%s FOR UPDATE
      ), f AS MATERIALIZED (
        SELECT file_id,xmin::text AS x,ctid::text AS p FROM source_files WHERE batch_id=%s FOR SHARE
      ), i AS MATERIALIZED (
        SELECT source_item_id,xmin::text AS x,ctid::text AS p FROM source_items WHERE batch_id=%s FOR SHARE
      )
      SELECT txid_current() AS xid,
        (SELECT batch_id::text||':'||x||':'||p FROM b) AS batch_identity,
        encode(sha256(convert_to(coalesce((SELECT string_agg(file_id::text||':'||x||':'||p,',' ORDER BY file_id) FROM f),''),'UTF8')),'hex') AS files_identity,
        encode(sha256(convert_to(coalesce((SELECT string_agg(source_item_id::text||':'||x||':'||p,',' ORDER BY source_item_id) FROM i),''),'UTF8')),'hex') AS items_identity""",
        (batch_id,batch_id,batch_id))
    row = cur.fetchone()
    if isinstance(row, dict):
        result = (row['xid'],row['batch_identity'],row['files_identity'],row['items_identity'])
    else:
        result = tuple(row)
    if not result[1]:
        raise ValueError('source batch is missing')
    return (id(cur.connection), *result)


def _verified_current_source(cur: Any, raw: dict[str, Any], holding_id: int,
                             holding_raw: dict[str, Any], *, locking: bool,
                             snapshot_cache: dict | None = None) -> bool:
    """Rebuild the published native snapshot and bind its decision to this holding."""
    from domain.imports.source_fact_evidence import _reviewed_snapshot

    batch, item = raw.get('batch'), raw.get('item')
    if not isinstance(batch, dict) or not isinstance(item, dict) or batch.get('workflow') != 'account_import':
        return False
    parsed, source = item.get('parsed_fields'), (item.get('raw_fields') or {}).get('source') or {}
    if (not isinstance(parsed, dict) or parsed.get('excluded') is not False
            or item.get('parse_method') not in {'manual', 'template', 'ocr'}
            or (item['parse_method'] == 'ocr' and parsed.get('acknowledged') is not True)
            or str(item.get('source_uid')) != str(item.get('source_item_id'))):
        return False
    if item['parse_method'] in {'template', 'ocr'}:
        if (not isinstance(source, dict) or not item.get('file_id')
                or str(source.get('file_id')) != str(item['file_id'])
                or source.get('origin') != ('ocr' if item['parse_method'] == 'ocr' else 'file')):
            return False
        cur.execute('SELECT batch_id,sha256 FROM source_files WHERE file_id=%s' + (' FOR SHARE' if locking else ''),
                    (str(item['file_id']),))
        file = cur.fetchone()
        if not file:
            return False
        owner, digest = ((file['batch_id'], file['sha256']) if isinstance(file, dict) else file)
        if str(owner) != str(batch.get('batch_id')) or source.get('file_sha256') != str(digest).strip():
            return False
    try:
        batch_id = str(batch['batch_id'])
        if locking and snapshot_cache is not None:
            identity = _locked_snapshot_identity(cur, batch_id)
            key = ('holding_source', batch_id)
            if snapshot_cache.get(key) != identity:
                if not _reviewed_snapshot(cur, batch, batch_id, locking=True, snapshot_cache={}):
                    return False
                snapshot_cache[key] = identity
        elif not _reviewed_snapshot(cur, batch, batch_id, locking=locking,
                                    snapshot_cache=snapshot_cache if snapshot_cache is not None else {}):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    result = batch.get('published_result')
    records = result.get('records') if isinstance(result, dict) else None
    if not isinstance(records, list):
        return False
    matches = [entry for entry in records if isinstance(entry, dict)
               and entry.get('table') == 'pams_holdings_snapshot' and str(entry.get('id')) == str(holding_id)
               and str(entry.get('row_id')) == str(item['source_item_id'])]
    after = matches[0].get('after') if len(matches) == 1 else None
    fields = parsed.get('fields')
    if not isinstance(after, dict) or not isinstance(fields, dict):
        return False
    if (after.get('as_of') != fields.get('business_date') or after.get('currency') != fields.get('currency')
            or any(after.get(key) != holding_raw.get(key) for key in ('account_code','name','symbol','asset_id','currency','as_of'))):
        return False
    cash = fields.get('asset_type') == 'cash' or (fields.get('kind') == 'cash' and fields.get('action') == 'balance')
    pairs = (('net_cash','quantity'),('net_cash','cost'),('net_cash','market_value')) if cash else (
        ('quantity','quantity'),('price','current_price'),('cost','cost'),('market_value','market_value'))
    try:
        return all(fields.get(source_key) not in (None, '') and after.get(result_key) not in (None, '')
                   and Decimal(str(fields[source_key])).is_finite()
                   and Decimal(str(fields[source_key])) == Decimal(str(after[result_key]))
                   for source_key, result_key in pairs)
    except (ValueError, ArithmeticError):
        return False


def _bound_consistent(evidence: dict[str, Any], *, account_code: str, business_date: str,
                      holding_raw: dict[str, Any]) -> bool:
    if evidence["snapshot"] is None:
        return True  # Missing data is unknown, not a fabricated mismatch.
    kind = evidence["kind"]
    if kind == "source_item":
        snapshot = evidence["snapshot"]
        kind_allowed = snapshot.get("business_type") in {"holding", "trade"}
        cash_balance = (snapshot.get("business_type") == "cash" and snapshot.get("source_action") == "balance"
                        and holding_raw.get("major_category") == "现金")
        if (evidence["source_ref"].get("account_code") != account_code or snapshot.get("batch_status") != "published"
                or snapshot.get("review_status") != "confirmed" or not (kind_allowed or cash_balance)):
            return False
        # A batch range can disprove alignment; it cannot prove the exact input date.
        if snapshot.get("date_from") and snapshot["date_from"] > business_date:
            return False
        if (snapshot.get("business_type") in {"holding", "cash"} and snapshot.get("date_to")
                and snapshot["date_to"] < business_date and evidence.get('role')!='ibkr_reviewed_contract_identity'):
            return False
        if evidence.get("role") == "current_source" and not _current_source_matches_holding(snapshot, holding_raw, business_date):
            return False
        return True
    if kind in {"holding", "trade"} and evidence["source_ref"].get("account_code") != account_code:
        return False
    source_date = evidence["business_date"]
    if not source_date:
        return False
    # Historical trades may contribute to a later holding; future trades cannot.
    return source_date <= business_date if kind == "trade" else source_date == business_date


def record_bound_holding_version(cur: Any, *, holding_id: int, calculation_version: str,
                                 input_refs: list[dict[str, Any]], snapshot_cache: dict | None = None) -> str:
    """Record explicitly selected current DB inputs; never infer old relationships."""
    from domain.imports.isolation import source_enabled
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError("valuation evidence requires an isolated transaction")
    if not isinstance(holding_id, int) or isinstance(holding_id, bool) or holding_id <= 0:
        raise ValueError("invalid holding id")
    cur.execute("SELECT account_code FROM public.pams_holdings_snapshot WHERE id=%s", (holding_id,))
    selected = cur.fetchone()
    if not selected:
        raise ValueError("holding result source is missing")
    account_before_lock = selected["account_code"] if isinstance(selected, dict) else selected[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account-import:" + account_before_lock,))
    holding, raw = _bound_input(cur, "holding", {"id": holding_id})
    if raw is None or not raw.get("account_code") or not raw.get("name") or not raw.get("as_of"):
        raise ValueError("holding result source is missing")
    if raw["account_code"] != account_before_lock:
        raise ValueError("holding account changed while acquiring lock")
    account, day = raw["account_code"], raw["as_of"]
    inputs = [holding]
    linked = None
    linked_raw = None
    if raw.get("source_item_id"):
        linked, source_raw = _bound_input(cur, "source_item", {"source_item_id": raw["source_item_id"]}, snapshot_cache=snapshot_cache)
        linked_raw = source_raw
        linked["role"] = "holding_creation_source"
        if source_raw is None or not _bound_consistent(linked, account_code=account, business_date=day, holding_raw=raw):
            raise ValueError("linked holding source is missing, unconfirmed or inconsistent")
        inputs.append(linked)
    for ref in input_refs:
        source_raw = None
        if not isinstance(ref, dict) or set(ref) - {"kind", "key", "role"} or ref.get("kind") not in KINDS:
            raise ValueError("only source kind, key and role may be supplied")
        role = ref.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ValueError("role must be nonempty")
        if role == "holding_creation_source":
            raise ValueError("holding_creation_source comes only from the holding's stored link")
        if role == "current_source" and ref["kind"] != "source_item":
            raise ValueError("current_source must refer to a source item or remain unknown")
        if "key" not in ref or ref["key"] is None:
            if role is None:
                raise ValueError("unknown input requires a role")
            item = {"kind": ref["kind"], "role": role, "source_ref": None, "source_version": None,
                    "business_date": None, "observed_at": None, "quality": "unknown", "snapshot": None}
        else:
            if (ref['kind'] == 'source_item' and linked is not None and isinstance(ref['key'], dict)
                    and str(ref['key'].get('source_item_id')) == str(raw.get('source_item_id'))):
                item, source_raw = dict(linked), linked_raw
            else:
                item, source_raw = _bound_input(cur, ref["kind"], ref["key"], snapshot_cache=snapshot_cache)
            item["role"] = role
        if not _bound_consistent(item, account_code=account, business_date=day, holding_raw=raw):
            raise ValueError("input account or business date conflicts with holding")
        if (role == 'current_source' and ref.get('key') is not None and source_raw is not None
                and not _verified_current_source(cur, source_raw, holding_id, raw, locking=True,
                                                 snapshot_cache=snapshot_cache)):
            raise ValueError('current source publication evidence is unavailable')
        inputs.append(item)
    logical_scope = "holding:" + _hash({"account_code": account, "name": raw["name"]})
    return record_result_version(cur, result_kind="holding", scope_key=logical_scope,
        account_code=account, business_date=date.fromisoformat(day), base_currency=raw["currency"],
        calculation_version=calculation_version, result_hash=_hash(raw), inputs=inputs)


def bound_holding_freshness(cur: Any, version_key: str) -> str:
    """Re-read captured references; unknown never asserts a complete input set."""
    if not verify_recorded_version(cur, version_key):
        return "invalid"
    cur.execute("SELECT account_code, business_date FROM public.valuation_evidence WHERE version_key=%s AND result_kind='holding'", (version_key,))
    result = cur.fetchone()
    if not result:
        return "invalid"
    account, day = ((result["account_code"], result["business_date"]) if isinstance(result, dict) else result)
    cur.execute("""SELECT e.evidence FROM public.valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
        WHERE v.version_key=%s""", (version_key,))
    stored = [(row["evidence"] if isinstance(row, dict) else row[0]) for row in cur.fetchall()]
    if not stored:
        return "unknown"
    holdings = [row for row in stored if row.get("kind") == "holding" and isinstance(row.get("snapshot"), dict)
                and isinstance(row["snapshot"].get("content"), dict)]
    if len(holdings) != 1:
        return "unknown"
    holding_raw = holdings[0]["snapshot"]["content"]
    current, known = [], []
    unknown, missing, replacement = False, False, False
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
            source_key = ref.get('key')
            if not isinstance(source_key, dict):
                raise ValueError('invalid source key')
            item, raw = _bound_input(cur, kind, source_key)
        except (TypeError, ValueError):
            unknown = True
            continue
        if raw is None:
            if original.get("snapshot") is not None:
                logical = ref.get("logical_key") if kind == "holding" else None
                if isinstance(logical, dict) and all(logical.get(field) for field in ("account_code", "name", "as_of")):
                    cur.execute("""SELECT id FROM public.pams_holdings_snapshot
                        WHERE account_code=%s AND name=%s AND as_of=%s FOR SHARE""",
                        (logical["account_code"], logical["name"], logical["as_of"]))
                    if cur.fetchone():
                        replacement = True
                    else:
                        missing = True
                else:
                    missing = True
            else:
                unknown = True
            continue
        item["role"] = original.get("role")
        if not _bound_consistent(item, account_code=account, business_date=day.isoformat(), holding_raw=holding_raw):
            return "inconsistent"
        if original.get("snapshot") and original["snapshot"].get("content_hash") != item["snapshot"].get("content_hash"):
            return "changed"
        item["role"] = original.get("role")
        known.append(original)
        current.append(item)
    freshness = recorded_input_freshness(known, current)
    if freshness == "changed":
        return "changed"
    if missing:
        return "missing"
    if replacement:
        return "replacement_candidate"
    return "unknown" if unknown else freshness


def versions_for_source_item(cur: Any, source_item_id: str) -> list[dict[str, Any]]:
    """Find explicitly recorded versions only; absence is not proof of no use."""
    try:
        item_id = str(UUID(str(source_item_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid source item id") from exc
    cur.execute("""SELECT v.version_key, v.scope_key, v.account_code, v.business_date, e.evidence->>'role' AS role
        FROM public.valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
        WHERE v.inputs @> %s::jsonb AND e.input_kind='source_item'
          AND e.evidence->'source_ref'->'key'->>'source_item_id'=%s
        ORDER BY v.business_date, v.version_key, role""",
        (Json([{'input_kind':'source_item','evidence':{'source_ref':{'key':{'source_item_id':item_id}}}}]),item_id))
    names = ("version_key", "scope_key", "account_code", "business_date", "role")
    return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in cur.fetchall()]


def current_holding_source(cur: Any, holding_id: int) -> dict[str, Any]:
    """Return only an explicit current source for the exact current snapshot.

    A recorded source is not proof that the source alone produced the holding.
    Competing sources, missing sources, and incomplete evidence fail closed.
    """
    if cur.connection.autocommit:
        raise ValueError("transaction required")
    if not isinstance(holding_id, int) or isinstance(holding_id, bool) or holding_id <= 0:
        raise ValueError("invalid holding id")
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    readonly, level = ((mode['readonly'], mode['level']) if isinstance(mode, dict) else mode)
    if readonly and level not in {'repeatable read', 'serializable'}:
        raise ValueError('holding source read requires a consistent read-only snapshot')
    cur.execute("SELECT account_code FROM public.pams_holdings_snapshot WHERE id=%s", (holding_id,))
    row = cur.fetchone()
    if not row:
        return {"status": "unknown", "source_item_id": None}
    account = row["account_code"] if isinstance(row, dict) else row[0]
    if not readonly:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account-import:" + account,))
    _, raw = _bound_input(cur, "holding", {"id": holding_id}, locking=not readonly)
    if raw is None or raw.get("account_code") != account or not raw.get("name") or not raw.get("as_of"):
        return {"status": "unknown", "source_item_id": None}
    scope = "holding:" + _hash({"account_code": account, "name": raw["name"]})
    cur.execute("""SELECT version_key FROM public.valuation_evidence
        WHERE result_kind='holding' AND account_code=%s AND scope_key=%s AND business_date=%s AND result_hash=%s""",
        (account, scope, raw["as_of"], _hash(raw)))
    versions = [(row["version_key"] if isinstance(row, dict) else row[0]) for row in cur.fetchall()]
    if not versions:
        return {"status": "unknown", "source_item_id": None}
    recorded: set[str] = set()
    unknown = False
    snapshot_cache: dict | None = {} if readonly else None
    for version in versions:
        if not verify_recorded_version(cur, version):
            unknown = True
            continue
        cur.execute("""SELECT e.input_kind,e.evidence FROM public.valuation_evidence v
            CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
            WHERE v.version_key=%s""", (version,))
        values = [(row["input_kind"], row["evidence"]) if isinstance(row, dict) else row for row in cur.fetchall()]
        holding_rows = [value for kind, value in values if kind == "holding"]
        if (len(holding_rows) != 1 or (holding_rows[0].get("source_ref") or {}).get("key") != {"id": holding_id}
                or not isinstance((holding_rows[0].get("snapshot") or {}).get("content"), dict)
                or (holding_rows[0].get("snapshot") or {}).get("content_hash") != _hash(raw)):
            unknown = True
            continue
        sources = [value for kind, value in values if kind == "source_item" and value.get("role") == "current_source"]
        if not sources:
            unknown = True
        for source in sources:
            ref = source.get("source_ref") or {}
            if ref.get("table") != "source_items":
                unknown = True
                continue
            try:
                source_key = ref.get('key')
                if not isinstance(source_key, dict):
                    raise ValueError('invalid source key')
                item, source_raw = _bound_input(cur, "source_item", source_key, locking=not readonly)
            except (TypeError, ValueError):
                unknown = True
                continue
            item["role"] = "current_source"
            if (source_raw is None or not _bound_consistent(item, account_code=account, business_date=raw["as_of"], holding_raw=raw)
                    or not _verified_current_source(cur, source_raw, holding_id, raw, locking=not readonly,
                                                    snapshot_cache=snapshot_cache)
                    or (source.get("snapshot") or {}).get("content_hash") != item["snapshot"]["content_hash"]):
                unknown = True
                continue
            recorded.add(item["source_ref"]["key"]["source_item_id"])
    if len(recorded) > 1:
        return {"status": "ambiguous", "source_item_id": None}
    if unknown or not recorded:
        return {"status": "unknown", "source_item_id": None}
    return {"status": "recorded", "source_item_id": next(iter(recorded))}
