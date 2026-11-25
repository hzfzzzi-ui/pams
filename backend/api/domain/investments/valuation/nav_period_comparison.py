"""Endpoint comparisons for the personal net worth chart."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


class DisplayContributions(dict):
    """Existing account amounts and dates, with the scope actually valued."""
    def __init__(self):
        super().__init__()
        self.basis = {}


def position_member(row):
    # The current portfolio view exposes a presentation ID alongside the
    # snapshot's canonical asset ID. Historical rows expose the latter directly.
    master = row.get('holding_snapshot_asset_id', row.get('asset_id'))
    symbol = row.get('symbol')
    identity = (f'asset:{master}' if master else f'symbol:{symbol}' if symbol else
                f"name:{row.get('name') or row.get('asset_name') or ''}")
    side = row.get('asset_liability') or ('负债' if row.get('asset_type') == 'liability' else '资产')
    return str(side), str(row.get('currency') or ''), identity


def comparison_scope(row):
    complete = row.get('net_worth') is not None
    value = row.get('net_worth') if complete else row.get('available_net_worth')
    try:
        known = value is not None and Decimal(str(value)).is_finite()
    except (ValueError, InvalidOperation):
        known = False
    if not known:
        return None
    scope = (complete, sorted(row.get('available_accounts', ())),
             sorted(row.get('unavailable_accounts', ())), sorted(row.get('source_evidence_unavailable_accounts', ())),
             row.get('_comparison_basis', {}))
    if '_unanchored_loan_changes' in row:
        scope += ('personal_loan_principal_unverified', row['_unanchored_loan_changes'])
    reference = (row.get('personal_loan_coverage') or {}).get('principal_reference')
    if reference:
        scope += ('personal_loan_reference', reference['opening_date'], sorted(reference['native']))
    encoded = json.dumps(scope, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def terminal_comparison_basis(row, account_codes, account_values, net):
    """Identify what the replacing terminal total values, independently of historical prices."""
    opaque = {code:'terminal_aggregate' for code in account_codes}
    if not account_codes or set(account_values) != account_codes:
        return opaque
    try:
        parts = [Decimal(str(value)) for value in account_values.values()]
        if any(not value.is_finite() for value in parts) or abs(sum(parts) - net) > Decimal('0.01'):
            return opaque
    except (TypeError, ValueError, InvalidOperation):
        return opaque
    sources = getattr(account_values, 'basis', {})
    if set(sources) == account_codes:
        return dict(sources)
    # Older published totals may provide the complete account breakdown without
    # source types. Only a separate same-day, complete reconstruction proves those.
    assembled = row.get('_comparison_basis', {})
    if (row.get('net_worth') is not None and not row.get('estimated_accounts')
            and not row.get('unavailable_accounts') and not row.get('source_evidence_unavailable_accounts')
            and set(assembled) == set(row.get('available_accounts', ())) == account_codes):
        return dict(assembled)
    return opaque


def apply_terminal_breakdown(row, basis, breakdown, dated_values):
    """Keep a replacing total's contributions and dating in step with its actual value."""
    row['_comparison_basis'] = basis
    row['available_accounts'] = sorted(basis)
    old = row.get('_account_contributions', {})
    if not breakdown or any(kind == 'terminal_aggregate' for kind in basis.values()):
        row['_account_contributions'] = {}
        return
    amounts = {code:Decimal(str(value)) for code,value in breakdown.items()}
    row['_account_contributions'] = amounts
    day = row['date']
    if getattr(breakdown, 'basis', {}):
        supported = {
            code for code, (amount, start, end) in dated_values.items()
            if code in amounts and start == end == day and abs(amount - amounts[code]) <= Decimal('0.01')
        }
        row.setdefault('_dated_cash_accounts', set()).update(
            code for code in supported if basis[code] == 'cash_balance'
        )
    else:
        # An archived value with no dates for its components requires matching,
        # independently assembled same-day amounts, as well as matching account scope.
        supported = {code for code, amount in amounts.items()
                     if code in old and abs(amount - Decimal(str(old[code]))) <= Decimal('0.01')}
    row['_undated_terminal_accounts'] = set(amounts) - supported


