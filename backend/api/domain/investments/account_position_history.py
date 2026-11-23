"""Dated investment-account marks reconstructed from its own verified trades."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from domain.investments.fund_closed_sessions import mainland_mark_floor
from domain.investments.fund_return_history import (
    FUND_ASSET_TYPES, SELLS, _position_history, _trade_day, load_fund_nav_marks,
)
from domain.investments.redemption_settlement import load_redemption_receivables
from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.investments.trade_read_v2 import load_trade_rows
from domain.investments.wealth_valuation import load_reviewed_wealth_anchors
from domain.market.quote_valuation import GOLD_BENCHMARK_ASSET, GOLD_CLOSE_SOURCE, uses_gold_close


class PositionMarks(dict):
    def __init__(self, values):
        super().__init__(values)
        self.price_dates = defaultdict(dict)


def _quantity_marks(cur, dates, trades, holdings, gold_assets):
    """All reported share counts must agree with the verified order history."""
    by_asset = defaultdict(list)
    funds = {}
    for trade in trades:
        asset = trade['asset_id']
        if trade['asset_type'] == 'wealth_management':
            continue
        if trade['asset_type'] in FUND_ASSET_TYPES:
            funds[asset] = {'primary_symbol': trade['symbol']}
        elif asset not in gold_assets:
            return None
        by_asset[asset].append(trade)
    if set(holdings) - by_asset.keys():
        return None
    floors = {day: mainland_mark_floor(day) for day in dates}
    first = min(floors.values())
    prices = load_fund_nav_marks(cur, assets=funds, start=first, end=dates[-1])
    if gold_assets:
        cur.execute("""SELECT as_of, CASE WHEN COUNT(DISTINCT close_price)=1 THEN MIN(close_price) END AS price FROM market_observations
            WHERE observation_type='daily' AND source=%s AND asset_id=%s AND as_of BETWEEN %s AND %s
              AND close_price>0 AND close_price::text NOT IN ('NaN','Infinity','-Infinity')
            GROUP BY as_of""",
            (GOLD_CLOSE_SOURCE, GOLD_BENCHMARK_ASSET, first, dates[-1]))
        gold_prices = {row['as_of']: row['price'] for row in cur.fetchall()}
    values = PositionMarks(dict.fromkeys(dates, Decimal(0)))
    for asset, orders in by_asset.items():
        replay, issue = _position_history(orders, holdings.get(asset, {}), dates)
        if issue:
            return None
        units, _ = replay
        symbol = orders[0]['symbol']
        quotes = gold_prices if asset in gold_assets else {d: p for (sym, d), p in prices.items() if sym == symbol}
        for day in dates:
            if values[day] is None or not units[day]:
                continue
            price_day = day
            while price_day > floors[day] and price_day not in quotes:
                price_day -= timedelta(days=1)
            price = quotes.get(price_day)
            if price is None:
                values[day] = None
            else:
                values.price_dates[day][symbol] = price_day
                values[day] += (units[day] * price).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return values


def load_account_trade_marks(cur, code, dates, *, positions_only):
    """Return None for no trade history, or a series with every required member priced.

    A position-only estimate can omit cash. An account-wide estimate also
    requires evidence of outstanding sale proceeds until their actual receipt.
    The caller reconciles the series to an independently observed account total.
    """
    cur.execute("""SELECT f.trade_id, a.primary_symbol FROM pams_investment_trade_fact f
        LEFT JOIN pams_asset_master a ON a.asset_id=f.asset_id AND a.currency=f.currency
        WHERE f.account_code=%s AND f.trade_date<=%s""", (code, dates[-1]))
    identities = {r['trade_id']: r['primary_symbol'] for r in cur.fetchall()}
    if not identities:
        return None
    trades = load_trade_rows(cur, account_code=code, start=date.min, end=dates[-1], include_fund_execution=True)
    if identities.keys() != {r['trade_id'] for r in trades} or any(
        not r['asset_id'] or not r['symbol'] or r['currency'] != 'CNY'
        or r['symbol'] != identities[r['trade_id']] or r['source_evidence_status'] != 'verified' for r in trades
    ):
        return {}
    symbols = {r['asset_id']: r['symbol'] for r in trades}
    if any(symbols[r['asset_id']] != r['symbol'] for r in trades) or len(set(symbols.values())) != len(symbols):
        return {}
    cur.execute(f"""SELECT h.id,h.as_of,h.asset_id,h.symbol,h.quantity,h.currency,h.asset_liability,
                    {uses_gold_close('h')} AS gram_gold
        FROM pams_holdings_snapshot h WHERE h.account_code=%s AND h.as_of<=%s""", (code, dates[-1]))
    snapshots = cur.fetchall()
    holdings = defaultdict(lambda: defaultdict(set))
    gold = set()
    gold_days = {(code, r['as_of']) for r in snapshots if r['gram_gold']}
    coverage = load_account_snapshot_coverage(cur, gold_days) if gold_days else {}
    for row in snapshots:
        asset = row['asset_id']
        if (asset not in symbols or row['symbol'] != symbols[asset] or row['currency'] != 'CNY'
                or row['asset_liability'] != '资产' or row['quantity'] is None):
            return {}
        holdings[asset][row['as_of']].add(row['quantity'])
        if row['gram_gold'] and row['id'] in coverage[code, row['as_of']]['member_ids']:
            gold.add(asset)
    marks = _quantity_marks(cur, dates, trades, holdings, gold)
    if marks is None:
        return {}

    wealth = [r for r in trades if r['asset_type'] == 'wealth_management']
    sales = [r for r in trades if r['trade_type'] in SELLS]
    claims = ({c['trade_id']: c for c in load_redemption_receivables(cur, [code], dates)}
              if sales and (wealth or not positions_only) else {})
    # Shares valued on the pricing day may only be cancelled several sessions
    # later. A receipt claim starts on confirmation; never draw a temporary loss
    # in the intervening days or count the sold shares alongside that claim.
    for sale in sales:
        claim = claims.get(sale['trade_id'])
        if not claim:
            if not positions_only and sale['trade_date'] >= dates[0]:
                # The missing settlement affects the balance from this disposal.
                # Earlier fully valued holdings remain useful.
                for day in dates:
                    if day >= _trade_day(sale):
                        marks[day] = None
            continue
        for day in dates:
            if marks[day] is None:
                continue
            if sale['asset_type'] != 'wealth_management' and _trade_day(sale) <= day < claim['confirmation']:
                marks[day] = None
            elif not positions_only and claim['confirmation'] <= day < claim['receipt']:
                marks[day] += claim['amount']

    if wealth:
        wealth_marks = {(r['date'], r['asset_id']): r for r in
                        load_reviewed_wealth_anchors(cur, accounts=[code], dates=dates)}
        for asset in {r['asset_id'] for r in wealth}:
            orders = [r for r in wealth if r['asset_id'] == asset]
            purchases = [r for r in orders if r['trade_type'] == 'buy']
            sales = [r for r in orders if r['trade_type'] == 'sell']
            if len(orders) == 2 and len(sales) == 1 and sales[0]['trade_date'] < dates[0]:
                continue
            if len(orders) != 2 or len(purchases) != 1 or len(sales) != 1:
                for day in dates:
                    if day >= min(row['trade_date'] for row in orders):
                        marks[day] = None
                continue
            claim = claims.get(sales[0]['trade_id'])
            for day in dates:
                if marks[day] is not None and purchases[0]['trade_date'] <= day < (
                    claim['confirmation'] if claim else sales[0]['trade_date']
                ):
                    mark = wealth_marks.get((day, asset))
                    if mark:
                        marks[day] += mark['amount']
                        marks.price_dates[day][orders[0]['symbol']] = mark['price_date']
                    else:
                        marks[day] = None
    first_trade = min(_trade_day(row) for row in trades)
    for day in dates:
        if marks[day] is None or (positions_only and day < first_trade):
            marks.pop(day)
            marks.price_dates.pop(day, None)
    return marks
