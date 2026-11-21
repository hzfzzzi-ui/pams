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


def period_comparisons(rows, end):
    """Use the requested boundaries, including the prior year-end for YTD."""
    last_month = end.replace(day=1) - timedelta(days=1)
    starts = {'ytd':date(end.year, 1, 1) - timedelta(days=1),
              '1m':last_month.replace(day=min(end.day, last_month.day)),
              '1w':end - timedelta(days=7)}
    observations = {row['date']:row for row in rows if row.get('comparison_scope')}
    current = observations.get(end.isoformat())
    result = {}
    for period, start in starts.items():
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
        result[period] = item
    return result
