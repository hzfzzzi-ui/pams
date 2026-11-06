"""Read-only Tonghuashun NAV estimate from reconciled cash and unadjusted prices."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from domain.imports.account_import_parse import _csv, _xlsx
from domain.imports.account_import_ths import stable_reference
from domain.imports.source_original_read import SourceOriginals
from domain.imports.ths_asset_candidates import PROFILE
from domain.investments.trade_read_v2 import load_trade_rows
from domain.market.tencent_stock_history import SOURCE

EXCHANGES = {'上海Ａ股': 'sh', '深圳Ａ股': 'sz'}
CALENDAR = 'sh000001'
SECURITY_ACTIONS = {'buy', 'sell', 'dividend', 'tax'}
CASH_ACTIONS = {'deposit', 'withdraw', 'interest'}


def _original_rows(content, file_format):
    (_, table), = (_xlsx(content) if file_format == 'xlsx' else _csv(content))
    headers = [str(cell).strip() for cell in table[0]]
    for number, cells in enumerate(table[1:], 2):
        if not any(str(cell).strip() for cell in cells):
            continue
        raw = {key: str(value).strip() for key, value in zip(headers, cells) if key}
        day = datetime.strptime(raw['成交日期'].replace('-', ''), '%Y%m%d').date()
        # Compare complete source rows when proving a duplicate across exports.
        reference = (stable_reference(raw.get('定位串')) or stable_reference(raw.get('成交编号')))
        try:
            balance = Decimal(raw['本次金额'])
            amount = Decimal(raw['发生金额'])
        except (KeyError, InvalidOperation):
            balance = amount = None
        if balance is not None and (not balance.is_finite() or not amount.is_finite()):
            balance = amount = None
        yield number, (day, amount, balance, reference, raw)


def _read_event_prefix(cur):
    originals = SourceOriginals(cur)
    facts = load_trade_rows(cur, account_code='tonghuashun', start=date.min, end=date.max, originals=originals)
    if not facts:
        return [], None
    cur.execute("""SELECT r.source_id AS original_source_id, r.account_code AS source_account,
        r.sha256, r.content IS NOT NULL AS inline_original, r.file_format,
        i.source_item_id, i.raw_fields->'source' AS origin, i.parse_method
        FROM source_records r JOIN source_items i USING (source_id)
        WHERE r.account_code='tonghuashun' AND r.status='published' AND r.document_type='trade'
        ORDER BY r.created_at, r.source_id""")
    records = cur.fetchall()
    origins = {row['source_item_id']: row for row in records}
    sheets = {}
    try:
        seen = set()
        for row in records:
            source_id = row['original_source_id']
            if source_id in seen:
                continue
            seen.add(source_id)
            if (row['file_format'] not in {'xls', 'xlsx', 'csv', 'tsv'} or row['parse_method'] != 'template'
                    or (row['origin'] or {}).get('profile') != PROFILE):
                continue
            content, verified = originals.read(row)
            if not verified:
                return [], None
            if row['sha256'] not in sheets:
                sheets[row['sha256']] = dict(_original_rows(bytes(content), row['file_format']))
    except (IndexError, KeyError, ValueError, TypeError, InvalidOperation):
        # An unreadable date cannot be placed after an already-verified interval.
        return [], None

    events = {}
    by_reference = defaultdict(list)
    gaps = []
    for fact in facts:
        row = origins.get(fact['source_item_id'])
        origin = (row or {}).get('origin') or {}
        key = ((row or {}).get('sha256'), origin.get('row'))
        original = sheets.get(key[0], {}).get(key[1])
        if (not original or row['parse_method'] != 'template' or origin.get('profile') != PROFILE
                or fact['source_evidence_status'] != 'verified' or fact['currency'] != 'CNY'
                or fact['trade_type'] not in SECURITY_ACTIONS | CASH_ACTIONS
                or original[2] is None or original[:2] != (fact['trade_date'], fact['net_cash'])
                or key in events):
            gaps.append(fact['trade_date'])
            if original:
                gaps.append(original[0])
            continue
        day, _, balance, reference, raw = original
        events[key] = {**fact, 'broker_cash': balance, 'row_number': key[1], 'source_digest': key[0]}
        if reference:
            by_reference[day, reference].append(raw)

    for digest, sheet in sheets.items():
        for number, (day, _, _, reference, raw) in sheet.items():
            if (digest, number) in events:
                continue
            matches = by_reference.get((day, reference), [])
            if not reference or len(matches) != 1 or matches[0] != raw:
                gaps.append(day)

    gap = min(gaps, default=None)
    # Preserve the broker's reverse row ordering within each original.
    order = {digest: index for index, digest in enumerate(sheets)}
    prefix = sorted(
        ((key, event) for key, event in events.items() if gap is None or event['trade_date'] < gap),
        key=lambda item: (item[1]['trade_date'], order[item[0][0]], -item[0][1]),
    )
    events = [event for _, event in prefix]
    if events:
        day = events[0]['trade_date']
        opening = [event for event in events if event['trade_date'] == day]
        if len({event['source_digest'] for event in opening}) > 1:
            ordered = _cash_chain(opening, None)
            if not ordered:
                return [], day
            events[:len(opening)] = ordered
    return events, gap


def read_reconciled_events(cur):
    """Require a complete source history for the dedicated price backfill."""
    events, gap = _read_event_prefix(cur)
    return events if events and gap is None else None


def securities_by_symbol(events):
    symbols = {}
    for event in events:
        if event['trade_type'] not in {'buy', 'sell'}:
            continue
        symbol = event['symbol']
        prefix = EXCHANGES.get(event['source_market'])
        if (not prefix or not symbol or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit()
                or not event['quantity'] or event['quantity'] <= 0
                or symbols.setdefault(symbol, prefix + symbol) != prefix + symbol):
            return None
    return symbols


def _cash_chain(activity, opening_cash):
    by_opening = defaultdict(list)
    for event in activity:
        by_opening[event['broker_cash'] - event['net_cash']].append(event)
    if opening_cash is None:
        endings = {event['broker_cash'] for event in activity}
        starts = by_opening.keys() - endings
        if len(starts) != 1:
            return []
        opening_cash, = starts
    ordered = []
    for _ in activity:
        options = by_opening[opening_cash]
        if len(options) != 1:
            return []
        event = options.pop()
        ordered.append(event)
        opening_cash = event['broker_cash']
    return ordered


def daily_estimates(events, prices, dates, symbols):
    """Validate end-of-day cash against the original; return only covered valuations."""
    if not events or not dates:
        return {}
    events_by_day = defaultdict(list)
    for event in events:
        events_by_day[event['trade_date']].append(event)
    cash = events[0]['broker_cash'] - events[0]['net_cash']
    first_day, last_day = events[0]['trade_date'], events[-1]['trade_date']
    calendar = prices.get(CALENDAR, {})
    last_market_day = max(calendar, default=None)
    wanted = set(dates)
    quantities = defaultdict(Decimal)
    previous = {}
    result = {}
    day = first_day
    while day <= max(last_day, max(wanted)):
        activity = events_by_day[day]
        if len({event.get('source_digest') for event in activity}) > 1:
            activity = _cash_chain(activity, cash)
            if not activity:
                return result
        for event in activity:
            cash += event['net_cash']
            if event['trade_type'] in {'buy', 'sell'}:
                direction = 1 if event['trade_type'] == 'buy' else -1
                quantities[event['symbol']] += event['quantity'] * direction
                if quantities[event['symbol']] < 0:
                    return result
        if activity and cash not in {event['broker_cash'] for event in activity}:
            return result
        for provider_symbol in symbols.values():
            current = prices.get(provider_symbol, {}).get(day)
            if current is not None:
                previous[provider_symbol] = current
        if day in wanted:
            active = [symbol for symbol, qty in quantities.items() if qty]
            marks = previous if last_market_day and day < last_market_day and day not in calendar else {
                symbol: prices.get(symbol, {}).get(day) for symbol in symbols.values()
            }
            if all(marks.get(symbols[symbol]) is not None for symbol in active):
                result[day] = cash + sum((quantities[s] * marks[symbols[s]] for s in active), Decimal(0))
        day += timedelta(days=1)
    return result


def estimated_capital_performance(events, values, owner_ratio):
    """Calculate cumulative PnL and return after removing external capital flows."""
    if owner_ratio <= 0:
        return {}
    capital = events[0]['broker_cash'] - events[0]['net_cash']
    flows = iter(event for event in events if event['trade_type'] in {'deposit', 'withdraw'})
    flow = next(flows, None)
    result = {}
    for day, value in sorted(values.items()):
        while flow is not None and flow['trade_date'] <= day:
            capital += flow['net_cash']
            flow = next(flows, None)
        if capital > 0:
            pnl = value - capital
            result[day] = {'total_pnl': pnl * owner_ratio, 'display_return_rate': pnl / capital}
    return result


def project(cur, *, dates, owner_ratio, series_type):
    result = series_type(unknown_dates=dates, cash_scope_unverified=True)
    if not dates:
        return result
    events, gap = _read_event_prefix(cur)
    if gap:
        dates = [day for day in dates if day < gap]
    symbols = securities_by_symbol(events) if events else None
    if not dates or not events or symbols is None:
        return result
    cur.execute("""SELECT symbol, as_of, close_price FROM market_observations
        WHERE observation_type='daily' AND source=%s AND market='CN_A' AND currency='CNY'
          AND symbol=ANY(%s) AND as_of BETWEEN %s AND %s
          AND close_price>0 AND close_price::text NOT IN ('NaN','Infinity','-Infinity')""",
                (SOURCE, sorted(set(symbols.values()) | {CALENDAR}), events[0]['trade_date'], max(dates)))
    prices = defaultdict(dict)
    for row in cur.fetchall():
        prices[row['symbol']][row['as_of']] = row['close_price']
    values = daily_estimates(events, prices, dates, symbols)
    result.preview_by_date = {day: value * owner_ratio for day, value in values.items()}
    result.preview_performance_by_date = estimated_capital_performance(events, values, owner_ratio)
    result.preview_source_through = events[-1]['trade_date']
    return result
