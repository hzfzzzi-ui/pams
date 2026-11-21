"""Dated whole-subaccount observations in its own reporting currency."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from domain.accounts.account_policy import excluded_accounts
from domain.accounts.balance_reference import historical_account_balance, load_bound_balance_references
from domain.accounts.bank_source_status import summaries
from domain.accounts.bank_statement_balance import statement_balance_history
from domain.accounts.credit_card_debt import confirmed_table_net_history, uses_table_net_reference
from domain.accounts.position_reference import historical_position_reference, position_reference_config
from domain.accounts.recorded_positions import recorded_event_dates, recorded_history
from domain.imports.source_original_read import SourceOriginals
from domain.investments.account_snapshot_evidence import _complete_member_proven
from domain.investments.account_total_evidence import load as load_account_totals, prior_single_account_total
from domain.investments.broker_account_history import broker_history_rows
from domain.investments.installment_principal import load_unbilled_principal
from domain.investments.position_nav_projection import load_position_nav_estimates
from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.investments.statement_period_nav import load_statement_period_points
from domain.investments.valuation_nav import fx_rate_at, load_fx_timelines
from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.market.quote_valuation import gold_close_join, gold_market_value


ZERO = Decimal(0)


def _manual_observation(cfg, currency, through):
    item = cfg.get('manual_balance') or {}
    if not isinstance(item, dict) or not item.get('source') or item.get('currency', currency) != currency:
        return None
    try:
        day = date.fromisoformat(str(item['as_of']))
        value = Decimal(str(item['amount']))
    except (ValueError, KeyError, InvalidOperation):
        return None
    return (day, value) if day <= through and value.is_finite() else None


def _holding_observations(cur, code, currency, dates, *, cash_account):
    """Use actual snapshot dates and the same immutable membership as the account and NAV views."""
    cur.execute(f"""SELECT h.id,h.as_of,h.source_item_id,h.currency,h.major_category,h.asset_liability,
               CASE WHEN h.asset_liability='负债' THEN -abs(h.market_value)
                    ELSE {gold_market_value('h')} END AS amount, gold_close.quote_date AS mark_date
        FROM pams_holdings_snapshot h {gold_close_join('h', 'h.as_of')}
        WHERE h.account_code=%s AND h.as_of=ANY(%s::date[]) ORDER BY h.as_of,h.id""", (code, dates))
    groups = defaultdict(list)
    for row in cur.fetchall():
        groups[row['as_of']].append(row)
    coverage = load_account_snapshot_coverage(cur, {(code, day) for day in groups})
    rates = (load_fx_timelines(cur, currency)
             if any(r['currency'] != currency for members in groups.values() for r in members) else {})
    observations = {}
    for day, members in groups.items():
        proof = coverage[code, day]
        complete = proof['status'] == 'complete' and proof['member_ids'] == {r['id'] for r in members}
        assets, debt = [], []
        stale_mark = False
        for row in members:
            ident = row['id']
            if ident not in proof['member_ids'] and (
                ident in proof.get('point_in_time_member_ids', ())
                or not row['source_item_id'] or not _complete_member_proven(cur, ident, row['source_item_id'])
            ):
                continue
            rate = fx_rate_at(rates, row['currency'], day, currency)
            value = row['amount']
            value = value * rate if value is not None and rate is not None else ZERO if value == 0 else None
            component = debt if row['asset_liability'] == '负债' or (value is not None and value < 0) else assets
            component.append(value)
            stale_mark |= bool(row['mark_date'] and row['mark_date'] < day)
        # A signed cash balance has a narrower scope than the entire set of
        # holdings. It is authoritative only for a cash-balance subaccount.
        cash_balance = (cash_account and len(members) == 1 and members[0]['major_category'] == '现金'
                        and len(assets) + len(debt) == 1)
        known = assets + debt
        status = 'estimated' if stale_mark else 'confirmed'
        observation = {}
        if known and None not in known and (complete or cash_balance):
            observation.update(net_value=sum(known, ZERO), net_value_status=status, complete_scope=complete)
        if complete or debt:
            if None not in debt:
                observation.update(liabilities=-sum(debt, ZERO),
                                   liabilities_status='confirmed' if complete else 'partial')
        if observation or (known and not cash_account):
            observations[day] = observation
    return observations


def _reconciled_cash_movements(cur, code, currency, anchors):
    """Reconstruct only between dated balances whose difference reconciles to the verified ledger."""
    days = sorted(anchors)
    if len(days) < 2 or all((end - start).days == 1 for start, end in zip(days, days[1:])):
        return {}
    flows = load_cash_rows(cur, account_codes=[code], start=days[0], end=days[-1], base_currency='CNY')
    movements = defaultdict(Decimal)
    issues = set()
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
    origins = SourceOriginals(cur)
    statements = statement_balance_history(cur, {code: (currency, card)}, dates[-1], originals=origins)
    billed = {row['as_of']: Decimal(row['amount']) for row in statements.get(code, [])}
    for (_, day), (unit, side, amount) in load_statement_period_points(cur, [code], dates).items():
        if unit == currency and (side == '负债') == card:
            billed[day] = -amount if card else amount
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
        result = {}
        if uses_table_net_reference(code, cfg):
            status = summaries(cur, [code]).get(code, {})
            for row in confirmed_table_net_history(cur, code, currency=currency, today=dates[-1],
                                                    has_duplicate_alerts=bool(status.get('duplicate_count'))):
                result[row['as_of']] = {'liabilities': max(-Decimal(row['amount']), ZERO),
                                        'liabilities_status': 'estimated'}
        for day in dates:
            members = contracts.get((day, code), {})
            remaining = sum((value[0] for key, value in members.items() if key[1] == currency), ZERO)
            if day in billed:
                # Only a billed-only statement may be supplemented. A general
                # debt snapshot can already include the remaining principal.
                result[day] = {'liabilities': max(remaining - billed[day], ZERO), 'liabilities_status': 'confirmed'}
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
    anchors.update(billed)
    ref = historical_account_balance(cfg, dates[-1], Decimal(1), account_code=code,
                                      bindings=load_bound_balance_references(cur, {code: cfg}))
    reconciliation = anchors
    if ref:
        day, amount = ref['balance_as_of'], Decimal(ref['balance_amount'])
        points.setdefault(day, {'net_value': amount, 'net_value_status': 'estimated'})
        # A dated balance reference may bound a verified ledger interval.
        # Contributed principal excludes interest and cannot certify a balance.
        if ref.get('balance_reference_basis') != 'contributed_principal' and anchors:
            reconciliation = {day: amount, **anchors}
    for day, amount in _reconciled_cash_movements(cur, code, currency, reconciliation).items():
        points.setdefault(day, {'net_value': amount, 'net_value_status': 'estimated'})
    for day, amount in anchors.items():
        points[day] = {'net_value': amount, 'net_value_status': 'confirmed'}
    return points


def _apply_totals(cur, account_code, configs, dates, points):
    """Attribute an observed platform total only after every other covered member is known."""
    totals = load_account_totals(cur, dates=dates, accounts=configs, source_accounts=[account_code])
    sibling_days = defaultdict(set)
    for (day, _), total in totals.items():
        for sibling in set(total['covered_account_codes']) - {account_code}:
            sibling_days[sibling].add(day)
    sibling_values = {}
    for code, days in sibling_days.items():
        cfg = configs.get(code)
        if not cfg:
            continue
        exact = load_account_totals(cur, dates=sorted(days), accounts={code})
        for (day, _), total in exact.items():
            if total.get('observed_total') or total.get('confirmed_empty'):
                sibling_values[code, day] = (total['currency'], Decimal(total['net_asset']))
        missing = sorted(day for day in days if (code, day) not in sibling_values)
        if missing and cfg.get('reconcile_method', 'balance') == 'balance':
            currency = cfg.get('base_currency', 'CNY')
            observed = _holding_observations(cur, code, currency, missing, cash_account=True)
            manual = _manual_observation(cfg, currency, max(missing))
            if manual and manual[0] in missing:
                observed.setdefault(manual[0], {'net_value': manual[1], 'net_value_status': 'confirmed'})
            for day in missing:
                point = observed.get(day, {})
                if point.get('net_value_status') == 'confirmed':
                    sibling_values[code, day] = (currency, point['net_value'])
    currency = configs[account_code].get('base_currency', 'CNY')
    for (day, source), total in totals.items():
        if source != account_code or total['currency'] != currency:
            continue
        amounts = [sibling_values.get((code, day)) for code in set(total['covered_account_codes']) - {account_code}]
        if any(value is None or value[0] != currency for value in amounts):
            continue
        value = Decimal(total['net_asset']) - sum((v[1] for v in amounts), ZERO)
        if value < 0 and amounts:
            continue
        point = points.setdefault(day, {})
        if point.get('net_value_status') == 'confirmed' and value != point['net_value']:
            continue
        if point.get('net_value_status') != 'confirmed':
            point.update(net_value=value, net_value_status='confirmed' if total.get('observed_total')
                         or total.get('confirmed_empty') else 'estimated')
        if total.get('confirmed_components') and not amounts:
            point['confirmed_components'] = True


def _position_estimates(cur, code, cfg, dates, points):
    """Only continue a fund NAV when the projected member set reconciles to an actual total."""
    positions_only = cfg.get('latest_position_evidence') == 'fund_snapshot_without_cash'
    unit = cfg.get('base_currency', 'CNY')
    if cfg.get('account_type') == 'fund_platform' and unit == 'CNY':
        from domain.investments.account_position_history import load_account_trade_marks
        prior = (prior_single_account_total(cur, code, dates[0], unit)
                 if not positions_only and points.get(dates[0], {}).get('net_value') is None else None)
        if not prior and not positions_only and not any(p.get('net_value') is not None for p in points.values()):
            return
        # Replay from the opening composition, returning only requested dates.
        samples = [prior[0], *dates] if prior else dates
        marks = load_account_trade_marks(cur, code, samples, positions_only=positions_only)
        if marks is not None:
            prior_value = marks.get(prior[0]) if prior else None
            anchored = prior_value is not None and abs(prior_value - prior[1]) <= Decimal('0.005')
            for day in dates:
                value = marks.get(day)
                actual = points.get(day, {}).get('net_value')
                if positions_only and value is not None:
                    points.setdefault(day, {}).update(position_value=value, position_value_status='partial')
                elif actual is not None:
                    anchored = value is not None and abs(actual - value) <= Decimal('0.005')
                elif anchored and value is not None:
                    points.setdefault(day, {}).update(net_value=value, net_value_status='estimated')
            return
    if not any(p.get('net_value') is not None for p in points.values()):
        return
    rates = load_fx_timelines(cur, unit)
    estimates = load_position_nav_estimates(cur, accounts={code: cfg}, dates=dates,
        owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal(1),
        rate_at=lambda currency, day: fx_rate_at(rates, currency, day, unit))
    members = None
    previous = None
    for day in dates:
        by_code = estimates.get(day)
        point = points.get(day, {})
        amount = by_code.get(code) if by_code else None
        basis = by_code.basis.get(code) if amount else None
        identities = basis[1] if isinstance(basis, list) and basis[0] == 'position_members' else None
        if point.get('net_value') is not None:
            members = identities if amount and abs(amount[0] - point['net_value']) <= Decimal('0.005') else None
            previous = point['net_value']
        elif identities and identities == members:
            if amount[0] != previous:
                points.setdefault(day, {}).update(net_value=amount[0], net_value_status='estimated')
                previous = amount[0]
        elif identities:
            # A missing or newly added member ends the anchored projection.
            members = None


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
            if (position_reference_config(cfg) and (cfg.get('manual_balance') or {}).get('source')
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
            row[key] = point.get(key)
            row[key + '_status'] = point.get(key + '_status', 'confirmed') if row[key] is not None else 'unavailable'
            if point.get(key + '_as_of') and point[key + '_as_of'] != day:
                row[key + '_as_of'] = point[key + '_as_of']
        rows.append(row)
    return {'account_code': account_code, 'currency': currency, 'scope': 'account',
            'start_date': start_date, 'end_date': end_date, 'series': series, 'rows': rows,
            'last_prior_date': last_prior_date, 'interpolation': 'step' if recorded_equity else 'linear'}