def _eligible_contributions(row):
    """Return dated personal values with independently identified, non-overlapping scope."""
    if not row:
        return {}
    result = {}
    blocked = set(row.get('source_evidence_unavailable_accounts', ())) | set(row.get('_undated_terminal_accounts', ()))
    unavailable = set(row.get('unavailable_accounts', ()))
    as_of = row['date'].isoformat()
    amounts = row.get('_account_contributions', {})
    for code in row.get('available_accounts', ()):
        source = row.get('_comparison_basis', {}).get(code)
        if code in blocked or code not in amounts or not source:
            continue
        kind = source[0] if isinstance(source, (list, tuple)) else source
        scope = {code}
        if kind == 'account_total':
            scope = set(source[1])
            if not scope or code not in scope or scope & blocked:
                continue
        elif kind in ('cash_balance', 'cash_anchor'):
            if code not in row.get('_dated_cash_accounts', ()):
                continue
            source = 'cash_balance'
        elif kind in ('position_members', 'net_value', 'posted_flow_net'):
            dates = row.get('latest_known_components', {}).get(code, {})
            if ((kind == 'position_members' and not source[1])
                    or dates.get('from') != as_of or dates.get('through') != as_of):
                continue
        elif kind == 'complete_positions':
            if code in unavailable or code in row.get('estimated_accounts', ()):
                continue
        elif kind == 'official_base_nav_at_internal_historical_fx':
            close = row.get('ibkr_daily_close') or {}
            if close.get('status') != 'ok' or close.get('as_of') != as_of:
                continue
        else:
            continue
        try:
            amount = Decimal(str(amounts[code]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount.is_finite():
            result[code] = (source, scope, amount)
    # A parent total and a separately emitted covered member cannot both
    # establish a unique contribution, even if their amounts happen to agree.
    return {code:entry for code,entry in result.items()
            if not any(entry[1] & other[1] for key,other in result.items() if code != key)}


def _comparable_accounts(opening, closing, labels):
    result = {'status':'unavailable', 'opening':None, 'closing':None, 'change':None,
              'change_rate':None, 'account_codes':[], 'account_labels':[], 'estimated_account_codes':[]}
    before, after = _eligible_contributions(opening), _eligible_contributions(closing)
    opening_loans = (opening or {}).get('_unanchored_loan_changes', {})
    closing_loans = (closing or {}).get('_unanchored_loan_changes', {})
    affected_cash = {code for code in opening_loans.keys() | closing_loans.keys()
                     if opening_loans.get(code) != closing_loans.get(code)}
    codes = sorted(code for code in before.keys() & after.keys()
                   if before[code][:2] == after[code][:2] and not before[code][1] & affected_cash)
    if not codes:
        return result
    first, last = (sum(values[code][2] for code in codes) for values in (before, after))
    estimated = set(opening.get('estimated_accounts', ())) | set(closing.get('estimated_accounts', ()))
    result.update(status='ok', opening=str(first), closing=str(last), change=str(last-first),
                  change_rate=str((last-first)/first) if first > 0 else None, account_codes=codes,
                  account_labels=[labels.get(code) or code for code in codes],
                  estimated_account_codes=sorted(estimated.intersection(codes)))
    return result


def period_starts(end):
    last_month = end.replace(day=1) - timedelta(days=1)
    return {'ytd':date(end.year, 1, 1) - timedelta(days=1),
            '1m':last_month.replace(day=min(end.day, last_month.day)),
            '1w':end - timedelta(days=7)}


def period_comparisons(rows, end, account_rows=(), account_labels=None):
    """Use the requested boundaries, including the prior year-end for YTD."""
    observations = {row['date']:row for row in rows if row.get('comparison_scope')}
    by_day = {row['date'].isoformat():row for row in account_rows}
    current = observations.get(end.isoformat())
    result = {}
    for period, start in period_starts(end).items():
        opening = observations.get(start.isoformat())
        item = {'start':start.isoformat(), 'end':end.isoformat(), 'status':'missing_endpoint',
                'change':None, 'change_rate':None}
        if opening and current:
            if opening['comparison_scope'] != current['comparison_scope']:
                item['status'] = 'different_coverage'
            else:
                first, last = (Decimal(row['netWorth'] if row['netWorth'] is not None else row['availableNetWorth'])
                               for row in (opening, current))
                delta = last - first
                item.update(status='ok', change=str(delta), change_rate=str(delta / first) if first > 0 else None)
        item['comparable_accounts'] = _comparable_accounts(
            by_day.get(start.isoformat()) if opening else None,
            by_day.get(end.isoformat()) if current else None, account_labels or {},
        )
        result[period] = item
    return result
