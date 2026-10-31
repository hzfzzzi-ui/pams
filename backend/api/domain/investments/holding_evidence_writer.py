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


def prepare_holding_change(cur: Any, *, account_code: str, name: str, as_of, source_item_id: str | None):
    """Lock account and capture the old value before any upsert or replacement."""
    _require(cur)
    if not account_code or not name or not as_of:
        raise ValueError('holding identity is incomplete')
    lock_holding_accounts(cur, [account_code])
    cur.execute("SELECT to_regclass('public.pams_valuation_result_version') AS table_name")
    if not _value(cur.fetchone(), 'table_name', 0):
        raise ValueError('holding correction requires versioned evidence')
    cur.execute('SELECT id,source_item_id FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s FOR UPDATE',
                (account_code, name, as_of))
    previous = cur.fetchone()
    if previous:
        previous_id = _value(previous, 'id', 0)
        source = current_holding_source(cur, previous_id)
        known_source = {'source_item_id': source['source_item_id']} if source['status'] == 'recorded' else None
        record_bound_holding_version(cur, holding_id=previous_id, calculation_version='holding-change-before',
            input_refs=[{'kind':'source_item', 'key':known_source, 'role':'current_source'}])
    previous_source = _value(previous, 'source_item_id', 1) if previous else None
    return {'previous_id': _value(previous, 'id', 0) if previous else None,
            'retain_origin': bool(previous_source is not None and str(previous_source) != str(source_item_id or ''))}


def finish_holding_change(cur: Any, *, holding_id: int, source_item_id: str | None,
                          input_refs: list[dict[str, Any]] | None = None):
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
        input_refs=[{'kind':'source_item', 'key':source_key, 'role':'current_source'}, *refs])


def verify_historical_holding_source(cur: Any, *, holding_id: int, source_item_id: str) -> bool:
    """Prove an explicit source contributed to a recorded holding version."""
    _require(cur)
    cur.execute("""SELECT s.version_key FROM pams_valuation_input_evidence s
        JOIN pams_valuation_input_evidence h ON h.version_key=s.version_key AND h.input_kind='holding'
        WHERE s.input_kind='source_item' AND s.evidence->>'role'='current_source'
          AND s.evidence->'source_ref'->'key'->>'source_item_id'=%s
          AND h.evidence->'snapshot'->'content'->>'id'=%s""", (str(source_item_id), str(holding_id)))
    return any(verify_recorded_version(cur, _value(row, 'version_key', 0)) for row in cur.fetchall())
