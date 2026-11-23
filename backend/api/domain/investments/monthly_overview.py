"""Read the last completed calendar months from NAV, position and ledger evidence."""
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.brokers.ibkr.ibkr_monthly_attribution import monthly_attribution
from domain.accounts.monthly_cash_attribution import CASH_METHOD, monthly_cash_attribution
from domain.investments import page_projection
from domain.investments.capital_ownership import position_ownership, read_monthly_profit_reallocations
from domain.investments.trade_read_v2 import load_trade_rows
from domain.investments.valuation_nav import load_fx_timelines, fx_rate_at
from domain.investments.position_performance import build_position_return_series
from domain.ledger.ledger_view import build_ledger_workbench_payload


def money(value):
    return None if value is None else float(round(Decimal(str(value)), 2))


def difference(left, right):
    return None if left is None or right is None else money(Decimal(str(left)) - Decimal(str(right)))


def _net(row):
    if row.get('monthly_book') is not None:
        return row['monthly_book']['known_nav']
    value = row.get('netWorth')
    return value if value is not None else row.get('availableNetWorth')


def _investment_categories(changes, names):
    grouped = defaultdict(lambda: defaultdict(Decimal))
    labels = {'stock': 'A 股个股', 'fund': '基金', 'wealth': '银行理财', 'gold': '贵金属', 'reference': '员工持股',
              'money_market': '货币基金', 'fx': '汇兑变动', 'broker_component': '投资税费及收益',
              'broker_mark': '其他盯市'}
    for (kind, currency, symbol), amount in changes.items():
        label = labels.get(kind, {'USD': '美元证券', 'HKD': '港元证券', 'KRW': '韩元证券'}.get(currency, currency + '证券'))
        name = None if kind == 'money_market' else names.get(symbol)
        grouped[label][f'{name} · {symbol}' if name and name != symbol else symbol] += amount
    return {label: {'total': money(sum(parts.values())), 'parts': {k: money(v) for k, v in parts.items()}}
            for label, parts in grouped.items()}


def _categories(rows):
    return {row['category']: {'total': row['amount'], 'parts': {
        item['category']: item['amount'] for item in row['children']
    }} for row in rows}


def _cash_interest(cur, nav, start, end, owner_ratio, excluded):
    """Interest on uninvested broker cash is outside security marks."""
    by_account = defaultdict(set)
    for row in nav.values():
        for code, entry in (row.get('monthly_book') or {}).get('accounts', {}).items():
            if entry.get('method') == 'trade_cash_and_shares' and code not in excluded:
                by_account[code].add(row['date'][:7])
    events = []
    for account, months in by_account.items():
        for row in load_trade_rows(cur, account_code=account, start=start, end=end, trade_types=('interest',)):
            if (row['trade_date'].strftime('%Y-%m') in months and row.get('asset_id') is None
                    and row['symbol'] == 'CASH_' + row['currency']):
                events.append(row)
    if not events:
        return {}, set(), set()
    dates = sorted({r['trade_date'] for r in events})
    owners = position_ownership(cur, by_account, dates, owner_ratio)
    rates = load_fx_timelines(cur, 'CNY') if any(r['currency'] != 'CNY' for r in events) else {}
    amounts = defaultdict(Decimal)
    sources, missing = set(), set()
    for event in events:
        day = event['trade_date']
        month = day.replace(day=1)
        ratio = owners[event['account_code']].get(day)
        rate = fx_rate_at(rates, event['currency'], day, 'CNY')
        if (ratio is None or rate is None or event['source_evidence_status'] != 'verified'
                or event['net_cash'] is None):
            missing.add(month)
            continue
        amounts[month] += event['net_cash'] * rate * ratio
        sources.add(event['source_item_id'])
    return amounts, sources, missing


def _conversion_spreads(cur, ledger, nav, excluded_accounts, owner_ratio):
    included = {}
    for month, report in ledger.items():
        closing = month.replace(day=monthrange(month.year, month.month)[1])
        book = nav.get(closing.isoformat(), {}).get('monthly_book') or {}
        accounts = {code for code, row in book.get('accounts', {}).items()
                    if row.get('method') == CASH_METHOD and code not in excluded_accounts}
        included[month] = [s for s in report.get('conversion_settlements', []) if accounts.issuperset(s['accounts'])]
    pairs = [s for month in included.values() for s in month]
    if not pairs:
        return {}
    shares = position_ownership(cur, {code for pair in pairs for code in pair['accounts']},
                                {pair['day'] for pair in pairs}, owner_ratio)
    totals = {}
    for month, settlements in included.items():
        for settlement in settlements:
            ratios = {shares[code].get(settlement['day']) for code in settlement['accounts']}
            if len(ratios) != 1 or None in ratios or settlement['amount'] is None:
                totals[month] = None
                break
            totals[month] = totals.get(month, Decimal(0)) + settlement['amount'] * ratios.pop()
    return totals


