"""Dated native account values and their personal, non-overlapping NAV contributions."""
from bisect import bisect_right
from datetime import timedelta
from decimal import Decimal

from domain.accounts.account_history import read_account_history
from domain.accounts.account_policy import excluded_accounts
from domain.accounts.balance_reference import historical_account_balance, load_bound_balance_references
from domain.accounts.dated_valuation import AccountValuation
from domain.investments.account_total_evidence import load as account_totals
from domain.investments.capital_ownership import load_contribution_ratios
from domain.investments.personal_loan_nav import apply_personal_loan_coverage
from domain.market.month_end_marks import month_end_fx
from domain.market.valuation_fx import load_fx_timelines

ZERO = Decimal(0)
ONE = Decimal(1)


def _cash_candidates(cur, account, cfg, days):
    """Native currency, before ownership. Only cash-like accounts may use ledger reconstruction."""
    from domain.investments import valuation_nav as nav
    code = account['account_code']
    unit = account.get('base_currency') or 'CNY'
    card = account.get('account_type') in ('credit_card', 'liability')
    primary = dict(account.get('primary_balance') or {})
    if primary.get('source_amount_basis') == 'owner_adjusted':
        primary['amount'] = primary.get('gross_amount')
    native = {**account, 'owner_ratio': ONE, 'primary_balance': primary}
    points = nav._historical_cash_points(cur, [native], days[-1])
    latest = max((p for p in points.values() if p['complete_cash_scope']), key=lambda p: p['as_of'], default=None)
    native = nav._cash_account_at_terminal(native, days[-1], latest)
    start = min(days[0], nav.account_anchor_date(native, days[-1]))
    flows = nav._statement_rows_by_account(cur, [code], start, days[-1])
    for row in flows.get(code, ()):
        row['transaction_date'] = row.get('posting_date') or row['transaction_date']
    buckets = nav._bank_backcast_series(accounts=[native], rows_by_account=flows, dates=days, terminal_date=days[-1],
                                        fx_timelines={}, base_currency=unit)
    from domain.accounts.dated_valuation import statement_valuations
    from domain.accounts.credit_card_debt import uses_table_net_reference
    from domain.investments.installment_principal import load_unbilled_principal
    specs = {code: (unit, card)}
    periods = statement_valuations(cur, specs, days, table_cards=[code] if card and uses_table_net_reference(code, cfg, cur) else [])
    contracts = load_unbilled_principal(cur, [code], days) if card else {}
    nav._apply_historical_cash_points(cur, bank_series=buckets, accounts=[native], dates=days,
            rate_at=lambda currency, day: ONE if currency == unit else None,
            points=points, periods=periods, unbilled=contracts)
    posted_days = sorted({r['transaction_date'] for r in flows.get(code, ()) if r.get('source_evidence_status') == 'verified'})
    anchor_day = nav.account_anchor_date(native, days[-1]) if native.get('primary_balance') else None
    for day, bucket in buckets.items():
        if anchor_day and code in bucket.get('account_values', {}):
            i = bisect_right(posted_days, day)
            bucket['anchor_date'] = anchor_day
            bucket['observed_on'] = max(anchor_day, posted_days[i-1]) if i and anchor_day <= day else anchor_day
    return buckets, periods


def _fixing(rates, currency, day, base):
    fixing = month_end_fx(rates, currency, day, base)
    if fixing or day.weekday() < 5:
        return fixing
    friday = day - timedelta(days=day.weekday() - 4)
    return month_end_fx(rates, currency, friday, base)


