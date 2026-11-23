"""Price investment positions independently of the account cash ledger."""

from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import Decimal
import re

from domain.imports.source_original_read import SourceOriginals
from domain.investments.fund_return_history import NAV_SOURCES, _trade_day
from domain.investments.investment_return_ibkr import _ibkr_yfinance_symbol
from domain.investments.investment_return_math import norm
from domain.investments.trade_read_v2 import load_trade_rows
from domain.investments.valuation_nav import fx_rate_at, load_fx_timelines, series_dates
from domain.market.quote_valuation import GOLD_BENCHMARK_ASSET, GOLD_CLOSE_SOURCE
from domain.market.tencent_stock_history import SOURCE as STOCK_CLOSE_SOURCE


METHOD = 'investment_position_time_weighted_return'
CATEGORIES = ('A股', '美股', '贵金属', 'ESOP')
BUYS = {'buy', 'fund_subscription'}
SELLS = {'sell', 'fund_redemption'}
CHANGES = BUYS | SELLS | {'split', 'corporate_action', 'transfer_in_kind', 'transfer_out_kind', 'position_adjustment'}
INCOME = {'dividend', 'interest', 'coupon', 'fee', 'tax'}
ZERO = Decimal(0)


def latest_position_valuation_date(cur, *, holding_date, through):
    """Advance the investment view when a held instrument gets a newer mark."""
    if through <= holding_date:
        return holding_date
    cur.execute("SELECT to_regclass('source_records') IS NOT NULL AS available")
    if not (cur.fetchone() or {}).get('available'):
        return holding_date
    cur.execute("""
        SELECT MAX(q.as_of) AS latest
        FROM market_observations q
        WHERE q.as_of > %s AND q.as_of <= %s
          AND (q.observation_type='daily' AND q.source=ANY(%s)
              OR q.observation_type='quote' AND q.quote_session='close' AND q.return_status='confirmed')
          AND COALESCE(q.unit_nav,q.close_price,q.price)>0
          AND EXISTS (
              SELECT 1 FROM pams_holdings_snapshot h
              WHERE h.as_of<=q.as_of AND h.asset_liability='资产' AND h.quantity>0
                AND NOT EXISTS (SELECT 1 FROM account_snapshot_member pm JOIN account_snapshot_version pv ON pv.version_id=pm.version_id
                    WHERE pm.holding_id=h.id AND pv.coverage_status='partial'
                      AND pv.scope_attestation->'point_in_time_only'='true'::jsonb)
                AND h.major_category=ANY(%s) AND h.currency=q.currency
                AND (q.symbol=h.symbol OR q.asset_id=h.asset_id
                    OR q.market='CN_A' AND q.symbol IN ('sh'||h.symbol,'sz'||h.symbol)
                    OR q.source=%s AND h.major_category='贵金属')
          )
    """, (holding_date, through, [*NAV_SOURCES, STOCK_CLOSE_SOURCE, GOLD_CLOSE_SOURCE],
          list(CATEGORIES), GOLD_CLOSE_SOURCE))
    return (cur.fetchone() or {}).get('latest') or holding_date


def _identity(row):
    symbol = (row.get('asset_symbol') or row.get('symbol') or '').strip().upper()
    if re.fullmatch(r'(SH|SZ)\d{6}', symbol):
        symbol = symbol[2:]
    return row['account_code'], row['currency'], symbol


def _kind(row):
    if row.get('asset_type') == 'wealth_management':
        return 'wealth'
    if row.get('minor_category') == 'A股基金' or row.get('asset_type') in ('fund', 'fund_cn', '基金', 'ETF / 基金'):
        return 'fund'
    major = row.get('major_category')
    if major == '贵金属':
        return 'gold'
    if major == 'ESOP':
        return 'reference'
    if major == 'A股':
        return 'stock'
    if major in ('美股', '港股', '韩股'):
        return 'overseas'
    if row.get('trade_type') in ('fund_subscription', 'fund_redemption'):
        return 'fund'
    market = row.get('market')
    if market == 'ESOP':
        return 'reference'
    if market in ('CN_A', 'A股') or row.get('source_market') in ('上海Ａ股', '深圳Ａ股'):
        return 'stock'
    if market in ('STK', 'US', '美股') and row['currency'] != 'CNY':
        return 'overseas'
    return None