def build_monthly_overview(cur, *, today, owner_ratio, excluded_accounts):
    last = today.replace(day=1) - timedelta(days=1)
    first = date(last.year, 1, 1)
    published = page_projection.read_page(cur, 'analysis', business_date=today)
    if not published:
        return {'months': []}
    analysis = published['data']
    nav = {row['date']: row for row in analysis['nav']['rows']}
    reallocations = read_monthly_profit_reallocations(cur, nav, excluded_accounts)
    changes = defaultdict(lambda: defaultdict(Decimal))
    statement_months, statement_cash = monthly_attribution(cur, nav, excluded_accounts=excluded_accounts)
    interest, counted_income_sources, missing_interest = _cash_interest(cur, nav, first, last, owner_ratio, excluded_accounts)
    for month, amount in interest.items():
        changes[month]['broker_component', 'CNY', '证券账户现金利息'] += amount
    # Match the published series' original valuation window and universe.
    total = analysis['total']
    start = date.fromisoformat(total['start'])
    end = date.fromisoformat(total['end'])
    if end >= first and start <= last:
        build_position_return_series(cur, start=start, end=end, interval='daily', base_currency='CNY',
                                     owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, monthly_changes=changes,
                                     counted_income_sources=counted_income_sources, statement_months=statement_months)
    for month, accounts in statement_months.items():
        for breakdown in accounts.values():
            for key, amount in breakdown.items():
                changes[month][key] += amount
    symbols = sorted({symbol for items in changes.values() for _, _, symbol in items})
    cur.execute("""SELECT primary_symbol, min(coalesce(display_name,canonical_name)) AS name
                   FROM pams_asset_master WHERE primary_symbol=ANY(%s) GROUP BY primary_symbol
                   HAVING count(distinct coalesce(display_name,canonical_name))=1""", (symbols,))
    names = {row['primary_symbol']: row['name'] for row in cur.fetchall()}
    ledger = build_ledger_workbench_payload(cur, view='monthly_close', month=last.strftime('%Y-%m'),
                                           counted_income_sources=counted_income_sources, statement_cash=statement_cash)
    cash_adjustments = monthly_cash_attribution(cur, nav, ledger, start=first, end=last)
    conversion_spreads = _conversion_spreads(cur, ledger, nav, excluded_accounts, owner_ratio)
    months = []
    for number in range(1, last.month + 1):
        opening = date(first.year, number, 1)
        closing = opening.replace(day=monthrange(first.year, number)[1])
        report = ledger.get(opening, {})
        totals = report.get('totals', {})
        cats = report.get('categories', {})
        closing_row = nav.get(closing.isoformat(), {})
        value = _net(closing_row)
        prior = _net(nav.get((opening - timedelta(days=1)).isoformat(), {}))
        net_change = Decimal(str(value)) - Decimal(str(prior)) if None not in (value, prior) else None
        delta = money(net_change)
        items = changes[opening]
        book = closing_row.get('monthly_book')
        missing_statements = sorted(code for code, account in (book or {}).get('accounts', {}).items()
                                    if account.get('method') == 'broker_cash_legs_and_executions'
                                    and code not in excluded_accounts and code not in statement_months.get(opening, {}))

        missing_yield = False
        for receipt in report.get('money_fund_returns', []):
            account = receipt['account']
            if account in excluded_accounts:
                continue
            share = owner_ratio(account)
            if not share:
                continue
            if receipt['amount'] is None:
                missing_yield = True
            else:
                items['money_market', 'CNY', receipt['name']] += Decimal(str(receipt['amount'])) * share
        has_position_period = items and end >= closing and start <= opening
        pnl_value = sum(items.values()) if (has_position_period and not missing_yield
                                             and not missing_statements and opening not in missing_interest) else None
        pnl = money(pnl_value)
        cash = difference(totals.get('income'), totals.get('expense'))
        ownership = reallocations.get(opening, {})
        adjustments = {}
        if opening in conversion_spreads:
            adjustments['换汇结算差额'] = conversion_spreads[opening]
        if ownership:
            adjustments['出资份额调整'] = sum(ownership.values()) if None not in ownership.values() else None
        if book and 'cash_transfer_fx' in book:
            transfer_fx = book['cash_transfer_fx']
            if transfer_fx is None or Decimal(str(transfer_fx)) != 0:
                adjustments['券商资金交收汇兑'] = transfer_fx
        accounts = cash_adjustments.get(opening, {})
        for field, label in (('exchange', '现金与欠款汇兑'), ('posting', '跨月入账')):
            if accounts:
                amounts = [account[field] for account in accounts.values()]
                adjustments[label] = sum(amounts) if None not in amounts else None
        known_adjustments = sum((Decimal(str(v)) for v in adjustments.values() if v is not None), Decimal(0))
        net_cash = totals.get('unrounded_net', cash)
        gap = (money(net_change - Decimal(str(net_cash)) - pnl_value - known_adjustments)
               if None not in (net_change, net_cash, pnl_value) and None not in adjustments.values() else None)
        months.append({
            'm': number, 'date': closing.isoformat(), 'open': money(prior), 'nav': money(value), 'delta': delta,
            'assets': money(closing_row.get('assets')), 'debts': money(closing_row.get('liabilities')),
            'book': book, 'partial': not book['complete'] if book else closing_row.get('netWorth') is None, 'inc': totals.get('income'), 'exp': totals.get('expense'),
            'missing_investment_statements': missing_statements,
            'adjustments': {label: money(amount) for label, amount in adjustments.items()}, 'cash_attribution': accounts, 'cash': cash, 'pnl': pnl, 'gap': gap, 'invest': _investment_categories(items, names) if pnl is not None else {},
            'receipts': _categories(cats.get('income', [])), 'payments': _categories(cats.get('expense', [])),
        })
    return {'months': months, 'projection': published['projection']}