def _observations(cur, account, config, days, owner_ratio):
    code = account['account_code']
    cfg = {**account, **config.get('accounts', {}).get(code, {})}
    unit = cfg.get('base_currency') or 'CNY'
    cash = cfg.get('reconcile_method', 'balance') == 'balance'
    banks, periods = _cash_candidates(cur, account, cfg, days) if cash else ({}, None)
    history = read_account_history(cur, account_code=code, account_config=config, start_date=days[0], end_date=days[-1], statements=periods)
    if history is None:
        return None
    points = {r['date']: r for r in history['rows']}
    card = cfg.get('account_type') in ('credit_card', 'liability')
    bindings = load_bound_balance_references(cur, {code: cfg}) if not card else {}
    mode = cfg.get('portfolio_value_evidence')
    ratios = None
    if cfg.get('funding_history'):
        from domain.investments.nav_trade_source_v2 import _read_event_prefix
        events, gap = _read_event_prefix(cur, code)
        ratios = load_contribution_ratios(cur, cfg, events, days, unverified_from=gap)
    if mode == 'official_broker_close':
        from domain.brokers.ibkr.ibkr_report_state import _calendar
        calendar = _calendar()
    elif mode == 'reconciled_trade_cash':
        from exchange_calendars import get_calendar
        calendar = get_calendar('XSHG')
    member_estimates = {}
    if card or cfg.get('account_type') == 'fund_platform':
        from domain.investments.position_nav_projection import load_position_nav_estimates
        from domain.investments.installment_principal import load_unbilled_principal
        native_rates = load_fx_timelines(cur, unit)
        missing_days = [d for d in days if points.get(d, {}).get('signed_balance' if card else 'net_value') is None]
        member_estimates = load_position_nav_estimates(cur, accounts={code: cfg}, dates=missing_days,
            owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: ONE,
            rate_at=lambda u, d: (_fixing(native_rates, u, d, unit) or (None, None))[1],
            unbilled=load_unbilled_principal(cur, [code], missing_days) if card and missing_days else {})
    static_ratio = None if ratios is not None else owner_ratio(code)
    values = {}
    previous = None
    for day in days:
        source = points.get(day)
        point = dict(source or {})
        bucket = banks.get(day, {})
        problem = code in bucket.get('source_evidence_unavailable_accounts', ()) or point.get('coverage_break')
        signed = point.get('signed_balance') if card else point.get('net_value')
        quality = point.get('liabilities_status' if card else 'net_value_status')
        observed = point.get('net_value_as_of') or day
        basis = point.get('basis') or ('cash_balance' if cash else 'net_value')
        if (mode in ('official_broker_close', 'reconciled_trade_cash') and not source and previous
                and calendar.first_session.date() <= day <= calendar.last_session.date() and not calendar.is_session(day.isoformat())):
            session = calendar.date_to_session(day.isoformat(), direction='previous').date()
            if previous['date'] == session:
                point = dict(previous)
                signed, quality, observed = point['net_value'], 'estimated', session
                if mode == 'official_broker_close':
                    point.update(liabilities_as_of=session, liabilities_status='estimated')
                else:
                    point['price_dates'] = {'market_close': session}
        if history['interpolation'] == 'step' and not source and previous:
            point = dict(previous)
            signed, quality = point.get('net_value'), 'estimated'
            observed = point.get('net_value_as_of') or previous['date']
        if basis == 'posted_flow_net':
            # A cumulative movement without an opening balance is not a debt observation.
            point = {}
        if card and signed is None and point.get('liabilities_status') == 'confirmed' and point.get('liabilities') is not None and point['liabilities'] > 0:
            signed = -point['liabilities']
        if signed is None and code in bucket.get('account_values', {}):
            signed = bucket['account_values'][code]
            basis = bucket.get('_comparison_basis', {}).get(code, 'cash_anchor')
            quality = 'estimated' if code in bucket.get('estimated_accounts', ()) else 'confirmed'
            if card:
                point.update(liabilities=max(-signed, ZERO), liabilities_status=quality)
            if bucket.get('anchor_date'):
                point['anchor_date'] = str(bucket['anchor_date'])
                observed = min(day, bucket['observed_on'])
        if signed is None and not card and not problem:
            ref = historical_account_balance(cfg, day, ONE, account_code=code, bindings=bindings)
            if ref:
                signed = Decimal(ref['balance_amount'])
                point['reference_basis'] = ref.get('balance_reference_basis')
                basis, quality, observed = 'balance_reference', 'estimated', ref['balance_as_of']
        if signed is None and not problem:
            members = member_estimates.get(day, {})
            if point.get('liabilities_status') == 'partial' and point.get('liabilities') is not None:
                point['known_component_value'] = -point['liabilities']
                basis = 'known_debt'
            elif point.get('position_value') is not None and point.get('position_value') != 0:
                point['known_component_value'] = point['position_value']
                basis = ['position_members', point.get('position_symbols', [])]
            elif code in members and members.basis.get(code, [None])[0] == 'position_members':
                amount, first, last = members[code]
                observed = last
                point.update(known_component_value=amount, evidence_dates=(first, last))
                basis = members.basis[code]
                if card:
                    point.update(liabilities=max(-amount, ZERO), liabilities_status='partial')
                elif point.get('position_value') is None:
                    point.update(position_value=amount, position_value_status='partial')
        if problem:
            point, signed, quality, basis = {'coverage_break': True}, None, 'unavailable', 'conflicting_evidence'
        if source:
            previous = None if problem else source
        if signed is not None or point or problem:
            value = {**point, 'date': day, 'signed_balance': signed, 'basis': basis,
                     'observed_on': observed, 'quality': quality or 'unavailable',
                     'owner_ratio': ratios.get(day) if ratios is not None else static_ratio}
            if not card and signed is not None:
                value.update(net_value=signed, net_value_status=quality)
                if observed != day:
                    value['net_value_as_of'] = observed
            values[day] = value
    series = list(history['series'])
    if not card and any(v.get('position_value') is not None for v in values.values()) and 'position_value' not in series:
        series.append('position_value')
    return {**history, 'series': series, 'rows': values, 'currency': unit}


