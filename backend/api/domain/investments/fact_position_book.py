"""Roll trade units from opening positions and price the remaining units at each close."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.investments.fund_closed_sessions import mainland_mark_floor
from domain.investments.fund_return_history import (
    BUYS,
    SELLS,
    FUND_ASSET_TYPES,
    _trade_day,
    load_fund_nav_marks,
)
from domain.investments.redemption_settlement import load_redemption_receivables
from domain.investments.subscription_settlement import subscription_refunds
from domain.investments.trade_read_v2 import load_trade_rows
from domain.investments.wealth_valuation import load_reviewed_wealth_anchors
from domain.investments.valuation_input_evidence import current_holding_source
from domain.market.quote_valuation import (
    GOLD_BENCHMARK_ASSET,
    GOLD_CLOSE_SOURCE,
    uses_gold_close,
)


def dated_fund_book(cur, code, start, days):
    trades = load_trade_rows(
        cur,
        account_code=code,
        start=date.min,
        end=days[-1],
        include_fund_execution=True,
    )
    cur.execute(
        f"""SELECT h.id,h.asset_id,h.symbol,h.quantity, a.asset_type,{uses_gold_close("h")} AS gram_gold,as_of
        FROM pams_holdings_snapshot h LEFT JOIN pams_asset_master a ON a.asset_id=h.asset_id WHERE h.account_code=%s AND (as_of=%s OR {uses_gold_close("h")})""",
        (code, start),
    )
    anchors = [
        r
        for r in cur.fetchall()
        if current_holding_source(cur, r["id"])["status"] == "recorded"
    ]
    gold = {r["asset_id"] for r in anchors if r["gram_gold"]}
    opening = {
        r["asset_id"]: r["quantity"]
        for r in anchors
        if r["as_of"] == start and r["quantity"] is not None
    }
    funds = {
        r["asset_id"]: {"primary_symbol": r["symbol"]}
        for r in trades
        if r.get("asset_type") in FUND_ASSET_TYPES
    }
    funds.update(
        {
            r["asset_id"]: {"primary_symbol": r["symbol"]}
            for r in anchors
            if r["as_of"] == start and r["asset_type"] in FUND_ASSET_TYPES
        }
    )
    if (not trades and not opening) or any(
        r["source_evidence_status"] != "verified" or r["currency"] != "CNY"
        for r in trades
    ):
        return {}
    days = [start, *days]
    floor = min(mainland_mark_floor(d) for d in days)
    quotes = load_fund_nav_marks(cur, assets=funds, start=floor, end=days[-1])
    if gold:
        cur.execute(
            """SELECT as_of,MIN(close_price) price FROM market_observations
            WHERE source=%s AND asset_id=%s AND observation_type='daily' AND as_of BETWEEN %s AND %s
            AND close_price>0 GROUP BY as_of HAVING COUNT(DISTINCT close_price)=1""",
            (GOLD_CLOSE_SOURCE, GOLD_BENCHMARK_ASSET, floor, days[-1]),
        )
        quotes.update({("gram_gold", r["as_of"]): r["price"] for r in cur.fetchall()})
    wealth = {r["asset_id"] for r in trades if r["asset_type"] == "wealth_management"}
    marked = (
        {
            (r["asset_id"], r["date"]): r
            for r in load_reviewed_wealth_anchors(cur, accounts=[code], dates=days)
        }
        if wealth
        else {}
    )
    claims = load_redemption_receivables(cur, [code], days)
    refunds = subscription_refunds(cur, trades)
    units = defaultdict(Decimal, opening)
    events = sorted(trades, key=lambda r: (_trade_day(r), r["trade_id"]))
    index = 0
    results = {}
    for day in days:
        while index < len(events) and _trade_day(events[index]) <= day:
            r = events[index]
            index += 1
            asset = r["asset_id"]
            if (asset in opening and _trade_day(r) <= start) or asset in wealth:
                continue
            if r["trade_type"] in BUYS | SELLS:
                if r["quantity"] is None or (asset not in funds and asset not in gold):
                    return results
                units[asset] += (
                    r["quantity"] if r["trade_type"] in BUYS else -r["quantity"]
                )
        total = Decimal(0)
        price_dates = {}
        for asset, qty in units.items():
            if not qty or asset in wealth:
                continue
            symbol = (
                "gram_gold"
                if asset in gold
                else funds.get(asset, {}).get("primary_symbol")
            )
            price_day = day
            limit = mainland_mark_floor(day)
            while price_day > limit and (symbol, price_day) not in quotes:
                price_day -= timedelta(days=1)
            price = quotes.get((symbol, price_day))
            if qty < 0 or price is None:
                total = None
                break
            total += qty * price
            price_dates[symbol] = price_day
        if total is not None:
            for asset in wealth:
                orders = [r for r in trades if r["asset_id"] == asset]
                purchase = min(
                    (_trade_day(r) for r in orders if r["trade_type"] in BUYS),
                    default=date.max,
                )
                sale = min(
                    (
                        c["confirmation"]
                        for c in claims
                        if c["trade_id"] in {r["trade_id"] for r in orders}
                    ),
                    default=min(
                        (_trade_day(r) for r in orders if r["trade_type"] in SELLS),
                        default=date.max,
                    ),
                )
                if purchase <= day < sale:
                    mark = marked.get((asset, day))
                    if not mark:
                        total = None
                        break
                    total += mark["amount"]
                    price_dates[asset] = mark["price_date"]
        if total is not None:
            total += sum(amount for paid, returned, amount in refunds if paid <= day < returned)
            total += sum(
                c["amount"] for c in claims if c["confirmation"] <= day < c["receipt"]
            )
        results[day] = (total, price_dates)
    return results
