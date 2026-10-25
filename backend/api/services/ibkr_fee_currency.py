"""Explicit commission units only. No FX, cash, cost or return calculations."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def currency_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    return code.upper() if len(code) == 3 and code.isascii() and code.isalpha() else None


def explicit_ibkr_fee_currency(row: Mapping[str, Any], fee_amount: Decimal) -> str | None:
    """Use the actual raw attribute, never the parser's transaction-currency default."""
    raw = row.get('raw_json')
    if row.get('source_table') != 'pams_ibkr_trades' or not isinstance(raw, Mapping):
        return None
    values = {}
    for field in ('ibcommissioncurrency', 'ibcommission'):
        matches = [value for key, value in raw.items() if str(key).lower() == field]
        if len(matches) != 1:
            return None
        values[field] = matches[0]
    code = currency_code(values['ibcommissioncurrency'])
    if code is None or values['ibcommission'] is None or isinstance(values['ibcommission'], bool):
        return None
    try:
        commission = Decimal(str(values['ibcommission']))
        if not commission.is_finite() or not fee_amount.is_finite() or abs(commission) != fee_amount:
            return None
    except (InvalidOperation, ValueError):
        return None
    return code


def fact_currency_check(fact: Mapping[str, Any], expected: Any, *, source_verified: bool) -> dict:
    present = 'fee_currency' in fact
    actual = fact.get('fee_currency')
    code = currency_code(actual)
    if not present:
        status = 'column_unavailable'
    elif actual is None:
        status = 'missing'
    elif code is None or actual != code:
        status = 'invalid'
    elif currency_code(expected) is None or not source_verified:
        status = 'unverified'
    else:
        status = 'matched' if code == currency_code(expected) else 'mismatch'
    return {'status': status, 'column_available': present, 'expected': expected, 'actual': actual,
            'cash_settlement_verified': False, 'additional_deduction_proposed': False}