def _price_symbol(key, kind):
    _, currency, symbol = key
    if kind == 'stock' and re.fullmatch(r'\d{6}', symbol):
        return ('sh' if symbol.startswith('6') else 'sz') + symbol
    if kind == 'overseas':
        return _ibkr_yfinance_symbol(symbol, currency) or symbol
    return symbol


def _load_positions(cur, end, excluded, selection=None):
    restriction = ''
    params = [end, list(CATEGORIES) + (['港股', '韩股'] if selection else []), sorted(excluded)]
    if selection:
        restriction = """ AND (h.account_code,COALESCE(NULLIF(h.asset_id,''),
            'symbol:' || h.currency || ':' || h.symbol)) IN (SELECT * FROM unnest(%s::text[],%s::text[]))"""
        params.extend(([account for account, _ in selection], [asset for _, asset in selection]))
    cur.execute("""SELECT h.id,h.account_code,h.currency,h.asset_id,h.symbol,h.quantity,h.current_price,
        h.major_category,h.minor_category,h.as_of,h.source_item_id,m.primary_symbol AS asset_symbol
        FROM pams_holdings_snapshot h
        LEFT JOIN pams_asset_master m ON h.asset_id=m.asset_id
        WHERE h.as_of<=%s AND h.asset_liability='资产' AND h.major_category=ANY(%s)
          AND NOT (h.account_code=ANY(%s::text[]))
          AND NOT EXISTS (SELECT 1 FROM account_snapshot_member pm JOIN account_snapshot_version pv ON pv.version_id=pm.version_id
              WHERE pm.holding_id=h.id AND pv.coverage_status='partial'
                AND pv.scope_attestation->'point_in_time_only'='true'::jsonb)
          AND NOT (h.source_item_id IS NULL AND EXISTS (
            SELECT 1 FROM valuation_evidence v
            WHERE v.result_kind='holding' AND v.account_code=h.account_code AND v.business_date=h.as_of
              AND v.inputs @> jsonb_build_array(
                jsonb_build_object('evidence',jsonb_build_object('role','valuation_quote')),
                jsonb_build_object('evidence',jsonb_build_object(
                  'kind','holding','source_ref',jsonb_build_object('key',jsonb_build_object('id',h.id))))
              )
          ))
        """ + restriction + " ORDER BY h.as_of,h.id", params)
    positions = defaultdict(dict)
    kinds = {}
    asset_keys = {}
    for row in cur.fetchall():
        key = _identity(row)
        kind = _kind(row)
        if not key[2] or not kind or row['quantity'] is None:
            continue
        kinds[key] = kind
        identity = row['asset_id'] or f"symbol:{row['currency']}:{row['symbol']}"
        asset_keys[key[0], key[1], identity] = key
        positions[key][row['as_of']] = row
    return positions, kinds, asset_keys


def _load_events(cur, end, excluded, asset_keys, kinds, selected_keys=None):
    cur.execute("""SELECT DISTINCT account_code FROM pams_investment_trade_fact
        WHERE trade_date<=%s AND trade_type=ANY(%s) AND NOT (account_code=ANY(%s::text[]))
        ORDER BY account_code""", (end, sorted(CHANGES | INCOME), sorted(excluded)))
    accounts = [row['account_code'] for row in cur.fetchall()]
    originals = SourceOriginals(cur)
    events = defaultdict(lambda: defaultdict(list))
    wealth_rows = defaultdict(list)
    supplemental_prices = defaultdict(dict)
    for account in accounts:
        for row in load_trade_rows(cur, account_code=account, start=date.min, end=date.max, originals=originals,
                                   include_fund_execution=True, trade_types=tuple(sorted(CHANGES | INCOME))):
            key = asset_keys.get((account, row['currency'], row['asset_id']), _identity(row))
            if selected_keys is not None and key not in selected_keys:
                continue
            kind = kinds.get(key) or _kind(row)
            if not kind or not key[2]:
                # Account-level interest and fees belong to the separate cash calculation.
                continue
            kinds[key] = kind
            if kind == 'wealth':
                wealth_rows[key].append(row)
                continue
            if row['trade_date'] > end:
                continue
            day = _trade_day(row) if kind == 'fund' else row['trade_date']
            if day <= end:
                events[key][day].append(row)
    from domain.imports.trade_supplemental_evidence import read as read_trade_evidence
    from domain.investments.position_evidence_replay import replay_reviewed_cycle
    for key, rows in wealth_rows.items():
        proof = {row['trade_id']: read_trade_evidence(cur, trade_id=row['trade_id']) for row in rows}
        replay = replay_reviewed_cycle(rows, proof)
        if not replay:
            continue
        for original in rows:
            row = {**original, 'quantity': replay['buy_quantity']}
            day = replay['exit_date'] if row['trade_type'] in SELLS else row['trade_date']
            if day <= end:
                events[key][day].append(row)
        supplemental_prices[key] = replay
    return events, supplemental_prices


