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


def statement_opening_balance(statement, *, target, currency, account_tail):
    """Infer the prior closing balance from an account-identified complete report."""
    from datetime import timedelta
    if (statement.statement_start != target + timedelta(days=1) or not statement.statement_end
            or statement.statement_end < statement.statement_start or statement.account_tail != account_tail):
        raise ValueError('账单账户或覆盖期不符')
    rows = list(statement.rows)
    if not rows or any(row.get('currency') != currency or row.get('balance') is None
                       or row.get('amount') is None or not statement.statement_start <= row['transaction_date'] <= statement.statement_end
                       for row in rows):
        raise ValueError('账单缺少可核对的同币种余额流水')
    rows.sort(key=lambda row: (row['transaction_date'], (row.get('raw') or {}).get('transaction_time') or ''))
    for prev, row in zip(rows, rows[1:]):
        if prev['transaction_date'] == row['transaction_date'] and (
                not (prev.get('raw') or {}).get('transaction_time') or
                not (row.get('raw') or {}).get('transaction_time') or
                (prev.get('raw') or {}).get('transaction_time') == (row.get('raw') or {}).get('transaction_time')):
            raise ValueError('同日流水顺序不明')
        if row['balance'] - prev['balance'] != row['amount']:
            raise ValueError('账单相邻余额无法衔接')
    opening = rows[0]['balance'] - rows[0]['amount']
    if not opening.is_finite() or opening < 0:
        raise ValueError('账单推算余额无效')
    return opening


def verify_original_opening(*, content, filename, account, day, currency):
    """Reparse the configured original report; never trust supplied values."""
    import re
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import pdfplumber
    from collectors.bank_statement_parser import parse_statement, _parse_icbc_datetime, _parse_icbc_currency
    from domain.imports.account_import_statement import profiles

    configs = profiles().get(account, [])
    if len(configs) != 1 or configs[0].get('statement_template') != 'icbc_debit_pdf':
        raise ValueError('该账单来源尚不支持历史期初核验')
    cfg = configs[0]
    expected = str(cfg.get('account_tail') or '')
    if not re.fullmatch(r'\d{4}', expected) or Path(filename).suffix.lower() != '.pdf':
        raise ValueError('账单账户身份不明确')
    with TemporaryDirectory(prefix='pams-opening-') as directory:
        path = Path(directory) / 'statement.pdf'
        path.write_bytes(bytes(content))
        with pdfplumber.open(path, password=str(cfg.get('pdf_password') or '')) as pdf:
            if not pdf.pages or len(pdf.pages) > 50:
                raise ValueError('账单页数无效')
            header = pdf.pages[0].extract_text() or ''
            match = re.search(r'卡号\s*(\d+)', header)
            if not match or not match.group(1).endswith(expected):
                raise ValueError('原件卡号与账户不符')
            expected_rows = 0
            tables = 0
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if not table or not table[0] or '交易日期' not in str(table[0][0] or ''):
                        continue
                    tables += 1
                    for row in table[1:]:
                        if not any(row):
                            continue
                        if len(row) < 13 or _parse_icbc_datetime(row[0])[0] is None:
                            raise ValueError('账单存在无法核验的流水行')
                        row_currency = _parse_icbc_currency(row[4])
                        if not row_currency or not row[8] or not row[9]:
                            raise ValueError('账单流水币种或余额缺失')
                        if row_currency == currency:
                            expected_rows += 1
            if not tables or not expected_rows:
                raise ValueError('账单缺少可核对的流水')
        statement = parse_statement(path, cfg, since=None)
    if len(statement.rows) != expected_rows:
        raise ValueError('账单流水解析不完整')
    return statement_opening_balance(statement, target=day, currency=currency, account_tail=expected)


