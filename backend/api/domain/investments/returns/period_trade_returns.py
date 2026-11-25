"""Reconcile observed, fully matched trade cycles independently of open positions."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from domain.brokers.ibkr.ibkr_daily_holdings import MARKET_LABELS
from domain.imports.sources.source_original_read import SourceOriginals
from domain.imports.sources.ths_asset_candidates import MARKETS
from domain.investments.returns.fund_return_history import METHOD as FUND_HISTORY_METHOD
from domain.investments.holdings.holdings_positions import _trade_order_ambiguous
from domain.investments.returns.investment_return_data import exact_fx_rate_at
from domain.investments.trades.trade_read_v2 import load_trade_rows
from domain.investments.valuation.valuation_nav import load_fx_timelines


BUYS = {'buy', 'fund_subscription'}
SELLS = {'sell', 'fund_redemption'}
INCOME = {'dividend', 'interest', 'coupon', 'tax', 'fee'}
SCOPE_ACCOUNTS = {
    'a_fund': ('ccb_yhs300_fund', 'tencent_licaitong'),
    'a_stock': ('tonghuashun',),
    'us_stock': ('ibkr',),
}


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _closed_trade_cycles(trades, *, start, end, fx_rate, owner_ratio):
    """Recognize matched trade profit, without claiming complete account returns.

    Ignore a cycle already open at the start. Its period profit needs a verified
    opening mark. Stop at any unresolved change in quantity or cash so a later
    trade cannot accidentally consume an invented cost basis.
    """
    trades = [row for row in trades if row['trade_date'] <= end]
    executions = [row for row in trades if row['trade_type'] in BUYS | SELLS]
    if not executions or _trade_order_ambiguous(executions):
        return [], None
    if any(_number(row.get('quantity')) is None for row in executions):
        return _confirmed_cash_cycle(trades, start=start, fx_rate=fx_rate, owner_ratio=owner_ratio)
    quantity = cost = cash = distributions = Decimal(0)
    opened = None
    closed = []
    for row in sorted(trades, key=lambda r: (r['trade_date'], str(r['trade_time'] or ''), r['trade_id'])):
        day, kind = row['trade_date'], row['trade_type']
        if row['source_evidence_status'] != 'verified':
            return [], None
        net = _number(row.get('net_cash'))
        if kind not in BUYS | SELLS | INCOME or net is None:
            return [], None
        if kind in INCOME:
            if (net > 0) != (kind in {'dividend', 'interest', 'coupon'}) or net == 0:
                return [], None
            # A distribution after an exit may belong to a different period.
            # Leave it out of the matched-cycle subtotal without a linked lot.
            if not quantity:
                continue
            rate = fx_rate(row) if opened >= start else None
            if opened >= start and rate is None:
                return [], None
            if rate is not None:
                distributions += net * rate
            continue
        size = _number(row.get('quantity'))
        buy = kind in BUYS
        if size is None or size <= 0 or (buy and net >= 0) or (not buy and (net < 0 or size > quantity)):
            return [], None
        if not quantity:
            opened = day
        if opened >= start:
            rate = fx_rate(row)
            if rate is None:
                return [], None
            cash += net * rate
            if buy:
                cost -= net * rate
        quantity += size if buy else -size
        if quantity == 0:
            if opened >= start:
                closed.append({'open_date': opened, 'exit_date': day, 'trade_pnl': cash * owner_ratio,
                               'cost': cost * owner_ratio, 'distributions': distributions * owner_ratio})
            opened = None
            cost = cash = distributions = Decimal(0)
    return closed, quantity


def _confirmed_cash_cycle(trades, *, start, fx_rate, owner_ratio):
    """One explicitly confirmed complete lifecycle when units are unavailable.

    Multiple exits cannot be allocated without units; never infer them from a
    final zero or fabricate a position quantity. Net cash already includes fees.
    """
    rows = sorted(trades, key=lambda r: (r['trade_date'], str(r['trade_time'] or ''), r['trade_id']))
    executions = [row for row in rows if row['trade_type'] in BUYS | SELLS]
    if (len(executions) < 2 or executions[0]['trade_type'] not in BUYS
            or executions[-1]['trade_type'] not in SELLS
            or any(row['trade_type'] not in BUYS for row in executions[:-1])
            or executions[-1].get('complete_exit_evidence') is not True
            or executions[-1].get('complete_trade_ids') != {row['trade_id'] for row in rows}):
        return [], None
    opening_value = Decimal(0)
    if executions[0]['trade_date'] < start:
        opening_day = (start - timedelta(days=1)).isoformat()
        prices = {_number(source.get('valuation_unit_price')) for row in rows
                  for source in row.get('supplemental_fields', []) if source.get('valuation_date') == opening_day}
        if len(prices) != 1 or None in prices or next(iter(prices)) <= 0:
            return [], None
        quantities = []
        for row in executions[:-1]:
            if row['trade_date'] >= start:
                continue
            quantities_found = ({_number(row['quantity'])} if row.get('quantity') is not None else
                {_number(source.get('quantity')) for source in row.get('supplemental_fields', [])
                 if source.get('quantity') is not None})
            if len(quantities_found) != 1 or None in quantities_found or next(iter(quantities_found)) <= 0:
                return [], None
            quantities.append(next(iter(quantities_found)))
        rate = fx_rate({**executions[0], 'trade_date': start - timedelta(days=1)})
        if not quantities or rate is None:
            return [], None
        opening_value = sum(quantities, Decimal(0)) * next(iter(prices)) * rate
    cash = cost = distributions = Decimal(0)
    for row in rows:
        kind, net, rate = row['trade_type'], _number(row.get('net_cash')), fx_rate(row)
        if (row.get('source_evidence_status') != 'verified' or kind not in BUYS | SELLS | INCOME
                or net is None or rate is None or not executions[0]['trade_date'] <= row['trade_date'] <= executions[-1]['trade_date']
                or (kind in BUYS | {'tax', 'fee'} and net >= 0)
                or (kind in SELLS | {'dividend', 'interest', 'coupon'} and net <= 0)):
            return [], None
        if row['trade_date'] >= start:
            if kind in INCOME:
                distributions += net * rate
            else:
                cash += net * rate
                if kind in BUYS:
                    cost -= net * rate
    cash -= opening_value
    cost += opening_value
    return [{'open_date': executions[0]['trade_date'], 'exit_date': executions[-1]['trade_date'],
             'trade_pnl': cash * owner_ratio, 'cost': cost * owner_ratio,
             'distributions': distributions * owner_ratio}], Decimal(0)


def _section(account, rows):
    kinds = {row['trade_type'] for row in rows}
    if all(row.get('asset_type') == 'wealth_management' for row in rows):
        return 'wealth_management'
    if account in SCOPE_ACCOUNTS['a_fund'] and kinds & {'fund_subscription', 'fund_redemption'}:
        return 'a_fund' if not kinds & {'buy', 'sell'} else None
    if account in SCOPE_ACCOUNTS['a_stock'] and all(
        row.get('source_market') in MARKETS
        for row in rows if row['trade_type'] in BUYS | SELLS
    ):
        return 'a_stock'
    if account in SCOPE_ACCOUNTS['us_stock'] and {
        MARKET_LABELS.get(row.get('listing_exchange')) for row in rows if row['trade_type'] in BUYS | SELLS
    } == {'美股'}:
        return 'us_stock'
    return None


def summarize_closed_trades(trades, *, start, end, fx_rate, owner_ratio, opening_positions, final_positions):
    """Account, instrument and currency isolate otherwise similar trades."""
    groups = defaultdict(list)
    unresolved_symbols = set()
    for row in trades:
        if row['trade_date'] > end or row['trade_type'] in {'deposit', 'withdraw', 'fx_conversion'}:
            continue
        identity = row.get('instrument_identity') if row['account_code'] == 'ibkr' else row.get('asset_id')
        if identity and row.get('symbol'):
            groups[row['account_code'], identity, row['currency']].append(row)
        elif row['trade_type'] in BUYS | SELLS and row.get('symbol'):
            unresolved_symbols.add((row['account_code'], row['symbol'], row['currency']))
    result = defaultdict(list)
    for key, rows in groups.items():
        account, _, currency = key
        section = _section(account, rows)
        unresolved = any((account, row['symbol'], currency) in unresolved_symbols for row in rows)
        has_opening = key in opening_positions or any(
            (account, row['symbol'], currency) in opening_positions for row in rows)
        cash_only = any(row['trade_type'] in BUYS | SELLS and row.get('quantity') is None for row in rows)
        if not section or unresolved or (has_opening and not cash_only):
            continue
        # An unexplained live balance contradicts a purported final exit. A
        # later, separately recorded buy can reopen the position.
        ratio = owner_ratio(account)
        cycles, quantity = _closed_trade_cycles(rows, start=start, end=end, fx_rate=fx_rate, owner_ratio=ratio)
        if not cycles:
            continue
        last = max(rows, key=lambda r: (r['trade_date'], r['trade_id']))
        observed = final_positions.get((account, last['symbol'], currency))
        if observed and observed[0] >= last['trade_date'] and observed[1] != quantity:
            continue
        result[section].append({
            'account_code': account, 'symbol': last['symbol'], 'name': last['name'], 'currency': currency,
            'cycles': cycles,
        })
    return dict(result)


def load_closed_trade_returns(cur, *, start, end, owner_ratio, base_currency='CNY', sections=None,
                              excluded_accounts=()):
    """Load only accounts with an applicable category in the existing views."""
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS redesigned")
    if not cur.fetchone()['redesigned']:
        return {}
    accounts = sorted({account for section, members in SCOPE_ACCOUNTS.items()
                       if sections is None or section in sections for account in members
                       if account not in excluded_accounts})
    wealth_accounts = set()
    if sections is None or 'wealth_management' in sections:
        cur.execute("""SELECT DISTINCT f.account_code FROM pams_investment_trade_fact f
            JOIN pams_asset_master a ON a.asset_id=f.asset_id
            WHERE a.asset_type='wealth_management' AND f.trade_date<=%s""", (end,))
        wealth_accounts = {row['account_code'] for row in cur.fetchall()
                           if row['account_code'] not in excluded_accounts}
        accounts = sorted(set(accounts) | wealth_accounts)
    if not accounts or start > end:
        return {}
    originals = SourceOriginals(cur)
    trades = [row for account in accounts
              for row in load_trade_rows(cur, account_code=account, start=date.min, end=end,
                                         originals=originals,
                                         include_instrument_identity=True, candidate_sale_start=start)]
    # A confirmed execution can precede the bank settlement recorded on the
    # original fact. Read complete wealth lifecycles before applying the
    # requested window; other categories keep their indexed query above.
    wealth_trades = [row for account in wealth_accounts for row in load_trade_rows(
        cur, account_code=account, start=date.min, end=date.max, originals=originals,
        include_instrument_identity=True) if row.get('asset_type') == 'wealth_management']
    trades = list({row['trade_id']: row for row in trades + wealth_trades}.values())
    if not trades:
        return {}
    from domain.imports.publication.trade_supplemental_evidence import read as read_trade_evidence
    from domain.investments.holdings.position_evidence_replay import replay_reviewed_cycle
    wealth_groups = defaultdict(list)
    for row in wealth_trades:
        wealth_groups[(row['account_code'], row.get('asset_id'), row['currency'])].append(row)
    for rows in wealth_groups.values():
        evidence = {row['trade_id']: read_trade_evidence(cur, trade_id=row['trade_id']) for row in rows}
        cycle = replay_reviewed_cycle(rows, evidence)
        if cycle:
            for row in rows:
                if row['trade_type'] in SELLS:
                    row['trade_date'] = cycle['exit_date']
    for row in trades:
        if row['trade_type'] in BUYS | SELLS and row.get('quantity') is None:
            evidence = read_trade_evidence(cur, trade_id=row['trade_id'])
            row['supplemental_fields'] = [item['fields'] for item in evidence if item['status'] == 'verified']
            if row['trade_type'] not in SELLS:
                continue
            statements = [item['fields'] for item in evidence if item['status'] == 'verified'
                          and (item.get('fields') or {}).get('complete_exit') is True
                          and (item.get('fields') or {}).get('complete_history') is True
                          and isinstance(item['fields'].get('complete_trade_ids'), list)
                          and all(type(value) is int and value > 0 for value in item['fields']['complete_trade_ids'])]
            distinct = {tuple(sorted(item.get('complete_trade_ids') or [])) for item in statements}
            row['complete_exit_evidence'] = len(distinct) == 1
            row['complete_trade_ids'] = set(next(iter(distinct))) if len(distinct) == 1 else set()
    cur.execute("""SELECT DISTINCT ON (boundary, h.account_code, h.symbol, h.currency)
            boundary,h.account_code,h.asset_id,h.symbol,h.currency,h.quantity,h.as_of
        FROM (VALUES (%s::date), (%s::date)) AS dates(boundary)
        JOIN pams_holdings_snapshot h ON h.as_of <= boundary
        WHERE h.account_code=ANY(%s::text[]) AND h.symbol IS NOT NULL
        ORDER BY boundary,h.account_code,h.symbol,h.currency,h.as_of DESC,h.id DESC""",
                (start - timedelta(days=1) if start > date.min else None, end, accounts))
    observed = cur.fetchall()
    opening = {(row['account_code'], identity, row['currency'])
               for row in observed if row['boundary'] < start and row['quantity'] != 0
               for identity in (row['asset_id'], row['symbol']) if identity}
    final = {(row['account_code'], row['symbol'], row['currency']): (row['as_of'], row['quantity'])
             for row in observed if row['boundary'] == end and row['quantity'] is not None}
    timelines = load_fx_timelines(cur, base_currency) if any(row['currency'] != base_currency for row in trades) else {}
    groups = summarize_closed_trades(
        trades, start=start, end=end, owner_ratio=owner_ratio, opening_positions=opening,
        final_positions=final,
        fx_rate=lambda row: exact_fx_rate_at(timelines, row['currency'], row['trade_date'], base_currency),
    )
    if sections is not None:
        groups = {section: rows for section, rows in groups.items() if section in sections}
    return {section: {
        'start': start.isoformat(), 'end': end.isoformat(),
        'status': 'partial', 'basis': 'verified_matched_trades', 'base_currency': base_currency,
        'amount': str(sum((cycle['trade_pnl'] + cycle['distributions']
                           for row in rows for cycle in row['cycles']), Decimal(0))),
        'items': [{**row, 'cycles': [{key: str(value) for key, value in cycle.items()}
                                   for cycle in row['cycles']]} for row in rows],
    } for section, rows in groups.items()}


def fund_holding_profit(holdings, closed_returns, *, account_code=None):
    """A reconciliation of current floating and known closed profit, on separate bases."""
    active = [row for row in holdings.get('rows', []) if not row.get('is_closed')
              and (account_code is None or (row.get('account_code') or row.get('account')) == account_code)]
    amounts = [(_number(row.get('market_value_cny')), _number(row.get('cost_cny'))) for row in active]
    if any(value is None or cost is None for value, cost in amounts):
        return None
    floating = sum((value - cost for value, cost in amounts), Decimal(0))
    closed = (Decimal(closed_returns['amount']) if account_code is None else
              sum((Decimal(cycle['trade_pnl']) + Decimal(cycle['distributions'])
                   for item in closed_returns['items'] if item['account_code'] == account_code
                   for cycle in item['cycles']), Decimal(0)))
    return str(floating + closed)


def attach_closed_trade_returns(payload, closed_returns):
    """Publish a currency amount beside period performance, never inside an existing TWR."""
    if not closed_returns:
        return payload
    result = {**payload, 'closed_trade_returns': closed_returns}
    if payload.get('section') != 'a_fund' or payload.get('method') == FUND_HISTORY_METHOD:
        return result
    # The legacy fund curve uses sparse account snapshots. It has no verified
    # marks for these trade-only positions, so the resulting rate is incomplete.
    fields = ('daily_return_rate', 'time_weighted_return_rate', 'capital_return_rate',
              'display_return_rate', 'display_return_rate_raw', 'return_rate')
    rows = payload.get('rows', [])
    historical_rows = rows[:-1] if (payload.get('section_pnl_state') or {}).get('overlay_applied') else rows
    if any(row.get('display_return_rate') is not None for row in historical_rows):
        result['estimated_series'] = {
            'label': '基金历史估值（部分）',
            'method': 'fund_partial_snapshot_return',
            'rows': [{'date': row['date'], 'display_return_rate': row.get('display_return_rate')}
                     for row in historical_rows],
        }
    return {
        **result,
        'status': 'unavailable',
        'reason': 'fund_period_valuation_unverified',
        'coverage_notes': ['已核对清仓交易，基金期初和期间估值覆盖仍待核验。'],
        'summary': {**payload.get('summary', {}), 'total_return': None,
                    'total_return_rate': None, 'annualized_return_rate': None},
        'rows': [{**row, **dict.fromkeys(fields)} for row in rows],
    }
