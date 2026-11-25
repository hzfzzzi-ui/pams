"""Native position and whole-account observations, before ownership and reporting FX."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from domain.accounts.balances.position_reference import position_reference_config
from domain.investments.holdings.account_snapshot_evidence import _complete_member_proven
from domain.investments.valuation.account_total_evidence import load as load_account_totals, prior_single_account_total
from domain.investments.holdings.position_nav_projection import load_position_nav_estimates
from domain.investments.holdings.snapshot_coverage import load_account_snapshot_coverage
from domain.market.valuation_fx import fx_rate_at, load_fx_timelines
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
        if complete and known and None not in known:
            observation.update(gross_assets=sum(assets, ZERO), gross_debt=-sum(debt, ZERO))
        if complete or debt:
            if None not in debt:
                observation.update(liabilities=-sum(debt, ZERO),
                                   liabilities_status='confirmed' if complete else 'partial')
        if observation or (known and not cash_account):
            observations[day] = observation
    return observations


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
        from domain.investments.holdings.account_position_history import load_account_trade_marks
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
            manual = _manual_observation(cfg, unit, dates[-1]) if position_reference_config(cfg) else None
            for day in dates:
                value = marks.get(day)
                actual = points.get(day, {}).get('net_value')
                if positions_only and value is not None:
                    points.setdefault(day, {}).update(position_value=value, position_value_status='partial',
                                                     position_symbols=sorted(marks.price_dates.get(day, {})))
                elif actual is not None:
                    anchored = value is not None and abs(actual - value) <= Decimal('0.005')
                elif anchored and value is not None:
                    if manual and manual[0] == day and any(d != day for d in marks.price_dates.get(day, {}).values()):
                        points.setdefault(day, {}).update(net_value=manual[1], net_value_status='estimated')
                        continue
                    points.setdefault(day, {}).update(net_value=value, net_value_status='estimated')
                if value is not None and (positions_only or actual is None and anchored):
                    source_dates = marks.price_dates.get(day, {})
                    if any(price_day != day for price_day in source_dates.values()):
                        points[day]['price_dates'] = source_dates
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


def uses_fund_account_valuation(cfg):
    return (cfg.get('account_type') == 'fund_platform' and cfg.get('reconcile_method', 'balance') != 'balance'
            and cfg.get('portfolio_value_evidence') not in ('official_broker_close', 'reconciled_trade_cash')
            and cfg.get('latest_position_evidence') != 'recorded_equity_and_debt')


def fund_account_observations(cur, code, configs, dates):
    cfg = configs[code]
    unit = cfg.get('base_currency', 'CNY')
    points = _holding_observations(cur, code, unit, dates, cash_account=False)
    _apply_totals(cur, code, configs, dates, points)
    _position_estimates(cur, code, cfg, dates, points)
    return points