def _daily_row(day, values, records, fx, base):
    amounts, bases, refs = {}, {}, {}
    missing, estimated, errors, dated = set(), set(), set(), set()
    gross_assets = gross_debt = ZERO
    gross_complete = True
    close = None
    for code, account in records.items():
        point = values[code]['rows'].get(day, {})
        signed = point.get('signed_balance')
        unit = values[code]['currency']
        fixing = _fixing(fx, unit, day, base)
        ratio = point.get('owner_ratio')
        observation = AccountValuation(day, unit, signed, point.get('basis', 'unknown'),
                                       quality=point.get('quality', 'unavailable'))
        amount = observation.personal_value(ratio, fixing[1] if fixing else None)
        component = False
        if amount is None and point.get('known_component_value') is not None and not point.get('coverage_break'):
            component = True
            signed = point['known_component_value']
            part = AccountValuation(day, unit, signed, 'identified_members', quality='estimated')
            amount = part.personal_value(ratio, fixing[1] if fixing else None)
            missing.add(code)
            estimated.add(code)
        if point.get('coverage_break'):
            errors.add(code)
        if amount is None:
            missing.add(code)
            continue
        if point.get('reference_basis') == 'contributed_principal':
            missing.add(code)
            gross_complete = False
        amounts[code] = amount
        point['personal_contribution'] = amount
        point['contribution_scope'] = 'components' if component else 'account'
        bases[code] = observation.basis
        if observation.quality != 'confirmed' or (fixing and fixing[0] != day):
            estimated.add(code)
        observed = point['observed_on']
        dates = point.get('evidence_dates') or (observed, observed)
        refs[code] = {'basis': observation.basis, 'from': str(dates[0]), 'through': str(dates[1]),
                      'balance_as_of': str(observed), 'native_balance': str(signed), 'currency': unit,
                      'owner_ratio': str(ratio), 'fx_as_of': str(fixing[0]) if fixing else None,
                      'fx_rate': str(fixing[1]) if fixing else None}
        if point.get('price_dates'):
            refs[code]['price_dates'] = {k: str(v) for k, v in point['price_dates'].items()}
        if point.get('daily_close'):
            close = {**point['daily_close'], 'requested_as_of': str(day), 'carried': observed != day,
                     'status': 'last_valid' if observed != day else 'ok'}
            bases[code] = close['nav_basis']
        if point.get('anchor_date'):
            refs[code]['anchor_date'] = point['anchor_date']
        if observation.basis == 'cash_balance' and observed == day:
            dated.add(code)
        if component:
            gross_complete = False
        elif account.get('reconcile_method','balance') == 'balance':
            gross_assets += max(amount, ZERO)
            gross_debt += max(-amount, ZERO)
        elif point.get('gross_assets') is not None and point.get('gross_debt') is not None and fixing:
            gross_assets += point['gross_assets'] * ratio * fixing[1]
            gross_debt += point['gross_debt'] * ratio * fixing[1]
        else:
            gross_complete = False
    known = sum(amounts.values(), ZERO) if amounts else None
    return {'date': day, **({'ibkr_daily_close': close} if close else {}), '_account_contributions': amounts, '_comparison_basis': bases, '_dated_cash_accounts': dated,
            'net_worth': known if not missing else None, 'available_net_worth': known, 'available_accounts': sorted(amounts),
            'assets': gross_assets if not missing and gross_complete else None,
            'liabilities': gross_debt if not missing and gross_complete else None,
            'unavailable_accounts': sorted(missing), 'source_evidence_unavailable_accounts': sorted(errors),
            'estimated_accounts': sorted(estimated), 'valuation_quality': 'estimated' if estimated else 'confirmed' if known is not None else 'unavailable',
            'latest_known_components': refs}


