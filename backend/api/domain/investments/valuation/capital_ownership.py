"""Dated ownership of pooled accounts using reconciled personal funding legs."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from domain.imports.sources.source_original_read import SourceOriginals
from domain.ledger.cashflow_read_v2 import load_cash_rows


def contribution_ratios(events, personal_legs, dates, through, personal_total, account_total):
    """Apply signed principal changes on brokerage receipt, including partial contributions."""
    broker_legs = defaultdict(Decimal)
    for event in events:
        if event['trade_type'] in {'deposit', 'withdraw'} and event['trade_date'] <= through:
            key = event['trade_date'], event['trade_type']
            broker_legs[key] += event['net_cash']
    if sum(broker_legs.values()) != account_total or sum(personal_legs.values()) != personal_total:
        return {}
    for key, amount in personal_legs.items():
        if key not in broker_legs or amount * broker_legs[key] <= 0 or abs(amount) > abs(broker_legs[key]):
            return {}
    personal = total = Decimal(0)
    result = {}
    wanted = set(dates)
    # Subsequent reconciled trades establish continuity until the next
    # unidentified capital flow, even if it nets to zero with another flow.
    next_flow = min((event['trade_date'] for event in events if event['trade_date'] > through
                     and event['trade_type'] in {'deposit', 'withdraw'}), default=None)
    verified_through = max((event['trade_date'] for event in events), default=through)
    for day in sorted(wanted | {day for day, _ in broker_legs}):
        if day > max(through, verified_through) or (next_flow is not None and day >= next_flow):
            break
        for kind in ('deposit', 'withdraw'):
            key = day, kind
            total += broker_legs.get(key, 0)
            personal += personal_legs.get(key, 0)
        if total < 0 or not 0 <= personal <= total:
            return {}
        if day in wanted and total > 0:
            result[day] = personal / total
    return result


def funding_history(cur, account_code, cfg):
    """Show verified daily capital movements, bounded by the reconciled funding period."""
    from domain.investments.trades.trade_read_v2 import load_trade_rows

    history = cfg.get('funding_history') or {}
    if history.get('complete_personal_flows') is not True:
        return []
    try:
        through = date.fromisoformat(str(history.get('as_of')))
    except ValueError:
        return []
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS ready")
    if not cur.fetchone()['ready']:
        return []
    events = [row for row in load_trade_rows(cur, account_code=account_code, start=date.min, end=through)
              if row['trade_type'] in {'deposit', 'withdraw'}]
    if any(row['source_evidence_status'] != 'verified' or row['currency'] != 'CNY' for row in events):
        return []
    daily = defaultdict(Decimal)
    for row in events:
        daily[row['trade_date']] += row['net_cash']
    ratios = load_contribution_ratios(cur, cfg, events, sorted(daily))
    if set(daily) != set(ratios):
        return []
    result = []
    capital = personal = Decimal(0)
    for day, change in sorted(daily.items()):
        capital += change
        balance = (capital * ratios[day]).quantize(Decimal('0.01'))
        mine = balance - personal
        result.append({'date': day.isoformat(), 'personal_change': str(mine), 'other_change': str(change - mine),
                       'personal_balance': str(balance), 'account_balance': str(capital)})
        personal = balance
    return result


def load_contribution_ratios(cur, cfg, events, dates, *, unverified_from=None):
    """Revalidate the reviewed bank allocations against their original facts and broker totals."""
    try:
        history = cfg['funding_history']
        through = date.fromisoformat(str(history['as_of']))
        personal_total = Decimal(str(cfg['personal_capital']))
        account_total = Decimal(str(cfg['capital_base']))
        if (not personal_total.is_finite() or not account_total.is_finite()
                or history['complete_personal_flows'] is not True or not cfg['funding_source']):
            return {}
        receipts = {str(UUID(source)): date.fromisoformat(str(day))
                    for source, day in history['bank_receipts'].items()}
    except (AttributeError, InvalidOperation, ValueError, KeyError, TypeError):
        return {}
    if (not receipts or any(day > through for day in receipts.values())
            or (unverified_from is not None and unverified_from <= through)):
        return {}
    cur.execute("""SELECT cashflow_id,source_item_id FROM pams_cashflow_fact
        WHERE account_code=%s AND source_item_id=ANY(%s::uuid[]) ORDER BY cashflow_id""",
        (cfg['funding_source'], list(receipts)))
    refs = cur.fetchall()
    if len(refs) != len(receipts) or {str(r['source_item_id']) for r in refs} != receipts.keys():
        return {}
    originals = SourceOriginals(cur)
    legs = defaultdict(Decimal)
    for ref in refs:
        received = receipts[str(ref['source_item_id'])]
        rows = load_cash_rows(cur, start=date.min, end=through, base_currency='CNY',
                              account_codes=[cfg['funding_source']], fact_id=ref['cashflow_id'], originals=originals)
        if len(rows) != 1 or rows[0]['currency'] != 'CNY' or rows[0]['source_evidence_status'] != 'verified':
            return {}
        row = rows[0]
        amount = -row['amount']
        if amount == 0 or (amount > 0 and received < row['transaction_date']) or (amount < 0 and received > row['transaction_date']):
            return {}
        legs[received, 'deposit' if amount > 0 else 'withdraw'] += amount
    return contribution_ratios(events, legs, dates, through, personal_total, account_total)


def account_contribution_ratios(cur, code, cfg, dates):
    if not cfg.get('funding_history'):
        return None
    from domain.investments.trades.nav_trade_source_v2 import _read_event_prefix
    events, gap = _read_event_prefix(cur, code)
    return load_contribution_ratios(cur, cfg, events, dates, unverified_from=gap)


def position_ownership(cur, accounts, dates, owner_ratio):
    from domain.accounts.account_overview import _load_accounts_config
    configs = _load_accounts_config().get('accounts', {})
    schedules = {}
    for code in accounts:
        dated = account_contribution_ratios(cur, code, configs.get(code, {}), dates)
        schedules[code] = dict.fromkeys(dates, owner_ratio(code)) if dated is None else dated
    return schedules


def monthly_profit_reallocations(history, nav_by_date, code):
    """Allocate only accumulated gains or losses when net-contribution shares change."""
    result = {}
    previous_capital = personal = Decimal(0)
    for record in history:
        day = date.fromisoformat(record['date'])
        capital = Decimal(record['account_balance'])
        new_personal = Decimal(record['personal_balance'])
        if previous_capital and capital and day.isoformat() in nav_by_date:
            before, after = personal / previous_capital, new_personal / capital
            if after != before:
                month = day.replace(day=1)
                prior = nav_by_date.get((day - timedelta(days=1)).isoformat(), {})
                point = prior.get('latest_known_components', {}).get(code, {})
                current = nav_by_date[day.isoformat()].get('latest_known_components', {}).get(code, {})
                if (point.get('currency') != 'CNY' or point.get('native_balance') is None
                        or point.get('owner_ratio') is None or current.get('owner_ratio') is None
                        or Decimal(str(point['owner_ratio'])) != before
                        or Decimal(str(current['owner_ratio'])) != after):
                    result[month] = None
                elif result.get(month, 0) is not None:
                    pooled_pnl = Decimal(str(point['native_balance'])) - previous_capital
                    result[month] = result.get(month, Decimal(0)) + (after - before) * pooled_pnl
        previous_capital, personal = capital, new_personal
    return result


def read_monthly_profit_reallocations(cur, nav, excluded_accounts):
    from domain.accounts.account_overview import _load_accounts_config
    months = defaultdict(dict)
    active = {code for row in nav.values() for code in (row.get('monthly_book') or {}).get('accounts', {})}
    for code, cfg in _load_accounts_config().get('accounts', {}).items():
        if code not in active or code in excluded_accounts or not cfg.get('funding_history'):
            continue
        history = funding_history(cur, code, cfg)
        for month, value in monthly_profit_reallocations(history, nav, code).items():
            months[month][code] = value
    return months