def _load_prices(cur, kinds, end):
    symbols = sorted({_price_symbol(key, kind) for key, kind in kinds.items() if kind != 'reference'})
    quote_symbols = sorted(set(symbols) | {key[2] for key in kinds})
    cur.execute("""
        SELECT symbol, currency, market, as_of, COALESCE(unit_nav, close_price, price) AS price,
               1 AS priority
        FROM market_observations
        WHERE as_of<=%s
          AND ((symbol=ANY(%s) AND (
                (observation_type='daily' AND source=ANY(%s))
                OR (observation_type='quote' AND quote_session='close' AND return_status='confirmed')
              )) OR (observation_type='daily' AND asset_id=%s AND source=%s))
        UNION ALL
        SELECT symbol, NULL, market, trade_date, close, 2
        FROM pams_ohlcv_daily WHERE trade_date<=%s AND symbol=ANY(%s) AND market IN ('CN_A','YF')
        ORDER BY as_of, priority
    """, (end, quote_symbols, [*NAV_SOURCES, STOCK_CLOSE_SOURCE], GOLD_BENCHMARK_ASSET,
          GOLD_CLOSE_SOURCE, end, symbols))
    marks = defaultdict(dict)
    for row in cur.fetchall():
        price = row['price']
        if price is None or not price.is_finite() or price <= 0:
            continue
        market = row['market']
        symbol = (row['symbol'] or '').upper()
        if market == 'CN_A' and re.fullmatch(r'(SH|SZ)\d{6}', symbol):
            symbol = symbol[2:]
        if market in ('贵金属', 'GOLD', 'SGE'):
            symbol, market = GOLD_BENCHMARK_ASSET, 'gold'
        elif market == '基金':
            market = 'fund'
        elif market == 'CN_A':
            market = 'stock'
        else:
            market = 'overseas'
        currency = row['currency'] if market == 'overseas' and row['priority'] == 1 else None
        key = (market, symbol, currency)
        previous = marks[key].get(row['as_of'])
        if previous is None:
            marks[key][row['as_of']] = (row['priority'], price)
        elif previous[0] == row['priority'] and previous[1] != price:
            marks[key][row['as_of']] = (row['priority'], None)
    return {key: (list(days), [value for _, value in days.values()]) for key, days in marks.items()}


def _market_price(marks, key, kind, day):
    symbol = key[2] if kind != 'gold' else GOLD_BENCHMARK_ASSET
    candidates = ([(kind, symbol, key[0])] if kind == 'wealth' else
                  [(kind, symbol, key[1]), (kind, symbol, None),
                   (kind, _price_symbol(key, kind).upper(), None)])
    latest = None
    for key in dict.fromkeys(candidates):
        days, prices = marks.get(key, ([], []))
        index = bisect_right(days, day) - 1
        if index >= 0 and (latest is None or days[index] > latest[0]):
            latest = days[index], prices[index]
    return latest