def read_account_book(cur, *, accounts, config, start, end, evidence_through, base, owner_ratio, full_portfolio=True):
    """Evaluate one native series per account before projecting the requested reporting window."""
    from domain.investments.valuation_nav import _apply_account_totals, series_dates
    cur.execute("""SELECT least((SELECT min(as_of) FROM pams_holdings_snapshot),
                         (SELECT min(event_date) FROM pams_cashflow_fact),
                         (SELECT min(trade_date) FROM pams_investment_trade_fact)) AS first""")
    first = min(start, cur.fetchone()['first'] or start)
    days = series_dates(first, max(end, evidence_through), 'daily')
    values, included = {}, {}
    excluded = excluded_accounts(config)
    for account in accounts:
        code = account['account_code']
        if code in excluded or account.get('exclude_from_reconcile'):
            continue
        history = _observations(cur, account, config, days, owner_ratio)
        if history is not None:
            values[code] = history
            if account.get('include_in_nav', True):
                included[code] = account
    rates = load_fx_timelines(cur, base)
    samples = [d for d in days if d >= start]
    ownership = {code: owner_ratio(code) for code in included}
    totals = account_totals(cur, dates=samples, accounts=set(included))
    by_date = {}
    for (day, _), proof in totals.items():
        by_date.setdefault(day, []).append(proof)
    def rate_at(unit, day):
        fixing = _fixing(rates, unit, day, base)
        return fixing[1] if fixing else None

    rows = []
    for day in samples:
        row = _daily_row(day, values, included, rates, base)
        def ratio(code):
            point = values[code]['rows'].get(day)
            if point:
                return point.get('owner_ratio')
            if config.get('accounts', {}).get(code, {}).get('funding_history'):
                return None
            return ownership[code]
        _apply_account_totals(row, by_date.get(day, ()), ratio, rate_at)
        for proof in by_date.get(day, ()):
            parent = proof['account_code']
            basis = row['_comparison_basis'].get(parent)
            if not isinstance(basis, (list, tuple)) or basis[0] != 'account_total':
                continue
            for child in proof['covered_account_codes']:
                point = values[child]['rows'].setdefault(day, {'date': day})
                point.update(personal_contribution=row['_account_contributions'].get(child),
                             contribution_scope='platform_total' if parent == child else 'covered_by_total',
                             covered_by=parent)
        if not row['unavailable_accounts'] and row['available_net_worth'] is not None:
            row['net_worth'] = row['available_net_worth']
        rows.append(row)

    apply_personal_loan_coverage(cur, rows, set(included),
        lambda p: AccountValuation(p['as_of'], p['currency'], p['market_value'], 'principal').personal_value(
            owner_ratio(p['account_code']), rate_at(p['currency'], p['as_of'])),
        reference_rate_at=rate_at if full_portfolio else None, base_currency=base)
    if full_portfolio and base == 'CNY':
        from domain.accounts.monthly_fact_book import build_months, apply_month
        last_close = evidence_through.replace(day=1) - timedelta(days=1)
        # A Dec 31 request includes the opening inventory for the next year's report.
        first_month = start + timedelta(days=1) if (start.month, start.day) == (12, 31) else start
        books = build_months(cur, values, included, config, first_month, min(end, last_close),
                             rate_at=rate_at, owner_ratio=owner_ratio)
        for row in rows:
            if row['date'] in books:
                for entry in books[row['date']].values():
                    fixing = _fixing(rates, entry['currency'], row['date'], base)
                    entry['fx_date'] = str(fixing[0]) if fixing else None
                apply_month(row, values, books[row['date']], by_date.get(row['date'], ()))
    return values, [r for r in rows if r["date"] <= end]


