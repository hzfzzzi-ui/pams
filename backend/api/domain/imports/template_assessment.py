"""Summarize source-template recognition without asserting financial correctness."""
from __future__ import annotations


def assess_template(parsed: dict, kind: str) -> str:
    rows = parsed.get('rows') or []
    if not rows:
        return 'unmatched'
    codes = {warning.get('code') for warning in parsed.get('warnings', []) if isinstance(warning, dict)}
    if codes & {'headers_changed', 'ambiguous_header'}:
        return 'needs_mapping'
    if any(row.get('source', {}).get('profile') == 'ths_broker_ledger_v1' for row in rows):
        return 'matched'
    if kind == 'cash' and any(row.get('source', {}).get('statement') is True and not row.get('excluded') for row in rows):
        return 'matched'
    fields = set((parsed.get('suggested_mapping') or {}).keys())
    identity = bool(fields & {'symbol', 'name'})
    if kind == 'trade':
        matched = identity and {'business_date', 'action'} <= fields and bool(fields & {'gross_amount', 'net_cash', 'quantity'})
    elif kind == 'holding':
        matched = identity and {'business_date', 'quantity', 'market_value'} <= fields
    elif kind == 'cash':
        matched = {'business_date', 'net_cash'} <= fields
    else:
        matched = False
    return 'matched' if matched else 'needs_mapping'