def _supplement_selected_overseas_prices(marks, kinds, start, end):
    """Fill a selected security's missing historical closes for display only."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from domain.market.benchmark_history import overseas_history
    through = min(end, datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1))
    if start > through:
        return marks
    marks = dict(marks)
    for key, kind in kinds.items():
        if kind != 'overseas':
            continue
        symbol = _price_symbol(key, kind).upper()
        price_key = ('overseas', symbol, None)
        days, values = marks.get(price_key, ([], []))
        combined = dict(zip(days, values))
        try:
            for day, price in overseas_history(symbol, start, through):
                combined.setdefault(day, price)
        except (OSError, ValueError):
            continue
        ordered = sorted(combined.items())
        marks[price_key] = ([day for day, _ in ordered], [price for _, price in ordered])
    return marks


def _movement(row, kind):
    if row['source_evidence_status'] != 'verified' or (
        kind == 'fund' and row['trade_type'] in BUYS | SELLS and not row['fund_execution_confirmed']
    ):
        return None, None
    action, quantity, cash = row['trade_type'], row['quantity'], row['net_cash']
    if action in INCOME:
        return ZERO, -cash if cash is not None else None
    if action not in BUYS | SELLS or quantity is None:
        return None, None
    if kind == 'reference' and cash is None and row['gross_amount'] is not None:
        cash = -row['gross_amount'] if action in BUYS else row['gross_amount']
    return quantity if action in BUYS else -quantity, -cash if cash is not None else None


def _position_days(dates, anchors, events, kind):
    """Replay quantity and settlement independently and reset quantity at confirmed observations."""
    quantity, confirmed_on, reference = None, None, None
    first_day = min(set(anchors) | set(events), default=None)
    first_trades = events.get(first_day, ())
    from_open = bool(first_trades and all(
        row['trade_type'] in BUYS and _movement(row, kind)[0] is not None for row in first_trades
    ))
    if from_open:
        quantity = ZERO
    for day in sorted(set(dates) | set(anchors) | set(events)):
        flow = ZERO
        for event in events.get(day, ()):
            delta, cash = _movement(event, kind)
            flow = None if flow is None or cash is None else flow + cash
            if delta is None:
                quantity = None
            elif quantity is not None and delta:
                quantity += delta
                confirmed_on = day
        anchor = anchors.get(day)
        if anchor:
            if quantity is not None and quantity != anchor['quantity'] and day != first_day:
                flow = None
            if anchor['source_item_id'] is not None or quantity != anchor['quantity'] or confirmed_on is None:
                confirmed_on = day
            quantity = anchor['quantity']
            reference = anchor['current_price']
        yield day, quantity, confirmed_on, reference, flow


def build_position_return_series(cur, *, start, end, interval, base_currency, owner_ratio, excluded_accounts,
                                 position_kind=None, selection=None, monthly_changes=None, counted_income_sources=None,
                                 statement_months=None):
    """Compute a securities return from holdings and marks, with trade flows only."""
    start = min(start, end)
    dates = series_dates(start, end, 'daily')
    date_set = set(dates)
    if selection is None:
        positions, kinds, asset_keys = _load_positions(cur, end, excluded_accounts)
        events, supplemental_prices = _load_events(cur, end, excluded_accounts, asset_keys, kinds)
    else:
        positions, kinds, asset_keys = _load_positions(cur, end, excluded_accounts, selection)
        events, supplemental_prices = _load_events(cur, end, excluded_accounts, asset_keys, kinds, set(kinds))
    if position_kind is not None:
        kinds = {key: kind for key, kind in kinds.items() if kind == position_kind}
    marks = _load_prices(cur, kinds, end)
    for key, replay in supplemental_prices.items():
        ordered = sorted((day, price) for day, price in replay['prices'].items() if day <= end)
        marks['wealth', key[2], key[0]] = ([day for day, _ in ordered], [price for _, price in ordered])
    if selection:
        marks = _supplement_selected_overseas_prices(marks, kinds, start, end)
    fx = load_fx_timelines(cur, base_currency)
    base = base_currency.upper()
    valuation_fx = {}
    for currency in {key[1] for key in kinds}:
        if currency == base:
            valuation_fx[currency] = dict.fromkeys(dates, Decimal(1))
            continue
        timeline = fx.get(currency, ())
        observed = [day for day, _ in timeline]
        valuation_fx[currency] = {}
        for day in dates:
            index = bisect_right(observed, day) - 1
            if index >= 0:
                valuation_fx[currency][day] = timeline[index][1]
    values, flows = defaultdict(dict), defaultdict(dict)
    quantity_days, price_days = defaultdict(dict), defaultdict(dict)
    owners = {account: owner_ratio(account) for account in {key[0] for key in kinds}}
    kinds = {key: kind for key, kind in kinds.items() if owners[key[0]] != 0}
    for key, kind in kinds.items():
        account, currency, _ = key
        for day, quantity, confirmed_on, reference, flow in _position_days(
            dates, positions.get(key, {}), events.get(key, {}), kind,
        ):
            if day not in date_set:
                continue
            trade_rate = fx_rate_at(fx, currency, day, base_currency) if flow else None
            flows[day][key] = ZERO if flow == 0 else (
                flow * trade_rate * owners[account] if flow is not None and trade_rate is not None else None
            )
            mark = ((confirmed_on, reference) if reference is not None else None) if kind == 'reference' else (
                _market_price(marks, key, kind, day) if quantity else None
            )
            # Only an evidenced market closure permits carrying a prior mark.
            if (kind == 'wealth' and mark is not None and mark[0] < day
                    and day not in supplemental_prices.get(key, {}).get('closed_days', set())):
                mark = None
            valuation_rate = valuation_fx[currency].get(day)
            if quantity == 0:
                values[day][key] = ZERO
            elif quantity is not None and mark is not None and mark[1] is not None and valuation_rate is not None:
                values[day][key] = quantity * mark[1] * valuation_rate * owners[account]
                quantity_days[day][key] = confirmed_on
                price_days[day][key] = mark[0]

    rows = []
    cumulative, total_pnl = ZERO, ZERO
    previous = {}
    for day in dates:
        month = day.replace(day=1)
        current = values[day]
        common = current.keys() & previous.keys()
        # Compare the same instruments across each mark boundary. An unresolved
        # settlement leaves a gap for the affected part of the investment scope.
        comparable = [key for key in common if flows[day].get(key) is not None]
        prior = sum((previous[key] for key in comparable), ZERO)
        if prior > 0:
            change = ZERO
            for key in comparable:
                movement = current[key] - previous[key] - flows[day][key]
                change += movement
                if (monthly_changes is not None and key[0] not in (statement_months or {}).get(month, {})
                        and (current[key] or previous[key] or flows[day][key])):
                    monthly_changes[month][(kinds[key], key[1], key[2])] += movement
                    if counted_income_sources is not None:
                        counted_income_sources.update(event['source_item_id'] for event in events.get(key, {}).get(day, ())
                                                      if event['trade_type'] in {'dividend', 'coupon', 'interest'})
            daily = change / prior
            cumulative = (1 + cumulative) * (1 + daily) - 1
            total_pnl += change
        else:
            daily = None
        covered = bool(current) and (len(current) == len(kinds) or any(current.values()))
        if not rows and covered:
            daily = ZERO
        full_scope = len(current) == len(kinds) and (day == dates[0] or len(comparable) == len(current))
        row = {
            'date': day, 'market_value': sum(current.values(), ZERO) if covered else None,
            'daily_return_rate': daily, 'return_rate': cumulative if daily is not None else None,
            'display_return_rate': cumulative if daily is not None else None,
            'time_weighted_return_rate': cumulative if daily is not None else None,
            'total_pnl': total_pnl if daily is not None else None,
            'covered_positions': sum(value != 0 for value in current.values()),
            'missing_positions': len(kinds) - len(current),
            'return_scope': 'covered_positions' if full_scope else 'partial_positions',
            'comparable_positions': len(comparable),
            'estimated_positions': sum(key in supplemental_prices and value != 0 for key, value in current.items()),
            'carried_price_positions': sum(price_day < day for price_day in price_days[day].values()),
            'holding_as_of': max((date_ for date_ in quantity_days[day].values() if date_ is not None), default=None),
            'market_price_as_of': max((date_ for date_ in price_days[day].values() if date_ is not None), default=None),
        }
        rows.append({key: norm(value) for key, value in row.items()})
        previous = current
    if interval == 'weekly':
        sampled = set(series_dates(start, end, 'weekly'))
        rows = [row for row in rows if date.fromisoformat(row['date']) in sampled]
    available = any(row['return_rate'] is not None for row in rows)
    return {
        'method': METHOD, 'start': start.isoformat(), 'end': end.isoformat(), 'interval': interval,
        'base_currency': base_currency, 'status': 'available' if available else 'unavailable',
        **({} if available else {'reason': 'position_valuation_unavailable'}),
        'data_completeness': 'partial', 'return_scope': 'investment_positions',
        'coverage_notes': ['投资持仓按最近确认数量和截至估值日的行情计算，收益扣除证券成交资金。'
                           '现金余额和账户出入金独立核算。缺少价格或成交净额的标的仅影响相关区间。']
                          + (['累计净值推算的历史价格为估算。'] if supplemental_prices else []),
        'rows': rows,
    }