def render_histories(values, start, end):
    """Project native measures independently of personal ownership and presentation periods."""
    output = {}
    for code, history in values.items():
        result = {k: v for k, v in history.items() if k != 'rows'}
        result.update(start_date=start, end_date=end)
        result['last_prior_date'] = max((day for day, p in history['rows'].items() if day < start and
                                         any(p.get(key) is not None for key in history['series'])),
                                        default=history.get('last_prior_date'))
        result['rows'] = [point for day, point in history['rows'].items() if start <= day <= end]
        output[code] = result
    return output


def current_balances(histories):
    """Carry the last displayed native observation with its own date and scope."""
    result = {}
    for code, history in histories.items():
        summary = {'balance_amount': None, 'balance_as_of': None, 'balance_currency': history['currency'],
                   'balance_basis': 'unavailable', 'balance_label': '缺估值'}
        for point in reversed(history['rows']):
            if point.get('coverage_break'):
                break
            amount = point.get('signed_balance')
            label = '账户净值' if 'net_value' in history['series'] else '账户余额'
            basis = point.get('basis')
            if amount is None:
                amount = point.get('known_component_value')
                label = '已知部分'
            if amount is not None:
                if basis == 'balance_reference' and point.get('reference_basis') == 'contributed_principal':
                    label = '本金参考'
                elif point.get('quality') == 'estimated' and label != '已知部分':
                    label = '参考净值'
                summary.update(balance_amount=str(amount), balance_as_of=point.get('observed_on') or point['date'],
                               balance_basis=basis, balance_label=label, valuation_date=point['date'],
                               personal_contribution=point.get('personal_contribution'))
                break
        result[code] = summary
    return result


def read_account_view(cur, view, business_date, *, member=None, accounts=None):
    """Serve the published view, or build it from the same account book on cold start."""
    from domain.accounts import account_overview
    from domain.accounts.account_policy import owner_ratio_lookup
    from domain.investments import page_projection
    from domain.investments.nav_period_comparison import period_starts

    published = page_projection.read_page(cur, view, business_date=business_date, member=member)
    if published is not None:
        return published['data']
    cfg = account_overview._load_accounts_config()
    start = min(period_starts(business_date).values())
    if accounts is None:
        accounts = account_overview._overview_payload(cur=cur, include_transfers=False)['accounts']
    values, _ = read_account_book(cur, accounts=accounts, config=cfg, start=start, end=business_date,
                                  evidence_through=business_date, base='CNY', owner_ratio=owner_ratio_lookup(cfg))
    histories = render_histories(values, start, business_date)
    result = current_balances(histories) if view == 'account_balances' else histories
    return result if member is None else result.get(member)