def credit_statement_bridge(first, following, *, target, currency, first_previous, following_previous):
    """Reconstruct a dated credit liability from two consecutive statements.

    Cashflow amounts are signed from the customer's cash perspective.  Entries
    posted in the following statement can belong to an earlier transaction day.
    The result remains an estimated point observation, not a complete account.
    """
    from datetime import timedelta
    if (not first.statement_start or not first.statement_end or not following.statement_start
            or not following.statement_end or first.account_tail != following.account_tail
            or first.statement_start > target or first.statement_end < target
            or following.statement_start != first.statement_end + timedelta(days=1)
            or following.statement_end < following.statement_start
            or first.statement_balance is None or following.statement_balance is None
            or first.statement_balance != following_previous):
        raise ValueError('信用卡账单账户、周期或上期余额不连续')
    for statement in (first, following):
        for row in statement.rows:
            if (row.get('currency') != currency or row.get('amount') is None
                    or row.get('transaction_date') is None or row.get('posting_date') is None
                    or not statement.statement_start <= row['posting_date'] <= statement.statement_end
                    or row['transaction_date'] > row['posting_date']):
                raise ValueError('信用卡账单流水日期或币种不完整')
    if first_previous - sum((r['amount'] for r in first.rows), Decimal(0)) != first.statement_balance:
        raise ValueError('信用卡账单金额与逐笔流水不衔接')
    value = first.statement_balance
    value += sum((r['amount'] for r in first.rows if r['transaction_date'] > target), Decimal(0))
    value -= sum((r['amount'] for r in following.rows if r['transaction_date'] <= target), Decimal(0))
    if not value.is_finite() or value < 0:
        raise ValueError('信用卡历史余额无效')
    return value


def verify_original_credit_bridge(*, originals, account, day, currency):
    """Read two original monthly reports through the configured source adapter."""
    import re
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import pdfplumber
    from collectors.bank_statement_parser import parse_statement
    from domain.imports.account_import_statement import profiles

    configs = profiles().get(account, [])
    if (len(configs) != 1 or configs[0].get('statement_template') != 'guangfa_credit_pdf'
            or configs[0].get('currency') != currency or len(originals) != 2):
        raise ValueError('信用卡原始账单来源不支持')
    cfg = configs[0]
    tail = str(cfg.get('card_tail') or '')
    if not re.fullmatch(r'\d{4}', tail):
        raise ValueError('信用卡账户身份不明确')
    parsed = []
    with TemporaryDirectory(prefix='pams-credit-bridge-') as directory:
        for index, content in enumerate(originals):
            path = Path(directory) / f'{index}.pdf'
            path.write_bytes(bytes(content))
            with pdfplumber.open(path) as pdf:
                if not pdf.pages or len(pdf.pages) > 50:
                    raise ValueError('信用卡账单页数无效')
                summaries = []
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        for pos, row in enumerate(table):
                            if not row or ''.join(str(c or '') for c in row).replace('\n', '').find('上期账单金额') < 0:
                                continue
                            for values in table[pos + 1:]:
                                if values and str(values[0] or '').strip() == tail:
                                    if len(values) < 5:
                                        raise ValueError('信用卡账单汇总不完整')
                                    try:
                                        summaries.append((Decimal(str(values[2]).replace(',', '')),
                                                          Decimal(str(values[4]).replace(',', ''))))
                                    except (ValueError, InvalidOperation) as exc:
                                        raise ValueError('信用卡账单汇总金额无效') from exc
                if len(summaries) != 1 or not all(v.is_finite() for v in summaries[0]):
                    raise ValueError('信用卡账单缺少唯一的账户汇总')
            statement = parse_statement(path, cfg, since=None)
            if statement.account_tail != tail or statement.statement_balance != summaries[0][0]:
                raise ValueError('信用卡账单汇总与账户不符')
            parsed.append((statement, summaries[0][1]))
    return credit_statement_bridge(parsed[0][0], parsed[1][0], target=day, currency=currency,
                                   first_previous=parsed[0][1], following_previous=parsed[1][1])
