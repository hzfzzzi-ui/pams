"""Read-only Tonghuashun NAV estimate from reconciled cash and unadjusted prices."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from domain.imports.account_import_parse import _csv, _xlsx
from domain.imports.ths_asset_candidates import PROFILE
from domain.investments.trade_read_v2 import load_trade_rows
from domain.market.tencent_stock_history import SOURCE

EXCHANGES = {'上海Ａ股': 'sh', '深圳Ａ股': 'sz'}
CALENDAR = 'sh000001'
SECURITY_ACTIONS = {'buy', 'sell', 'dividend', 'tax'}
CASH_ACTIONS = {'deposit', 'withdraw', 'interest'}


def read_reconciled_events(cur):
    cur.execute("""SELECT source_id, file_format, public.source_original_content(account_code,sha256,content) AS content, sha256 FROM source_records
        WHERE account_code='tonghuashun' AND status='published' AND document_type='trade'
        ORDER BY created_at LIMIT 2""")
    sources = cur.fetchall()
    if len(sources) != 1 or sources[0]['file_format'] not in {'xls', 'xlsx', 'csv', 'tsv'}:
        return None
    source = sources[0]
    content = bytes(source['content'] or b'')
    if not content or sha256(content).hexdigest() != source['sha256']:
        return None
    facts = load_trade_rows(cur, account_code='tonghuashun', start=date.min, end=date.max)
    cur.execute("""SELECT source_item_id, raw_fields->'source' AS origin
        FROM source_items WHERE source_id=%s AND parse_method='template' AND review_status='confirmed'""",
                (str(source['source_id']),))
    origins = {item['source_item_id']: item['origin'] for item in cur.fetchall()}
    try:
        (_, table), = (_xlsx(content) if source['file_format'] == 'xlsx' else _csv(content))
        headers = [str(cell).strip() for cell in table[0]]
        business_date, amount, balance = (headers.index(name) for name in ('成交日期', '发生金额', '本次金额'))
        rows = {number for number, cells in enumerate(table[1:], 2) if any(str(cell).strip() for cell in cells)}
        if not facts or len(rows) != len(facts):
            return None
        events = []
        for fact in facts:
            origin = origins[fact['source_item_id']]
            if (origin.get('profile') != PROFILE or fact['source_evidence_status'] != 'verified'
                    or fact['currency'] != 'CNY' or fact['net_cash'] is None
                    or fact['trade_type'] not in SECURITY_ACTIONS | CASH_ACTIONS):
                return None
            number = origin['row']
            rows.remove(number)
            cells = table[number - 1]
            original_day = datetime.strptime(str(cells[business_date]).strip().replace('-', ''), '%Y%m%d').date()
            broker_cash = Decimal(str(cells[balance]).strip())
            if (original_day != fact['trade_date'] or Decimal(str(cells[amount]).strip()) != fact['net_cash']
                    or not broker_cash.is_finite()):
                return None
            events.append({**fact, 'broker_cash': broker_cash, 'row_number': number})
    except (IndexError, KeyError, ValueError, TypeError, InvalidOperation):
        return None
    return sorted(events, key=lambda event: (event['trade_date'], -event['row_number']))


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
        for event in activity:
            cash += event['net_cash']
            if event['trade_type'] in {'buy', 'sell'}:
                direction = 1 if event['trade_type'] == 'buy' else -1
                quantities[event['symbol']] += event['quantity'] * direction
                if quantities[event['symbol']] < 0:
                    return {}
        if activity and cash not in {event['broker_cash'] for event in activity}:
            return {}
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
    events = read_reconciled_events(cur)
    symbols = securities_by_symbol(events) if events else None
    if not events or symbols is None:
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
