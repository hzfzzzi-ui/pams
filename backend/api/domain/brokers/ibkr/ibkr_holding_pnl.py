"""Build IBKR holding P&L from verified positions, executions and cash events."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from domain.imports.source_original_read import SourceOriginals


_INCOME_TYPES = ('Dividends', 'Payment In Lieu Of Dividends', 'Withholding Tax')


def _contract(attributes):
    return attributes.get('conid'), attributes.get('currency')


def _ledger_counts(cur, day):
    # Count existing facts independently of the current archive. A rolling Flex
    # report must not silently drop older booked gains or dividend payments.
    cur.execute("""SELECT 'trade' AS kind, i.raw_fields->'official_attributes'->>'conid' AS conid,
        f.symbol,f.currency,count(*) AS n
        FROM public.pams_investment_trade_fact f JOIN public.source_items i USING (source_item_id)
        WHERE f.account_code='ibkr' AND f.trade_date<=%s
        GROUP BY 1,2,3,4
        UNION ALL
        SELECT 'cash',i.raw_fields->'official_attributes'->>'conid',
            i.raw_fields->'official_attributes'->>'symbol',f.currency,count(*)
        FROM public.pams_cashflow_fact f JOIN public.source_items i USING (source_item_id)
        WHERE f.account_code='ibkr' AND f.event_date<=%s AND f.summary=ANY(%s)
        GROUP BY 1,2,3,4""", (day, day, list(_INCOME_TYPES)))
    return cur.fetchall()


def _report_components(cur, originals, source, holdings):
    from domain.brokers.ibkr.ibkr_daily_holdings import read_report
    from domain.brokers.ibkr.ibkr_history_backfill import read_candidates, reconcile_candidates

    day = source['period_end']
    account = source['attributes']['accountId']
    content, verified = originals.read(source)
    if not verified:
        return {}
    content = bytes(content)
    digest, positions, _ = read_report(content, account, day)
    candidates, pending, _ = read_candidates(content, account, digest, day)
    if pending:
        return {}
    matched, _ = reconcile_candidates(cur, candidates, account, current_digest=digest)
    entries = {kind: defaultdict(list) for kind in candidates}
    for kind, events in candidates.items():
        for uid, (fields, origin, _) in events.items():
            attributes = origin['official_attributes']
            if kind == 'trade' or fields['summary'] in _INCOME_TYPES:
                entries[kind][_contract(attributes)].append((uid, fields, attributes))
    counts = _ledger_counts(cur, day)
    results = {}
    for holding in holdings:
        conid, currency = key = _contract(holding['attributes'])
        if (not conid or conid != holding['source_uid'] or currency != holding['currency']
                or conid not in positions or holding['attributes'] != positions[conid][0]):
            continue
        if any(row['conid'] is None and row['currency'] == currency
               and row['symbol'] in (None, '', holding['symbol']) for row in counts):
            continue
        expected = {kind: sum(row['n'] for row in counts if row['kind'] == kind
                              and (row['conid'], row['currency']) == key) for kind in entries}
        if any(len(entries[kind][key]) != expected[kind] or
               any(uid not in matched[kind] for uid, _, _ in entries[kind][key]) for kind in entries):
            continue
        try:
            official_position = positions[conid][1]
            if (holding['cost'] != official_position['cost'].quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)
                    or holding['quantity'] != official_position['quantity'].quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)):
                continue
            quantity = Decimal(0)
            realized = Decimal(0)
            income = Decimal(0)
            for _, fields, raw in entries['trade'][key]:
                shares = Decimal(fields['quantity'])
                if fields['trade_type'] == 'buy':
                    quantity += shares
                else:
                    quantity -= shares
                    realized += Decimal(raw['fifoPnlRealized'])
            for _, fields, raw in entries['cash'][key]:
                if fields['summary'] == 'Withholding Tax' and 'dividend' not in raw.get('description', '').lower():
                    raise ValueError('withholding is not attributable to dividend income')
                income += Decimal(fields['net_cash'])
            if quantity == holding['quantity'] and all(x.is_finite() for x in (realized, income)):
                results[holding['id']] = (realized, income)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return results


def enrich_ibkr_holding_pnl(cur, rows):
    """Keep verified cash components tied to the broker snapshot as prices move."""
    candidates = [row for row in rows if row.get('account') == 'ibkr'
                  and row.get('holding_id') and row.get('cost') is not None
                  and row.get('quantity') and row['quantity'] > 0]
    if not candidates:
        return rows
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS redesigned")
    if not cur.fetchone()['redesigned']:
        return rows
    cur.execute("""SELECT h.id,h.symbol,h.currency,h.quantity,h.cost,h.as_of,
        i.source_uid,i.raw_fields AS attributes,r.period_end,r.sha256,
        r.source_id AS original_source_id,r.account_code AS source_account,r.content IS NOT NULL AS inline_original
        FROM public.pams_holdings_snapshot h JOIN public.source_items i USING (source_item_id)
        JOIN public.source_records r USING (source_id)
        WHERE h.id=ANY(%s) AND h.account_code='ibkr' AND r.account_code=h.account_code
          AND r.source_name='ibkr_daily_holdings' AND r.document_type='holding' AND r.status='published'
          AND i.review_status='confirmed' AND r.period_end=h.as_of""", ([row['holding_id'] for row in candidates],))
    holdings = cur.fetchall()
    groups = defaultdict(list)
    by_id = {holding['id']: holding for holding in holdings}
    for holding in holdings:
        groups[holding['original_source_id']].append(holding)
    originals = SourceOriginals(cur)
    results = {}
    for group in groups.values():
        try:
            results.update(_report_components(cur, originals, group[0], group))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    for row in candidates:
        identifier = row['holding_id']
        if identifier not in results:
            continue
        holding = by_id[identifier]
        if any(row[field] != holding[field] for field in ('symbol', 'currency', 'quantity', 'cost')):
            continue
        realized, income = results[identifier]
        floating = row['market_value'] - row['cost']
        total = floating + realized + income
        rate = (row['cost_cny'] / row['cost'] if row.get('cost_cny') is not None and row['cost'] else
                row['market_value_cny'] / row['market_value'] if row.get('market_value_cny') is not None and row['market_value'] else None)
        row.update(holding_pnl=total, holding_pnl_native=total,
                   holding_pnl_cny=None if rate is None else total * rate,
                   realized_trade_pnl=realized, realized_trade_pnl_native=realized,
                   realized_trade_pnl_cny=None if rate is None else realized * rate,
                   realized_trade_pnl_source='verified_ibkr_flex',
                   net_distributions=income, net_distributions_cny=None if rate is None else income * rate)
    return rows
