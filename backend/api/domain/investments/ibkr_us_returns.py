"""CNY performance of the verified IBKR US securities sleeve."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from functools import lru_cache
import os

from domain.brokers.ibkr.ibkr_daily_close import _document
from domain.brokers.ibkr.ibkr_daily_holdings import MARKET_LABELS, _read_day
from domain.brokers.ibkr.ibkr_history_backfill import FACT_TABLES, SOURCE, _existing_matches, read_candidates
from domain.investments.holdings_view import _table_exists
from domain.investments.investment_return_math import (
    bucket_flow_events, cash_flow_adjusted_return_rows, norm, section_return_summary,
)


FX_SOURCE = 'openexchangerates'
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


def _verify_published(cur, digest, candidates):
    for kind, table in FACT_TABLES.items():
        cur.execute(f"""SELECT f.*, i.source_uid, i.raw_fields, i.parsed_fields, i.review_status,
                   r.source_name,r.status AS source_status
            FROM public.{table} f JOIN public.source_items i USING (source_item_id)
            JOIN public.source_records r USING (source_id)
            WHERE f.account_code='ibkr' AND r.account_code='ibkr' AND r.sha256=%s AND r.source_name=%s""",
                    (digest, SOURCE))
        seen = set()
        for row in cur.fetchall():
            uid = row['source_uid']
            entry = candidates[kind].get(uid)
            if uid in seen or not entry or row['raw_fields'] != entry[1] or not _existing_matches(row, entry[0], kind):
                raise ValueError('Flex row differs from the published fact')
            seen.add(uid)
        if seen != candidates[kind].keys():
            raise ValueError('Flex facts are not all published')


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
    dates = [day for day in values if start <= day <= through]
    if not dates:
        return None
    if any(day not in rates for day in dates):
        return None
    cny_values = {day: values[day] * rates[day] * owner_ratio for day in dates}
    events = [(day, amount * rates[day] * owner_ratio) for day, amount in native_flows.items()
              if dates[0] < day <= dates[-1]]
    flows = bucket_flow_events(dates, events)
    rows = cash_flow_adjusted_return_rows(dates=dates, values=cny_values, flows=flows)
    for row in rows:
        row['display_return_rate'] = row['time_weighted_return_rate']
    summary = section_return_summary(rows=rows, reported=None)
    if interval == 'weekly':
        # Compound every observed day first, then select each week's last observation.
        by_week = {row['date'].isocalendar()[:2]: row for row in rows}
        rows = [rows[0], *(row for row in by_week.values() if row is not rows[0])]
    return {
        'section': 'us_stock', 'label': '美股', 'status': 'available',
        'base_currency': 'CNY', 'interval': interval, 'method': 'cash_flow_adjusted_time_weighted_return',
        'start': dates[0].isoformat(), 'end': dates[-1].isoformat(), 'source_through': dates[-1].isoformat(),
        'fx_source': FX_SOURCE,
        'coverage_notes': ['按 IBKR 美股证券估值日计算，以区间首日收盘为基准，按日末资金流调整，计入已核对的股息及预扣税。'
                           '账户融资、账户级费用及其他市场不计入美股证券收益。'],
        'summary': {key: norm(value) for key, value in summary.items()},
        'rows': [{key: norm(value) for key, value in row.items()} for row in rows],
    }


def build_verified_us_return(cur, *, start, end, interval, owner_ratio):
    account = os.environ.get('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', '').strip()
    if not account or owner_ratio <= 0 or not _table_exists(cur, 'ibkr_source_originals'):
        return None
    cur.execute("""SELECT r.period_end,r.sha256,o.content
        FROM public.source_records r JOIN public.ibkr_source_originals o USING (sha256)
        WHERE r.account_code='ibkr' AND r.source_name='ibkr_daily_holdings'
          AND r.document_type='holding' AND r.acquisition_method='api'
          AND r.status='published' AND r.period_start=r.period_end
          AND EXISTS (SELECT 1 FROM public.source_records t WHERE t.sha256=r.sha256
                      AND t.account_code=r.account_code AND t.source_name=%s
                      AND t.document_type='trade' AND t.status='published')
        ORDER BY r.period_end DESC LIMIT 1""", (SOURCE,))
    report = cur.fetchone()
    if not report:
        return None
    try:
        candidates, positions = _read_archive(bytes(report['content']), account, report['sha256'], report['period_end'])
        _verify_published(cur, report['sha256'], candidates)
        values, flows = _native_inputs(positions, candidates)
    except (ValueError, ArithmeticError, KeyError):
        return None
    cur.execute("""SELECT as_of, rate FROM public.pams_fx_rates_daily
        WHERE base_currency='CNY' AND quote_currency='USD' AND source=%s
          AND as_of BETWEEN %s AND %s AND rate>0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')""",
                (FX_SOURCE, start, end))
    rates = {row['as_of']: row['rate'] for row in cur.fetchall()}
    return _payload(values, flows, rates, start=start, through=min(end, report['period_end']),
                    interval=interval, owner_ratio=owner_ratio)
