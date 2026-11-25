"""Explain exchange revaluation and booking timing on the same cash balances used by NAV."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from domain.accounts.monthly.monthly_fact_book import transaction_opening_accounts
from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows
from domain.market.month_end_marks import is_month_end


ZERO = Decimal(0)
CASH_METHOD = 'opening_and_cash_movements'


def _mark(row, account):
    point = row.get('latest_known_components', {}).get(account, {})
    entry = row.get('monthly_book', {}).get('accounts', {}).get(account)
    if entry:
        if entry['method'] != CASH_METHOD or entry['status'] in {'unpriced', 'negative_cash', 'principal_only'}:
            return None
        if entry['status'] == 'unmatched':
            # A bank close may still include a confirmed outgoing remittance.
            # Only the sender's published cash balance enters this bridge.
            transit = sum((Decimal(t['native']) for t in row['monthly_book']['in_transit']
                           if t['from_account'] == account and t['currency'] == entry['currency']
                           and t.get('native') is not None and Decimal(t['native']) > 0), ZERO)
            if not transit or Decimal(entry['difference']) != -transit:
                return None
    elif account not in row.get('account_contributions', {}):
        return None
    try:
        native, rate, ratio = (Decimal(str(point[k])) for k in ('native_balance', 'fx_rate', 'owner_ratio'))
    except (KeyError, InvalidOperation):
        return None
    fixing = point.get('fx_as_of')
    if (not fixing or fixing > row['date'] or not all(x.is_finite() for x in (native, rate, ratio))
            or rate <= 0 or not ZERO <= ratio <= 1):
        return None
    return native, rate, ratio


def _account_month(prior, close, account, currency, movements, operating_ids, ledger_months, owner_changed):
    first = date.fromisoformat(close['date']).replace(day=1)
    a = _mark(prior, account)
    b = _mark(close, account)
    result = {
        'currency': currency, 'exchange': None, 'posting': None,
        'opening_fx_date': prior.get('latest_known_components', {}).get(account, {}).get('fx_as_of'),
        'closing_fx_date': close.get('latest_known_components', {}).get(account, {}).get('fx_as_of'),
    }
    if not a or not b:
        return {**result, 'reason': 'missing_cash_endpoint'}
    if (prior['latest_known_components'][account]['currency'] != currency
            or close['latest_known_components'][account]['currency'] != currency):
        return {**result, 'reason': 'currency_changed'}
    if a[2] != b[2] or owner_changed:
        return {**result, 'reason': 'ownership_changed'}
    native_change = converted = timing = ZERO
    fx_complete = timing_complete = True
    for fact_id, event, posted, fact in movements:
        event_month, posting_month = event.replace(day=1), posted.replace(day=1)
        if first not in (event_month, posting_month):
            continue
        if not fact or fact['source_evidence_status'] != 'verified' or fact['currency'] != currency:
            return {**result, 'reason': 'unverified_cash_movement'}
        value = fact['amount_cny']
        if posting_month == first:
            native_change += fact['amount']
            if value is None:
                fx_complete = False
            else:
                converted += value
        if event_month != posting_month:
            if event_month not in ledger_months:
                timing_complete = False
            elif fact_id in operating_ids:
                if value is None:
                    timing_complete = False
                else:
                    timing += value if posting_month == first else -value
    if a[0] + native_change != b[0]:
        return {**result, 'reason': 'native_movement_mismatch'}
    if fx_complete:
        result['exchange'] = (b[0] * b[1] - a[0] * a[1] - converted) * b[2]
    if timing_complete:
        result['posting'] = timing * b[2]
    if not fx_complete or not timing_complete:
        result['reason'] = 'missing_flow_rate_or_period'
    return result


def _ownership_history(nav, accounts):
    ratios = defaultdict(set)
    for iso, row in nav.items():
        month = date.fromisoformat(iso).replace(day=1)
        for code, point in row.get('latest_known_components', {}).items():
            if code in accounts:
                try:
                    ratio = Decimal(str(point.get('owner_ratio')))
                except InvalidOperation:
                    ratio = None
                ratios[month, code].add(ratio)
    return ratios


def monthly_cash_attribution(cur, nav, ledger, *, start, end):
    """Return exact CNY amounts by calendar month and account; unresolved contributions remain None."""
    closes = {date.fromisoformat(key): row for key, row in nav.items()
              if is_month_end(date.fromisoformat(key)) and start.isoformat() <= key <= end.isoformat()
              and row.get('monthly_book')}
    cash_accounts = {code for row in closes.values()
                    for code, entry in row['monthly_book']['accounts'].items() if entry['method'] == CASH_METHOD}
    if not cash_accounts:
        return {}
    cur.execute("""SELECT cashflow_id,account_code,event_date,coalesce(posting_date,event_date) AS posted
                     FROM pams_cashflow_fact WHERE account_code=ANY(%s)
                      AND (coalesce(posting_date,event_date) BETWEEN %s AND %s OR event_date BETWEEN %s AND %s)""",
                (list(cash_accounts), start, end, start, end))
    facts = cur.fetchall()
    accounts = {code for row in closes.values() for code, entry in row['monthly_book']['accounts'].items()
                if entry['method'] == CASH_METHOD and entry['currency'] != 'CNY'}
    accounts.update(r['account_code'] for r in facts if r['event_date'].replace(day=1) != r['posted'].replace(day=1))
    if not accounts:
        return {}
    ids = [r['cashflow_id'] for r in facts if r['account_code'] in accounts]
    verified = {r['fact_id']: r for r in load_cash_rows(cur, start=date.min, end=date.max,
                fact_ids=ids, base_currency='CNY')} if ids else {}
    operating_ids = {fact_id for report in ledger.values() for fact_id in report['operating_timing_ids']}
    ratios = _ownership_history(nav, accounts)
    results = {}
    for year in sorted({d.year for d in closes}):
        origin = date(year - 1, 12, 31)
        transaction_openings = transaction_opening_accounts(cur, origin)
        movements = defaultdict(list)
        for fact in facts:
            code, key = fact['account_code'], fact['cashflow_id']
            if code in accounts and not (code in transaction_openings and fact['event_date'] <= origin):
                movements[code].append((key, fact['event_date'], fact['posted'], verified.get(key)))
        for day, row in closes.items():
            if day.year != year:
                continue
            first = day.replace(day=1)
            prior = nav.get((first - timedelta(days=1)).isoformat(), {})
            results[first] = {}
            entries = row['monthly_book']['accounts']
            for account in accounts & entries.keys():
                results[first][account] = _account_month(
                    prior, row, account, entries[account]['currency'], movements[account],
                    operating_ids, ledger.keys(), len(ratios[first, account]) > 1,
                )
    return results
