"""Dated whole-subaccount observations in its own reporting currency."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.accounts.account_policy import excluded_accounts
from domain.accounts.balance_reference import historical_account_balance, load_bound_balance_references
from domain.accounts.dated_valuation import AccountValuation, posted_card_valuations, statement_valuations
from domain.accounts.position_valuation import (
    _apply_totals, _holding_observations, _manual_observation, _position_estimates, fund_account_observations,
    uses_fund_account_valuation,
)
from domain.accounts.position_reference import historical_position_reference, position_reference_config
from domain.accounts.recorded_positions import recorded_event_dates, recorded_history
from domain.investments.broker_account_history import broker_history_rows
from domain.investments.installment_principal import load_unbilled_principal
from domain.ledger.cashflow_read_v2 import load_cash_rows


ZERO = Decimal(0)


def _reconciled_cash_movements(cur, code, currency, anchors, conflicts=()):
    """Reconstruct only between dated balances whose difference reconciles to the verified ledger."""
    days = sorted(anchors)
    if len(days) < 2 or all((end - start).days == 1 for start, end in zip(days, days[1:])):
        return {}
    flows = load_cash_rows(cur, account_codes=[code], start=days[0], end=days[-1], base_currency='CNY')
    movements = defaultdict(Decimal)
    issues = set(conflicts)
    for row in flows:
        day = row['transaction_date']
        if row['source_evidence_status'] != 'verified' or row['currency'] != currency:
            issues.add(day)
        else:
            movements[day] += row['amount']
    points = {}
    for first, last in zip(days, days[1:]):
        if any(first < day <= last for day in issues):
            continue
        total = anchors[first]
        interval = {}
        for day in sorted(day for day in movements if first < day <= last):
            total += movements[day]
            interval[day] = total
        if total == anchors[last]:
            points.update(interval)
    return points


def _cash_observations(cur, code, cfg, currency, dates, holdings, card):
    statements = statement_valuations(cur, {code: (currency, card)}, dates)
    billed = {day: obs for (_, day), obs in statements.items() if obs.amount is not None}
    conflicts = {day for (_, day), obs in statements.items() if obs.amount is None}
    anchors = {day: point['net_value'] for day, point in holdings.items()
               if point.get('net_value_status') == 'confirmed'}
    manual = _manual_observation(cfg, currency, dates[-1])
    if manual and card:
        # Configured debt is a magnitude; the signed account convention is negative.
        anchors.setdefault(manual[0], -abs(manual[1]))
    elif manual:
        anchors.setdefault(*manual)
    if card:
        contracts = load_unbilled_principal(cur, [code], dates)
        result = {day: {'liabilities': max(-value.amount, ZERO), 'liabilities_status': 'estimated'}
                  for day, value in posted_card_valuations(cur, code, cfg, currency, dates[-1]).items()}
        for day in dates:
            members = contracts.get((day, code), {})
            remaining = sum((value[0] for key, value in members.items() if key[1] == currency), ZERO)
            observed = holdings.get(day, {})
            if (day in conflicts or day in billed and observed.get('net_value_status') == 'confirmed'
                    and not billed[day].agrees_with_balance(observed['net_value'], remaining)):
                result[day] = {'coverage_break': True}
            elif day in billed:
                # Only a billed-only statement may be supplemented. A general
                # debt snapshot can already include the remaining principal.
                result[day] = {'liabilities': max(-billed[day].including_unbilled(remaining).amount, ZERO), 'liabilities_status': 'confirmed'}
            elif day in anchors:
                whole = holdings.get(day, {}).get('complete_scope')
                result[day] = {'liabilities': max(-anchors[day], ZERO),
                               'liabilities_status': 'partial' if remaining and not whole else 'confirmed'}
            elif holdings.get(day, {}).get('liabilities') is not None:
                item = holdings[day]
                result[day] = {k: item[k] for k in ('liabilities', 'liabilities_status')}
            elif members and any(value[1] == day for value in members.values()):
                result[day] = {'liabilities': remaining, 'liabilities_status': 'partial'}
        return result

    points = dict(holdings)
    conflicts.update(day for day, obs in billed.items() if holdings.get(day, {}).get('net_value_status') == 'confirmed'
                     and not obs.agrees_with_balance(holdings[day]['net_value']))
    anchors.update({day: obs.amount for day, obs in billed.items()})
    ref = historical_account_balance(cfg, dates[-1], Decimal(1), account_code=code,
                                      bindings=load_bound_balance_references(cur, {code: cfg}))
    for day in conflicts:
        anchors.pop(day, None)
    reconciliation = anchors
    if ref:
        day, amount = ref['balance_as_of'], Decimal(ref['balance_amount'])
        points.setdefault(day, {'net_value': amount, 'net_value_status': 'estimated'})
        # A dated balance reference may bound a verified ledger interval.
        # Contributed principal excludes interest and cannot certify a balance.
        if ref.get('balance_reference_basis') != 'contributed_principal' and anchors:
            reconciliation = {day: amount, **anchors}
    for day, amount in _reconciled_cash_movements(cur, code, currency, reconciliation, conflicts).items():
        points.setdefault(day, {'net_value': amount, 'net_value_status': 'estimated'})
    for day, amount in anchors.items():
        points[day] = {'net_value': amount, 'net_value_status': 'confirmed'}
    for day in conflicts:
        points[day] = {'coverage_break': True}
    return points


def read_account_history(cur, *, account_code, account_config, start_date, end_date):
    configs = account_config.get('accounts', {})
    cfg = configs.get(account_code)
    if account_code in excluded_accounts(account_config) or (cfg or {}).get('exclude_from_reconcile'):
        return None
    if cfg is None:
        cur.execute('SELECT base_currency,is_asset FROM pams_accounts WHERE account_code=%s AND active', (account_code,))
        registered = cur.fetchone()
        if not registered:
            return None
        cfg = {'base_currency': registered['base_currency'], 'account_type': 'funding' if registered['is_asset'] else 'liability'}
        configs = {**configs, account_code: cfg}
    if cfg.get('manual_balance') and _manual_observation(cfg, cfg.get('base_currency', 'CNY'), date.max) is None:
        cfg = {**cfg, 'manual_balance': {}}
    dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
    currency = str(cfg.get('base_currency') or 'CNY').upper()
    account_type = cfg.get('account_type')
    card = account_type in {'liability', 'credit_card'}
    mode = cfg.get('portfolio_value_evidence')
    recorded_equity = cfg.get('latest_position_evidence') == 'recorded_equity_and_debt'
    dual = mode == 'official_broker_close' or recorded_equity
    cash = cfg.get('reconcile_method', 'balance') == 'balance'
    if mode == 'official_broker_close':
        points = {row['date']: row for row in broker_history_rows(
            cur, account_code=account_code, currency=currency, start_date=start_date, end_date=end_date)}
    elif uses_fund_account_valuation(cfg):
        points = fund_account_observations(cur, account_code, configs, dates)
    else:
        if recorded_equity:
            dates = recorded_event_dates(cur, account_code, start_date, end_date)
        points = _holding_observations(cur, account_code, currency, dates, cash_account=cash)
        if cash:
            points = _cash_observations(cur, account_code, cfg, currency, dates, points, card)
        elif mode == 'reconciled_trade_cash':
            from domain.investments.broker_account_history import reconstructed_history_rows
            for point in reconstructed_history_rows(cur, account_code=account_code, currency=currency,
                                                    start_date=start_date, end_date=end_date):
                if points.get(point['date'], {}).get('net_value_status') != 'confirmed':
                    points.setdefault(point['date'], {}).update(point)
        if not card:
            _apply_totals(cur, account_code, configs, dates, points)
        if recorded_equity and currency == 'CNY':
            points = recorded_history(cur, account_code, dates, points, start_date, end_date)
        elif not cash and not recorded_equity and mode != 'reconciled_trade_cash':
            _position_estimates(cur, account_code, cfg, dates, points)
    if (not cash and not recorded_equity and mode not in ('reconciled_trade_cash', 'official_broker_close')
            and position_reference_config(cfg) and (cfg.get('manual_balance') or {}).get('source')
            and not any(point.get('net_value') is not None for point in points.values())):
        reference = historical_position_reference(cfg, end_date, Decimal(1))
        if reference:
            points.setdefault(reference['balance_as_of'], {}).update(
                net_value=Decimal(reference['balance_amount']), net_value_status='partial')
    has_debt = dual or (not cash and any(p.get('liabilities', ZERO) and p['liabilities'] > 0 for p in points.values()))
    series = ['liabilities'] if card else ['net_value', 'liabilities'] if has_debt else ['net_value']
    if any(point.get('position_value') is not None for point in points.values()):
        series.append('position_value')
    rows = []
    last_prior_date = None
    for day, point in sorted(points.items()):
        if day > end_date or (not point.get('coverage_break') and all(point.get(key) is None for key in series)):
            continue
        if day < start_date:
            if not point.get('coverage_break'):
                last_prior_date = day
            continue
        row = {'date': day}
        for key in series:
            observation = AccountValuation.from_measurement(day, currency, point, key)
            row[key] = observation.amount
            row[key + '_status'] = observation.quality
            if observation.observed_on != day:
                row[key + '_as_of'] = observation.observed_on
        rows.append(row)
    return {'account_code': account_code, 'currency': currency, 'scope': 'account',
            'start_date': start_date, 'end_date': end_date, 'series': series, 'rows': rows,
            'last_prior_date': last_prior_date, 'interpolation': 'step' if recorded_equity else 'linear'}
