"""CNY performance of the verified IBKR US securities sleeve."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
import os

from domain.brokers.ibkr.ibkr_daily_close import _document
from domain.brokers.ibkr.ibkr_daily_holdings import MARKET_LABELS, _read_day
from domain.brokers.ibkr.ibkr_history_backfill import (
    DAILY_FACTS_SOURCE, FACT_TABLES, SOURCE, read_candidates, reconcile_candidates,
)
from domain.brokers.ibkr.ibkr_report_state import _calendar
from domain.investments.holdings_view import _table_exists
from domain.investments.investment_return_math import (
    bucket_flow_events, cash_flow_adjusted_return_rows, norm, section_return_summary,
)
from domain.investments.valuation_nav import series_dates
from domain.market.fx_rates import OPEN_EXCHANGE_RATES_SOURCE


FX_SOURCE = OPEN_EXCHANGE_RATES_SOURCE
INCOME = {'Dividends', 'Payment In Lieu Of Dividends', 'Withholding Tax'}


@lru_cache(maxsize=2)
def _read_archive(content, account, digest, through):
    candidates, pending, _ = read_candidates(content, account, digest, through)
    if pending:
        raise ValueError('unresolved Flex archive rows')
    evidence, _, error = _document(content.decode('utf8'), account, digest)
    if error:
        raise ValueError('unverified Flex archive')
    positions = {}
    for day in sorted(evidence.by_day):
        rows, complete = _read_day(evidence, day)
        if not complete:
            raise ValueError('incomplete daily securities scope')
        positions[day] = {conid: row for conid, (_, row) in rows.items()
                          if row['major_category'] == '美股' and row['quantity']}
    return candidates, positions


def _verify_published(cur, digest, account, through, candidates):
    matched, conflicts = reconcile_candidates(cur, candidates, account, current_digest=digest)
    if conflicts or any(matched[kind] != rows.keys() for kind, rows in candidates.items()):
        raise ValueError('Flex facts are not all verified and published')

    for kind, table in FACT_TABLES.items():
        day = 'trade_date' if kind == 'trade' else 'event_date'
        cur.execute(f"""SELECT EXISTS(
            SELECT 1 FROM public.{table} f JOIN public.source_items i USING (source_item_id)
            JOIN public.source_records r USING (source_id)
            WHERE f.account_code='ibkr' AND r.account_code='ibkr' AND r.source_name=%s
              AND (i.source_position->>'statement_day'<=%s
                OR (i.source_position->>'statement_day' IS NULL AND f.{day}<=%s))
              AND NOT (i.source_uid=ANY(%s::text[]))) AS omitted""",
                    (SOURCE, through.isoformat(), through, list(candidates[kind])))
        if cur.fetchone()['omitted']:
            raise ValueError('Flex archive omits earlier published activity')


def _native_inputs(positions, candidates):
    quantities = Counter()
    transactions = defaultdict(list)
    contracts = {conid for rows in positions.values() for conid in rows}
    flows = defaultdict(Decimal)
    values = {}
    for fields, origin, _ in candidates['trade'].values():
        raw = origin['official_attributes']
        conid = raw.get('conid')
        if MARKET_LABELS.get(raw.get('listingExchange') or raw.get('exchange')) != '美股':
            if conid in contracts:
                raise ValueError('US contract has conflicting market evidence')
            continue
        if fields['currency'] != 'USD' or not conid:
            raise ValueError('US execution lacks a USD contract')
        contracts.add(conid)
        day = date.fromisoformat(fields['business_date'])
        sign = 1 if fields['trade_type'] == 'buy' else -1
        transactions[day].append((conid, sign * Decimal(fields['quantity'])))
        flows[day] -= Decimal(fields['net_cash'])

    for fields, origin, _ in candidates['cash'].values():
        if fields['summary'] not in INCOME or origin['official_attributes'].get('conid') not in contracts:
            continue
        if fields['currency'] != 'USD':
            raise ValueError('US distribution is not denominated in USD')
        flows[date.fromisoformat(fields['business_date'])] -= Decimal(fields['net_cash'])

    if flows.keys() - positions.keys():
        raise ValueError('US cash movement has no corresponding broker valuation date')
    if not transactions or min(flows) < min(transactions):
        raise ValueError('US opening trade is not established')
    for day, rows in positions.items():
        for conid, quantity in transactions.get(day, ()):
            quantities[conid] += quantity
        actual = {conid: row['quantity'] for conid, row in rows.items()}
        if {conid: qty for conid, qty in quantities.items() if qty} != actual:
            raise ValueError('US executions do not reproduce broker quantities')
        if any(row['currency'] != 'USD' or row['quantity'] < 0 for row in rows.values()):
            raise ValueError('unsupported US holdings')
        if values or rows or day in flows:
            values[day] = sum((row['market_value'] for row in rows.values()), Decimal(0))
    return values, flows


def _payload(values, native_flows, rates, *, start, through, interval, owner_ratio):
    if not values or start > through:
        return None
    first_trade = min(values)
    pre_trade_end = min(through, first_trade - timedelta(days=1))
    pre_trade_dates = series_dates(start, pre_trade_end, interval) if start <= pre_trade_end else []
    dates = [day for day in values if start <= day <= through]
    if not dates and not pre_trade_dates:
        return None
    if any(day not in rates for day in dates):
        return None
    cny_values = {day: values[day] * rates[day] * owner_ratio for day in dates}
    events = [(day, amount * rates[day] * owner_ratio) for day, amount in native_flows.items()
              if dates and dates[0] < day <= dates[-1]]
    flows = bucket_flow_events(dates, events)
    rows = cash_flow_adjusted_return_rows(dates=dates, values=cny_values, flows=flows)
    for row in rows:
        row['display_return_rate'] = row['time_weighted_return_rate']
    summary = section_return_summary(rows=rows, reported=None)
    if interval == 'weekly' and rows:
        # Compound every observed day first, then select each week's last observation.
        by_week = {row['date'].isocalendar()[:2]: row for row in rows}
        rows = [rows[0], *(row for row in by_week.values() if row is not rows[0])]
    # Pad the chart before inception without moving the return's opening basis.
    zero_fields = ('market_value', 'net_flow', 'cumulative_capital', 'capital_return_rate',
                   'daily_return_rate', 'time_weighted_return_rate', 'display_return_rate')
    pre_trade_rows = [dict.fromkeys(zero_fields, Decimal(0)) | {'date': day} for day in pre_trade_dates]
    rows = pre_trade_rows + rows
    chart_gap_dates = {}
    if interval == 'daily' and len(dates) > 1:
        calendar = _calendar()
        for previous, current in zip(dates, dates[1:]):
            day = previous + timedelta(days=1)
            while day < current:
                chart_gap_dates[day.isoformat()] = 'missing' if calendar.is_session(day.isoformat()) else 'closed'
                day += timedelta(days=1)
    return {
        'section': 'us_stock', 'label': '美股', 'status': 'available',
        'base_currency': 'CNY', 'interval': interval, 'method': 'cash_flow_adjusted_time_weighted_return',
        'start': rows[0]['date'].isoformat(), 'end': rows[-1]['date'].isoformat(),
        'source_through': dates[-1].isoformat() if dates else None, 'first_trade_date': first_trade.isoformat(),
        'fx_source': FX_SOURCE,
        'chart_gap_dates': chart_gap_dates,
        'coverage_notes': ['按 IBKR 美股证券估值日计算，以区间首日收盘为基准，按日末资金流调整，计入已核对的股息及预扣税。'
                           '账户融资、账户级费用及其他市场不计入美股证券收益。'],
        'summary': {key: norm(value) for key, value in summary.items()},
        'rows': [{key: norm(value) for key, value in row.items()} for row in rows],
    }


def build_verified_us_return(cur, *, start, end, interval, owner_ratio):
    account = os.environ.get('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', '').strip()
    if not account or owner_ratio <= 0 or not _table_exists(cur, 'ibkr_source_originals'):
        return None
    cur.execute("""WITH reports AS (
        SELECT r.period_end,r.sha256, EXISTS (
            SELECT 1 FROM public.source_records p WHERE p.account_code=r.account_code
              AND p.sha256=r.sha256 AND p.source_name=%s AND p.document_type='statement'
              AND p.status='published' AND p.period_end=r.period_end
        ) AS increment, EXISTS (
            SELECT 1 FROM public.source_records t WHERE t.account_code=r.account_code
              AND t.sha256=r.sha256 AND t.source_name=%s AND t.document_type='trade'
              AND t.status='published' AND t.period_end=r.period_end
        ) AS trade_archive
        FROM public.source_records r
        WHERE r.account_code='ibkr' AND r.source_name='ibkr_daily_holdings'
          AND r.document_type='holding' AND r.acquisition_method='api'
          AND r.status='published' AND r.period_start=r.period_end
    ), latest AS (
        SELECT period_end,sha256 FROM reports WHERE increment OR trade_archive
        ORDER BY period_end DESC LIMIT 3
    ), baseline AS (
        SELECT period_end,sha256 FROM reports WHERE trade_archive AND NOT increment
        ORDER BY period_end DESC LIMIT 1
    )
    SELECT period_end,sha256 FROM latest UNION SELECT period_end,sha256 FROM baseline
    ORDER BY period_end DESC""", (DAILY_FACTS_SOURCE, SOURCE))
    reports = cur.fetchall()
    if not reports:
        return None
    cur.execute("""SELECT as_of, rate FROM public.pams_fx_rates_daily
        WHERE base_currency='CNY' AND quote_currency='USD' AND source=%s
          AND as_of BETWEEN %s AND %s AND rate>0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')""",
                (FX_SOURCE, start, end))
    rates = {row['as_of']: row['rate'] for row in cur.fetchall()}
    best = None
    pending_fx = None
    for report in reports:
        through = min(end, report['period_end'])
        if best and best['end'] >= through.isoformat():
            break
        cur.execute('SELECT content FROM public.ibkr_source_originals WHERE sha256=%s', (report['sha256'],))
        original = cur.fetchone()
        if not original:
            continue
        try:
            candidates, positions = _read_archive(
                bytes(original['content']), account, report['sha256'], report['period_end'])
            _verify_published(cur, report['sha256'], account, report['period_end'], candidates)
            values, flows = _native_inputs(positions, candidates)
        except (ValueError, ArithmeticError, KeyError):
            continue
        missing_fx = next((day for day in values if start <= day <= through and day not in rates), None)
        if missing_fx:
            if pending_fx is None:
                pending_fx = missing_fx
            # Keep the verified prefix, but do not substitute a pre-inception zero
            # curve for a period where all actual valuations lack exchange rates.
            through = missing_fx - timedelta(days=1)
            if not any(start <= day <= through for day in values):
                continue
        payload = _payload(values, flows, rates, start=start, through=through,
                           interval=interval, owner_ratio=owner_ratio)
        if payload and (not best or payload['end'] > best['end']):
            best = payload
    if pending_fx is not None:
        if best is None:
            return {'section': 'us_stock', 'status': 'unavailable', 'reason': 'missing_historical_fx',
                    'fx_source': FX_SOURCE, 'missing_fx_date': pending_fx.isoformat(), 'rows': []}
        if pending_fx.isoformat() > best['end']:
            best['missing_fx_date'] = pending_fx.isoformat()
    return best
