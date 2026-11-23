"""Match a verified funding cash fact to the daily close that includes its capital."""
from collections import defaultdict
from decimal import Decimal

from domain.brokers.ibkr import ibkr_daily_close_read
from domain.brokers.ibkr.ibkr_daily_close import _day, _document, _number, _one, _rows
from domain.brokers.ibkr.ibkr_daily_holdings import ACCOUNT
from domain.brokers.ibkr.ibkr_history_backfill import _cash


def _funding_day(statement, evidence, day, close):
    base = close['base_currency']
    marks = []
    for cash in _rows(_one(_rows(statement, 'CashTransactions'), 'cash transactions'), 'CashTransaction'):
        if cash.get('type') not in ('Deposits/Withdrawals', 'Deposits & Withdrawals'):
            continue
        evidence.scope(cash, statement, base, day, day, currency=False)
        uid, fields = _cash(cash.attrib, evidence.account, day)
        if not cash.get('transactionID') or fields['action'] not in ('deposit', 'withdraw'):
            raise ValueError('unverified broker funding')
        lines = [r for r in _rows(statement, 'StatementOfFundsLine') if
                 r.get('transactionID') == cash.get('transactionID') and r.get('activityCode') in ('DEP', 'WITH')]
        native = _one([r for r in lines if r.get('levelOfDetail') == 'Currency'], 'currency cash leg')
        translated = _one([r for r in lines if r.get('levelOfDetail') == 'BaseCurrency'], 'base cash leg')
        for leg in (native, translated):
            evidence.scope(leg, statement, base, day, day, currency=False)
            if _day(leg.get('reportDate')) != day or _day(leg.get('date')).isoformat() != fields['business_date']:
                raise ValueError('cash report date mismatch')
        amount = Decimal(fields['net_cash'])
        if (fields['currency'] != native.get('currency') or _number(native, 'amount') != amount
                or translated.get('currency') != base):
            raise ValueError('cash currency leg mismatch')
        base_amount = _number(translated, 'amount')
        if base_amount * amount <= 0:
            raise ValueError('cash base amount direction mismatch')
        marks.append((uid, fields['business_date'], fields['currency'], amount, day, base, base_amount))
    change = _one(_rows(statement, 'ChangeInNAV'), 'NAV change')
    if sum((m[-1] for m in marks), Decimal(0)) != _number(change, 'depositsWithdrawals'):
        raise ValueError('cash flow not reproduced in published close')
    return marks


def funding_marks(cur, cash_ids):
    if not cash_ids:
        return {}
    cur.execute("""SELECT f.cashflow_id, i.source_uid, f.event_date, f.currency, f.amount,
                            i.raw_fields->'official_attributes'->>'reportDate' AS report_hint
                     FROM pams_cashflow_fact f JOIN source_items i USING(source_item_id)
                    WHERE f.cashflow_id=ANY(%s) AND f.account_code=%s""", (list(cash_ids), ACCOUNT))
    facts = cur.fetchall()
    if not facts:
        return {}
    # An archived report-date hint only bounds uncertainty; it never certifies an amount.
    result = {}
    for fact in facts:
        try:
            result[fact['cashflow_id']] = (_day(fact['report_hint']), None, None)
        except (ValueError, TypeError):
            continue
    source = ibkr_daily_close_read.read_published_portfolio_series(cur)
    if not source or source.get('status') != 'ok':
        return result
    cur.execute('SELECT content FROM ibkr_source_originals WHERE sha256=%s', (source['source_sha256'],))
    original = cur.fetchone()
    if not original:
        return result
    evidence, _, error = _document(bytes(original['content']).decode('utf8'), source['account_id'], source['source_sha256'])
    if error:
        return result
    verified = defaultdict(list)
    for day, statements in evidence.by_day.items():
        close = source['daily_closes'].get(day.isoformat(), {})
        if len(statements) != 1 or close.get('identity_evidence', {}).get('status') != 'ok':
            continue
        try:
            if _day(statements[0].get('fromDate')) != day:
                continue
            marks = _funding_day(statements[0], evidence, day, close)
        except (ValueError, KeyError, ArithmeticError):
            continue
        for uid, event, currency, amount, booked, base, translated in marks:
            verified[uid].append((event, currency, amount, booked, base, translated))
    for f in facts:
        marks = verified[f['source_uid']]
        if len(marks) == 1 and marks[0][:3] == (f['event_date'].isoformat(), f['currency'], f['amount']):
            result[f['cashflow_id']] = marks[0][3:]
    return result
