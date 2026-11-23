"""Recorded equity and loan balances that change on a business event."""
from datetime import timedelta
from decimal import Decimal

from domain.investments.account_total_evidence import VERSION as ACCOUNT_TOTAL_VERSION
from domain.investments.snapshot_coverage import point_in_time_visible_sql
from domain.investments.valuation_input_evidence import current_holding_source


def recorded_components(cur, account_code, day, *, source_cache=None):
    """Validate assets and debt independently; a missing debt row does not establish zero."""
    cache = {} if source_cache is None else source_cache
    cur.execute(f"""SELECT DISTINCT ON (h.name, h.currency) h.id,h.as_of,h.currency,h.asset_liability,h.market_value
        FROM public.pams_holdings_snapshot h
        WHERE h.account_code=%s AND h.as_of<=%s AND h.major_category<>'现金'
          AND {point_in_time_visible_sql('h', '%s::date')}
        ORDER BY h.name,h.currency,h.as_of DESC,h.id DESC""", (account_code, day, day))
    parts = {'资产': [], '负债': []}
    for row in cur.fetchall():
        side = row['asset_liability']
        if side not in parts:
            return None, None
        if parts[side] is None:
            continue
        ident = row['id']
        if ident not in cache:
            cache[ident] = current_holding_source(cur, ident)['status'] == 'recorded'
        if not cache[ident] or row['currency'] != 'CNY' or row['market_value'] is None:
            parts[side] = None
        else:
            parts[side].append(row)
    return parts['资产'], parts['负债']


def recorded_event_dates(cur, code, start, end):
    # An older holding may still apply at the start of a new year. A one-day
    # observation can expire the following day, even with no new position.
    cur.execute("""SELECT as_of AS day FROM pams_holdings_snapshot WHERE account_code=%s AND as_of<=%s
        UNION SELECT business_date AS day FROM valuation_evidence WHERE account_code=%s
            AND business_date<=%s AND calculation_version=%s""", (code, end, code, end, ACCOUNT_TOTAL_VERSION))
    events = {row['day'] for row in cur.fetchall()}
    return sorted(events | {day + timedelta(days=1) for day in events if day < end} | {start, end})


def recorded_history(cur, code, dates, points, start, end):
    source_cache = {}
    zero_debt_as_of = None
    components_reconcile = True
    previous = (None, None)
    result = {}
    for day in dates:
        observed = points.get(day, {})
        point = dict(observed)
        assets, debts = recorded_components(cur, code, day, source_cache=source_cache)
        gross = sum((r['market_value'] for r in assets), Decimal(0)) if assets else None
        complete = observed.get('confirmed_components') or observed.get('complete_scope')
        if debts == [] and gross is not None and complete and gross == observed.get('net_value'):
            zero_debt_as_of = day
        elif debts != [] or observed.get('liabilities'):
            zero_debt_as_of = None

        if debts:
            debt = sum(abs(r['market_value']) for r in debts)
            debt_as_of = max(r['as_of'] for r in debts)
        else:
            debt = Decimal(0) if debts == [] and zero_debt_as_of else None
            debt_as_of = zero_debt_as_of

        if debt is not None and point.get('liabilities') is None:
            point.update(liabilities=debt, liabilities_as_of=debt_as_of,
                         liabilities_status='confirmed' if debt == 0 and debt_as_of == day else
                         'partial' if debts else 'estimated')
        if observed.get('net_value') is not None and gross is not None and debt is not None:
            components_reconcile = observed['net_value'] == gross - debt
        if point.get('net_value') is None and gross is not None and debt is not None and components_reconcile:
            point.update(net_value=gross - debt, net_value_status='estimated',
                         net_value_as_of=max(max(r['as_of'] for r in assets), debt_as_of))
        if gross is not None and debt is not None and components_reconcile and gross - debt == point.get('net_value'):
            point.update(gross_assets=gross, gross_debt=debt)
        values = (point.get('net_value'), point.get('liabilities'))
        if observed or day in (start, end) or values != previous:
            point['coverage_break'] = values == (None, None) and previous != values
            result[day] = point
        previous = values
    return result
