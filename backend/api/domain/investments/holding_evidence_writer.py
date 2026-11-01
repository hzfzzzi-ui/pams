"""Isolated two-phase evidence for same-day holding changes.

Both phases and the caller's holding write run in one transaction. A known new
source must be published before finish; missing evidence is explicitly unknown.
The existing source_item_id denotes creation, not the current value's source.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.imports.isolation import source_enabled
from domain.investments.valuation_input_evidence import (
    current_holding_source,
    _bound_input,
    _bound_consistent,
    _hash,
    _verified_current_source,
    record_bound_holding_version,
    verify_recorded_version,
)


def _require(cur):
    if not source_enabled(cur) or cur.connection.autocommit:
        raise ValueError('holding evidence writer requires an isolated transaction')


def _value(row, key, index):
    return row[key] if isinstance(row, Mapping) else row[index]


def lock_holding_accounts(cur: Any, accounts) -> None:
    """Take the common transaction lock in stable order for multi-account writes."""
    for account in sorted({str(value) for value in accounts if value}):
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + account,))



def assert_holding_accounts_locked(cur: Any, accounts) -> None:
    """Reject an inherited lock set unless this transaction actually owns it."""
    values = sorted({str(value) for value in accounts if value})
    if not values:
        raise ValueError('holding account lock set is empty')
    cur.execute("""SELECT count(*) AS missing FROM unnest(%s::text[]) AS a(account_code)
        WHERE NOT EXISTS (SELECT 1 FROM pg_locks l
            WHERE l.pid=pg_backend_pid() AND l.locktype='advisory' AND l.granted
              AND l.mode='ExclusiveLock' AND l.objsubid=1
              AND l.classid=((hashtextextended('account-import:' || a.account_code,0) >> 32) & 4294967295)::oid
              AND l.objid=(hashtextextended('account-import:' || a.account_code,0) & 4294967295)::oid)""", (values,))
    if _value(cur.fetchone(),'missing',0):
        raise ValueError('holding account locks are not owned by this transaction')
    # pg_locks does not distinguish session-level from transaction-level locks.
    # The backend already holds each exclusive key, so a nonblocking transaction
    # lock cannot wait or expand the account set and guarantees release at commit.
    cur.execute("""SELECT bool_and(pg_try_advisory_xact_lock(hashtextextended('account-import:' || account_code,0))) AS acquired
        FROM unnest(%s::text[]) AS a(account_code)""", (values,))
    if not _value(cur.fetchone(),'acquired',0):
        raise ValueError('holding transaction locks could not be acquired')

def _current_holding_already_captured(cur: Any, holding_id: int, account_code: str, as_of) -> bool:
    """Reuse a verified immutable snapshot of the exact current row, regardless of source certainty."""
    _, raw = _bound_input(cur, 'holding', {'id': holding_id})
    if raw is None or raw.get('account_code') != account_code or raw.get('as_of') != as_of.isoformat():
        return False
    content_hash = _hash(raw)
    cur.execute("""SELECT v.version_key FROM public.valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
        WHERE v.result_kind='holding' AND v.account_code=%s AND v.business_date=%s AND v.result_hash=%s
          AND e.input_kind='holding'
          AND e.evidence->'source_ref'->'key'->>'id'=%s
          AND e.evidence->'snapshot'->>'content_hash'=%s""",
        (account_code, as_of, content_hash, str(holding_id), content_hash))
    versions = [_value(row, 'version_key', 0) for row in cur.fetchall()]
    return any(verify_recorded_version(cur, version) for version in versions)


def prepare_holding_change(cur: Any, *, account_code: str, name: str, as_of, source_item_id: str | None):
    """Lock account and capture the old value before any upsert or replacement."""
    _require(cur)
    if not account_code or not name or not as_of:
        raise ValueError('holding identity is incomplete')
    lock_holding_accounts(cur, [account_code])
    cur.execute("SELECT to_regclass('public.valuation_evidence') AS table_name")
    if not _value(cur.fetchone(), 'table_name', 0):
        raise ValueError('holding correction requires versioned evidence')
    cur.execute('SELECT id,source_item_id FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s FOR UPDATE',
                (account_code, name, as_of))
    previous = cur.fetchone()
    if previous:
        previous_id = _value(previous, 'id', 0)
        if not _current_holding_already_captured(cur, previous_id, account_code, as_of):
            source = current_holding_source(cur, previous_id)
            known_source = {'source_item_id': source['source_item_id']} if source['status'] == 'recorded' else None
            record_bound_holding_version(cur, holding_id=previous_id, calculation_version='holding-change-before',
                input_refs=[{'kind':'source_item', 'key':known_source, 'role':'current_source'}])
    previous_source = _value(previous, 'source_item_id', 1) if previous else None
    return {'previous_id': _value(previous, 'id', 0) if previous else None,
            'retain_origin': bool(previous_source is not None and str(previous_source) != str(source_item_id or ''))}


def finish_holding_change(cur: Any, *, holding_id: int, source_item_id: str | None,
                          input_refs: list[dict[str, Any]] | None = None, snapshot_cache: dict | None = None):
    """Capture the new value and verified source, or explicitly unknown source."""
    _require(cur)
    if input_refs is not None and not isinstance(input_refs, list):
        raise ValueError('holding input references must be a list')
    refs = input_refs or []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get('role') == 'current_source':
            raise ValueError('current source is controlled by the writer')
        if (source_item_id and ref.get('kind') == 'source_item' and isinstance(ref.get('key'), dict) and
            str(ref['key'].get('source_item_id')) == str(source_item_id)):
            raise ValueError('duplicate current source reference')
    source_key = {'source_item_id': str(source_item_id)} if source_item_id else None
    return record_bound_holding_version(cur, holding_id=holding_id, calculation_version='holding-change-after',
        input_refs=[{'kind':'source_item', 'key':source_key, 'role':'current_source'}, *refs], snapshot_cache=snapshot_cache)


def verify_historical_holding_source(cur: Any, *, holding_id: int, source_item_id: str,
                                     snapshot_cache: dict | None = None) -> bool:
    """Prove an explicit source contributed to a recorded holding version."""
    if cur.connection.autocommit:
        raise ValueError('holding evidence verification requires a transaction')
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    readonly, level = ((mode['readonly'], mode['level']) if isinstance(mode, Mapping) else mode)
    locked_account = None
    if readonly:
        if level not in {'repeatable read', 'serializable'}:
            raise ValueError('holding evidence verification requires a consistent read-only snapshot')
        snapshot_cache = None
    else:
        _require(cur)
        cur.execute('SELECT account_code FROM pams_holdings_snapshot WHERE id=%s', (holding_id,))
        account_row = cur.fetchone()
        if not account_row:
            return False
        account = _value(account_row, 'account_code', 0)
        if not account:
            return False
        lock_holding_accounts(cur, [account])
        cur.execute('SELECT account_code FROM pams_holdings_snapshot WHERE id=%s', (holding_id,))
        current_account = cur.fetchone()
        if not current_account or _value(current_account, 'account_code', 0) != account:
            return False
        locked_account = account
    cur.execute("""SELECT v.version_key,s.evidence AS source_evidence,h.evidence AS holding_evidence,
                       v.account_code,v.business_date FROM valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS s(input_kind text,input_hash text,evidence jsonb)
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS h(input_kind text,input_hash text,evidence jsonb)
        WHERE s.input_kind='source_item' AND s.evidence->>'role'='current_source'
          AND h.input_kind='holding'
          AND s.evidence->'source_ref'->'key'->>'source_item_id'=%s
          AND h.evidence->'snapshot'->'content'->>'id'=%s""", (str(source_item_id), str(holding_id)))
    for row in cur.fetchall():
        version, source, holding, account, day = (
            (row['version_key'], row['source_evidence'], row['holding_evidence'], row['account_code'], row['business_date'])
            if isinstance(row, Mapping) else row)
        if not readonly and account != locked_account:
            continue
        if not verify_recorded_version(cur, version):
            continue
        content = (holding.get('snapshot') or {}).get('content')
        ref = (source.get('source_ref') or {}).get('key')
        if not isinstance(content, dict) or not isinstance(ref, dict):
            continue
        try:
            actual, raw = _bound_input(cur, 'source_item', ref, locking=not readonly, snapshot_cache=snapshot_cache)
            actual['role'] = 'current_source'
            if (raw is not None and (source.get('snapshot') or {}).get('content_hash') == (actual.get('snapshot') or {}).get('content_hash')
                    and _bound_consistent(actual, account_code=account, business_date=day.isoformat(), holding_raw=content)
                    and _verified_current_source(cur, raw, holding_id, content, locking=not readonly,
                                                 snapshot_cache=snapshot_cache)):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False
