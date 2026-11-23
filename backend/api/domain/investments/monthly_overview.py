"""Read the last completed calendar months from NAV, position and ledger evidence."""
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.investments import page_projection
from domain.investments.position_performance import build_position_return_series
from domain.ledger.ledger_view import build_ledger_workbench_payload


def money(value):
    return None if value is None else float(round(Decimal(str(value)), 2))


def difference(left, right):
    return None if left is None or right is None else money(Decimal(str(left)) - Decimal(str(right)))


def _net(row):
    if row.get('monthly_book') is not None:
        return money(row['monthly_book']['known_nav'])
    value = row.get('netWorth')
    return money(value if value is not None else row.get('availableNetWorth'))


def _investment_categories(changes, names):
    grouped = defaultdict(lambda: defaultdict(Decimal))
    labels = {'stock': 'A 股个股', 'fund': '基金', 'wealth': '银行理财', 'gold': '贵金属', 'reference': '员工持股',
              'money_market': '货币基金'}
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


def build_monthly_overview(cur, *, today, owner_ratio, excluded_accounts):
    last = today.replace(day=1) - timedelta(days=1)
    first = date(last.year, 1, 1)
    published = page_projection.read_page(cur, 'analysis', business_date=today)
    if not published:
        return {'months': []}
    analysis = published['data']
    nav = {row['date']: row for row in analysis['nav']['rows']}
    changes = defaultdict(lambda: defaultdict(Decimal))
    counted_income_sources = set()
    # Match the published series' original valuation window and universe.
    total = analysis['total']
    start = date.fromisoformat(total['start'])
    end = date.fromisoformat(total['end'])
    if end >= first and start <= last:
        build_position_return_series(cur, start=start, end=end, interval='daily', base_currency='CNY',
                                     owner_ratio=owner_ratio, excluded_accounts=excluded_accounts, monthly_changes=changes,
                                     counted_income_sources=counted_income_sources)
    symbols = sorted({symbol for items in changes.values() for _, _, symbol in items})
    cur.execute("""SELECT primary_symbol, min(coalesce(display_name,canonical_name)) AS name
                   FROM pams_asset_master WHERE primary_symbol=ANY(%s) GROUP BY primary_symbol
                   HAVING count(distinct coalesce(display_name,canonical_name))=1""", (symbols,))
    names = {row['primary_symbol']: row['name'] for row in cur.fetchall()}
    ledger = build_ledger_workbench_payload(cur, view='monthly_close', month=last.strftime('%Y-%m'),
                                           counted_income_sources=counted_income_sources)
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
        delta = difference(value, prior)
        items = changes[opening]
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
        pnl = money(sum(items.values())) if items and not missing_yield and end >= closing and start <= opening else None
        cash = difference(totals.get('income'), totals.get('expense'))
        gap = difference(difference(delta, cash), pnl)
        book = closing_row.get('monthly_book')
        months.append({
            'm': number, 'date': closing.isoformat(), 'open': prior, 'nav': value, 'delta': delta,
            'assets': money(closing_row.get('assets')), 'debts': money(closing_row.get('liabilities')),
            'book': book, 'partial': not book['complete'] if book else closing_row.get('netWorth') is None, 'inc': totals.get('income'), 'exp': totals.get('expense'),
            'cash': cash, 'pnl': pnl, 'gap': gap, 'invest': _investment_categories(items, names) if pnl is not None else {},
            'receipts': _categories(cats.get('income', [])), 'payments': _categories(cats.get('expense', [])),
        })
    return {'months': months, 'projection': published['projection']}
