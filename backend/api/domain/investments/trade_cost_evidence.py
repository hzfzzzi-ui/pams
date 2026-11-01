"""Derive total trade charges only from explicitly recorded evidence."""
from decimal import Decimal, InvalidOperation


def unconverted_ibkr_fee(row):
    """A nonzero commission requires an explicit unit matching trade currency."""
    raw = row.get('raw_json')
    source = raw.get('source_row', {}) if isinstance(raw, dict) else {}
    table = row.get('source_table') or (source.get('source_table') if isinstance(source, dict) else None)
    if table != 'ibkr_flex_trade':
        return False
    try:
        fee = Decimal(str(row.get('fee_amount')))
    except (InvalidOperation, TypeError, ValueError):
        return True
    if not fee.is_finite():
        return True
    fee_currency = row.get('fee_currency') or (source.get('explicit_fee_currency') if isinstance(source, dict) else None)
    return fee != 0 and (not fee_currency or not row.get('currency') or
                         str(fee_currency).upper() != str(row['currency']).upper())


def known_trade_charges(row):
    """A reported total can be known even when its tax component is unknown."""
    raw = row.get('raw_json')
    source = raw.get('source_row', {}) if isinstance(raw, dict) else {}
    scope = source.get('fee_scope') if isinstance(source, dict) else None
    fee, tax = row.get('fee_amount'), row.get('tax_amount')
    if unconverted_ibkr_fee(row):
        return None
    if fee in (None, '') or (tax in (None, '') and scope != 'total_charges'):
        return None
    try:
        result = abs(Decimal(str(fee)))
        if scope != 'total_charges':
            result += abs(Decimal(str(tax)))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None
