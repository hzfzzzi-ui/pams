"""Adapt original statement rows to an explicitly dated balance observation."""
from datetime import date
from decimal import Decimal, InvalidOperation


def statement_row_balance(raw):
    source = raw.get('source') if isinstance(raw, dict) else None
    statement = source.get('statement_row') if isinstance(source, dict) else None
    detail = statement.get('raw') if isinstance(statement, dict) else None
    if not isinstance(detail, dict) or detail.get('statement_template') != 'ccb_debit_xls':
        return None
    cells = detail.get('row')
    try:
        if not isinstance(cells, list) or len(cells) < 9 or cells[2] != '人民币元':
            return None
        day = date.fromisoformat(str(statement['transaction_date']))
        if str(cells[4]) != day.strftime('%Y%m%d'):
            return None
        value = Decimal(str(cells[6]).replace(',', ''))
        if not value.is_finite() or value < 0:
            return None
        return {'business_date': day.isoformat(), 'currency': statement['currency'], 'amount': str(value)}
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def verify_original_row(*, content, filename, account, raw, day):
    from domain.imports.account_import_statement import parse
    parsed = parse(bytes(content), filename, account, 'cash')
    expected = (raw.get('source') or {}).get('statement_row')
    observed = []
    for row in parsed['rows']:
        value = statement_row_balance({'source': row.get('source') or {}})
        if value is not None and value['business_date'] == day:
            observed.append((row['source'].get('row'), row['source'].get('statement_row')))
    # This statement has no time-of-day field; multiple same-day rows do not
    # prove which balance closed the day.
    return len(observed) == 1 and observed[0][1] == expected
