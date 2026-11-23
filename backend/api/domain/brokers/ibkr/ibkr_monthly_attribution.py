"""Adapt verified daily broker movements to the common monthly investment categories."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.brokers.ibkr import ibkr_daily_close_read as archive
from domain.brokers.ibkr.ibkr_account_audit import DAILY_PNL_FIELDS
from domain.brokers.ibkr.ibkr_daily_close import _day, _document, _number, _one, _rows
from domain.brokers.ibkr.ibkr_daily_holdings import ACCOUNT
from domain.brokers.ibkr.ibkr_history_backfill import _cash
from domain.brokers.ibkr.ibkr_report_state import _calendar


ZERO = Decimal(0)
CENT = Decimal('.01')
LABELS = {
    'commissions': '交易佣金', 'transactionTax': '交易税', 'otherFees': '其他费用',
    'interest': '利息', 'dividends': '股息', 'withholdingTax': '预扣税',
    'changeInInterestAccruals': '应计利息变动', 'changeInDividendAccruals': '应计股息变动',
    'fxTranslation': '报表汇兑', 'netFxTrading': '外汇交易',
}


def _positions(statement, close):
    positions = {}
    for row in _rows(statement, 'OpenPosition'):
        if row.get('levelOfDetail') != 'SUMMARY':
            continue
        key = (row.attrib['currency'], row.attrib['symbol'], row.attrib['conid'])
        if key in positions:
            raise ValueError('duplicate security close')
        positions[key] = _number(row, 'positionValue') * _number(row, 'fxRateToBase')
    if abs(sum(positions.values(), ZERO) - close['position_value_base']) > CENT:
        raise ValueError('incomplete securities close')
    return positions


def _movements(evidence, closes, days, prior_day):
    previous = closes[prior_day.isoformat()]
    if prior_day in evidence.by_day:
        positions = _positions(_one(evidence.by_day[prior_day], 'opening statement'), previous)
    elif previous['nav_base'] == 0 and previous['position_value_base'] == 0 and previous['cash_base'] == 0:
        positions = {}
    else:
        raise ValueError('missing opening positions')
    parts, external, cash = defaultdict(Decimal), {}, {}
    trades = set()
    for day in days:
        st = _one(evidence.by_day[day], 'statement')
        close = closes[day.isoformat()]
        base = close['base_currency']
        if (close.get('status') != 'ok' or close.get('identity_evidence', {}).get('status') != 'ok'
                or base != previous['base_currency'] or _day(st.get('fromDate')) != day):
            raise ValueError('unverified account close')
        row = _one(_rows(st, 'ChangeInNAV'), 'ChangeInNAV')
        if _day(row.get('fromDate')) != day or _day(row.get('toDate')) != day:
            raise ValueError('unexpected movement dates')
        bridge = evidence.bridge(row, st, base, day, day)
        if abs(bridge['opening']-previous['nav_base']) > CENT or abs(bridge['closing']-close['nav_base']) > CENT:
            raise ValueError('noncontiguous closing balances')
        details = {k: _number(row, k) for k in DAILY_PNL_FIELDS - {'twr'} if k in row.attrib}
        if abs(bridge['closing']-bridge['opening']-bridge['flow']-sum(details.values(), ZERO)) > CENT:
            raise ValueError('unexplained statement movement')
        if bridge['flow']:
            external[day] = bridge['flow']
        if _number(row, 'assetTransfers'):
            raise ValueError('asset transfer requires security-level principal evidence')
        current = _positions(st, close)
        movements = {key: current.get(key, ZERO) - positions.get(key, ZERO) for key in positions.keys() | current.keys()}
        conversion_pnl = ZERO
        trades_section = _one(_rows(st, 'Trades'), 'trades')
        for trade in _rows(trades_section, 'Trade'):
            if trade.get('levelOfDetail') != 'EXECUTION':
                continue
            evidence.scope(trade, st, base, day, day, currency=False)
            trade_day = _day(trade.attrib['tradeDate'])
            # FX executed after a session may first affect a later official close.
            booked_fx = (trade.get('assetCategory') == 'CASH' and trade_day < day
                         and _day(trade.attrib['reportDate']) == day)
            if (trade_day != day and not booked_fx) or trade.attrib['tradeID'] in trades:
                raise ValueError('invalid execution identity')
            trades.add(trade.attrib['tradeID'])
            if trade.get('assetCategory') == 'CASH':
                bought, sold = trade.attrib['symbol'].split('.')
                if sold != trade.attrib['currency']:
                    raise ValueError('unverified FX pair')
                bought_rate = Decimal(1)
                if bought != base:
                    conversion = _one([r for r in _rows(st, 'ConversionRate') if
                                       r.get('fromCurrency') == bought and r.get('toCurrency') == base
                                       and _day(r.get('reportDate')) == day], 'FX trade closing rate')
                    bought_rate = _number(conversion, 'rate')
                conversion_pnl += (_number(trade, 'quantity') * bought_rate
                                   + _number(trade, 'proceeds') * _number(trade, 'fxRateToBase'))
                continue
            key = (trade.attrib['currency'], trade.attrib['symbol'], trade.attrib['conid'])
            movements[key] = movements.get(key, ZERO) + _number(trade, 'proceeds') * _number(trade, 'fxRateToBase')
        for (currency, symbol, _), value in movements.items():
            parts['overseas', currency, symbol] += value
        # Flex includes its marking of currency cash and other instruments in MTM.
        # Preserve the reported remainder separately from security-level price movement.
        cash_summary = _one([r for r in _rows(st, 'CashReportCurrency')
                             if r.get('currency') == 'BASE_SUMMARY'], 'cash summary')
        evidence.scope(cash_summary, st, base, day, day, currency=False)
        cash_fx = _number(cash_summary, 'fxTranslationGainLoss')
        parts['fx', base, '外币现金折算'] += cash_fx
        parts['fx', base, '换汇成交价差'] += conversion_pnl
        parts['broker_mark', base, '其他盯市与舍入'] += details.pop('mtm') - sum(movements.values(), ZERO) - cash_fx - conversion_pnl
        for name, amount in details.items():
            if amount and name not in LABELS:
                raise ValueError('unsupported nonzero P&L component')
            if amount:
                kind = 'fx' if name == 'fxTranslation' else 'broker_component'
                parts[kind, base, LABELS[name]] += amount
        for line in _rows(_one(_rows(st, 'CashTransactions'), 'cash transactions'), 'CashTransaction'):
            evidence.scope(line, st, base, day, day, currency=False)
            uid, fields = _cash(line.attrib, evidence.account, day)
            if fields['action'] not in ('deposit', 'withdraw'):
                if uid in cash or fields['business_date'] != day.isoformat():
                    raise ValueError('ambiguous income or fee evidence')
                cash[uid] = (day, fields['currency'], Decimal(fields['net_cash']))
        positions, previous = current, close
    return parts, external, cash


def _convert(parts, external, *, opening_nav, opening_rate, closing_rate, rates, share):
    result = {key: val * closing_rate * share for key, val in parts.items() if val}
    fx = opening_nav * (closing_rate - opening_rate)
    for day, amount in external.items():
        fx += amount * (closing_rate - rates[day])
    result['fx', 'CNY', '账户及期间净入金折算'] = fx * share
    return result


def _matched_cash(cur, code, cash):
    if not cash:
        return set()
    cur.execute("""SELECT i.source_uid, f.source_item_id, f.event_date, f.currency, f.amount
        FROM pams_cashflow_fact f JOIN source_items i USING(source_item_id)
        JOIN source_records r USING(source_id)
        WHERE f.account_code=%s AND r.account_code=f.account_code AND r.status='published'
          AND i.review_status='confirmed' AND i.source_uid=ANY(%s)""", (code, list(cash)))
    rows = cur.fetchall()
    if len({r['source_uid'] for r in rows}) != len(rows):
        raise ValueError('duplicate posted broker event')
    if any((r['event_date'], r['currency'], r['amount']) != cash[r['source_uid']] for r in rows):
        raise ValueError('posted broker event differs from statement')
    return {str(r['source_item_id']) for r in rows}


def monthly_attribution(cur, nav, *, excluded_accounts):
    book = {date.fromisoformat(day): row['monthly_book']['accounts'][ACCOUNT] for day, row in nav.items()
            if (row.get('monthly_book') or {}).get('accounts', {}).get(ACCOUNT, {}).get('method')
            == 'broker_cash_legs_and_executions'}
    if not book or ACCOUNT in excluded_accounts:
        return {}, {}
    source = archive.read_published_portfolio_series(cur)
    if not source or source.get('status') != 'ok':
        return {}, {}
    cur.execute('SELECT content FROM ibkr_source_originals WHERE sha256=%s', (source['source_sha256'],))
    original = cur.fetchone()
    if not original:
        return {}, {}
    evidence, _, error = _document(bytes(original['content']).decode('utf8'), source['account_id'], source['source_sha256'])
    if error:
        return {}, {}
    closes = source['daily_closes']
    calendar = _calendar()
    result, duplicates = {}, {}
    for end, account in sorted(book.items()):
        first = end.replace(day=1)
        prior = first - timedelta(days=1)
        try:
            base = account['currency']
            previous = book.get(prior)
            opening_day = calendar.date_to_session(prior.isoformat(), direction='previous').date()
            # An archive can also explicitly report a cashless opening on a holiday.
            if prior.isoformat() in closes:
                opening_day = prior
            end_day = _day(account['price_dates']['market_close'])
            days = sorted(d for d in evidence.by_day if opening_day < d <= end_day)
            required = calendar.sessions_in_range(first.isoformat(), end.isoformat())
            if (not days or end_day != days[-1] or not {s.date() for s in required}.issubset(days)
                    or end_day < required[-1].date()):
                continue
            start_close, end_close = closes[opening_day.isoformat()], closes[end_day.isoformat()]
            share = Decimal(account['owner_ratio'])
            if (start_close.get('identity_evidence', {}).get('status') != 'ok'
                    or start_close['base_currency'] != base or end_close['base_currency'] != base
                    or previous and (abs(start_close['nav_base'] - Decimal(previous['native'])) > CENT
                                     or Decimal(previous['owner_ratio']) != share)
                    or abs(end_close['nav_base'] - Decimal(account['native'])) > CENT):
                continue
            parts, external, cash = _movements(evidence, closes, days, opening_day)
            rates = {d: archive.historical_rate(cur, currency=base, base_currency='CNY', as_of=d) for d in external}
            if any(rate is None for rate in rates.values()):
                continue
            end_rate = Decimal(account['rate'])
            if previous is not None:
                opening_rate = Decimal(previous['rate'])
            elif start_close['nav_base'] == 0:
                opening_rate = end_rate
            else:
                opening_rate = archive.historical_rate(cur, currency=base, base_currency='CNY', as_of=prior)
                contribution = nav.get(prior.isoformat(), {}).get('account_contributions', {}).get(ACCOUNT)
                if (opening_rate is None or contribution is None
                        or abs(start_close['nav_base'] * opening_rate * share - Decimal(contribution)) > CENT):
                    continue
            converted = _convert(parts, external, opening_nav=start_close['nav_base'], opening_rate=opening_rate,
                                 closing_rate=end_rate, rates=rates, share=share)
            matched = _matched_cash(cur, ACCOUNT, cash)
        except (ValueError, KeyError, TypeError, ArithmeticError, IndexError):
            # A missing original, conversion or conflicting close leaves this month unverified.
            continue
        result[first] = {ACCOUNT: converted}
        duplicates[first] = matched
    return result, duplicates
