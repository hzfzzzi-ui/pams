"""Reconstruct dated fund sleeve valuations from verified trades and unit NAVs."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re

import exchange_calendars

from domain.imports.source_original_read import SourceOriginals
from domain.investments.holdings.holdings_positions import _trade_order_ambiguous
from domain.investments.returns.investment_return_math import cash_flow_adjusted_return_rows, norm, return_chart_gap_dates
from domain.investments.trades.trade_read_v2 import load_trade_rows


FUND_ASSET_TYPES = ("ETF / 基金", "fund_cn", "fund", "基金")
BUYS = {"buy", "fund_subscription"}
SELLS = {"sell", "fund_redemption"}
FUND_TRADES = BUYS | SELLS | {"dividend", "fee", "tax"}
NAV_SOURCES = ("tushare_fund_nav", "eastmoney_f10")
METHOD = "fund_verified_trade_nav_replay"


def _number(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _business_day(value):
    try:
        return value if type(value) is date else date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _trade_day(row):
    return _business_day(row.get("fund_pricing_date")) or row["trade_date"]


def load_fund_history_inputs(cur, *, end, excluded_accounts=(), include_trades=True):
    """Use the instrument's asset master, holdings classification and explicit fund events."""
    cur.execute("""SELECT DISTINCT account_code FROM (
          SELECT h.account_code FROM pams_holdings_snapshot h
          WHERE h.minor_category='A股基金' AND h.asset_liability='资产' AND h.as_of<=%s
          UNION
          SELECT t.account_code FROM pams_investment_trade_fact t
          WHERE t.trade_date<=%s AND (t.trade_type IN ('fund_subscription','fund_redemption')
              OR t.asset_id IN (SELECT asset_id FROM pams_asset_master WHERE asset_type=ANY(%s)))
        ) candidates WHERE NOT (account_code=ANY(%s::text[])) ORDER BY account_code""",
                (end, end, list(FUND_ASSET_TYPES), sorted(excluded_accounts)))
    accounts = [row["account_code"] for row in cur.fetchall()]
    if not accounts:
        return {}, [], []
    cur.execute("""SELECT DISTINCT asset_id, primary_symbol, canonical_name, currency, asset_type
        FROM pams_asset_master WHERE asset_type=ANY(%s)
          AND asset_id IN (
            SELECT asset_id FROM pams_investment_trade_fact WHERE account_code=ANY(%s) AND trade_date<=%s
            UNION SELECT asset_id FROM pams_holdings_snapshot
                WHERE account_code=ANY(%s) AND as_of<=%s AND minor_category='A股基金'
          )""", (list(FUND_ASSET_TYPES), accounts, end, accounts, end))
    assets = {row["asset_id"]: dict(row) for row in cur.fetchall()
              if row["currency"] == "CNY" and re.fullmatch(r"[0-9]{6}", row["primary_symbol"] or "")}
    if not include_trades:
        return assets, [], []
    originals = SourceOriginals(cur)
    trades = [row for account in accounts
              for row in load_trade_rows(cur, account_code=account, start=date.min, end=end,
                                         originals=originals, include_fund_execution=True)
              if row["trade_type"] in {"fund_subscription", "fund_redemption"}
              or (row["asset_id"] in assets and row["trade_type"] in FUND_TRADES)]
    # Include confirmed zero snapshots and reject contradictory quantities, even
    # if the conflicting observation is earlier than the latest account snapshot.
    # A one-day observation cannot attest the ending balance of a replay.
    cur.execute("""SELECT h.account_code, h.asset_id, h.symbol, h.as_of, h.quantity
        FROM pams_holdings_snapshot h WHERE h.account_code=ANY(%s) AND h.minor_category='A股基金'
          AND h.currency='CNY' AND h.asset_liability='资产' AND h.as_of<=%s
          AND NOT EXISTS (SELECT 1 FROM account_snapshot_member m JOIN account_snapshot_version v ON v.version_id=m.version_id
              WHERE m.holding_id=h.id AND v.coverage_status='partial'
                AND v.scope_attestation->'point_in_time_only'='true'::jsonb)
        ORDER BY h.as_of,h.id""", (accounts, end))
    snapshots = [dict(row) for row in cur.fetchall()]
    return assets, trades, snapshots


def load_fund_nav_marks(cur, *, assets, start, end):
    if not assets:
        return {}
    symbols = sorted({asset["primary_symbol"] for asset in assets.values()})
    cur.execute("""SELECT symbol, as_of, unit_nav FROM market_observations
        WHERE observation_type='daily' AND market='基金' AND currency='CNY' AND symbol=ANY(%s)
          AND as_of BETWEEN %s AND %s AND source=ANY(%s)
          AND unit_nav>0 AND unit_nav::text NOT IN ('NaN','Infinity','-Infinity')""",
                (symbols, start, end, list(NAV_SOURCES)))
    prices = defaultdict(set)
    for row in cur.fetchall():
        prices[row["symbol"], row["as_of"]].add(row["unit_nav"])
    return {key: next(iter(values)) if len(values) == 1 else None for key, values in prices.items()}


def _issue(account, symbol, reason, day=None):
    return {"account_code": account, "symbol": symbol, "date": norm(day), "reason": reason}


def _position_history(rows, snapshots, days):
    """Replay verified changes only and reconcile all observed share counts."""
    by_day = defaultdict(list)
    for row in rows:
        day = _trade_day(row)
        by_day[day].append({**row, "trade_date": day})
    for day, events in by_day.items():
        orders = [row for row in events if row["trade_type"] in BUYS | SELLS]
        if _trade_order_ambiguous(orders):
            return None, ("trade_order_ambiguous", day)
    units = Decimal(0)
    positions, flows = {}, {}
    timeline = sorted(set(days) | set(by_day) | set(snapshots))
    for day in timeline:
        flow = Decimal(0)
        for row in by_day.get(day, ()):
            if row["source_evidence_status"] != "verified" or not row.get("fund_execution_confirmed"):
                return None, ("trade_unverified", day)
            kind = row["trade_type"]
            cash = _number(row.get("net_cash"))
            if cash is None:
                return None, ("trade_cash_missing", day)
            if kind in BUYS | SELLS:
                quantity = _number(row.get("quantity"))
                if quantity is None or quantity <= 0:
                    return None, ("trade_quantity_missing", day)
                buy = kind in BUYS
                if (buy and cash >= 0) or (not buy and (cash < 0 or quantity > units)):
                    return None, ("opening_or_trade_quantity_unverified", day)
                units += quantity if buy else -quantity
            elif kind != "dividend" or cash <= 0 or not units:
                return None, ("distribution_or_fee_unverified", day)
            flow -= cash
        if day in snapshots and snapshots[day] != {units}:
            return None, ("snapshot_quantity_mismatch", day)
        positions[day] = units
        if flow:
            flows[day] = flow
    if timeline and positions[timeline[-1]] > 0 and not snapshots:
        return None, ("ending_quantity_unverified", timeline[-1])
    return (positions, flows), None


def reconstruct_fund_series(*, assets, trades, snapshots, marks, start, end, interval, owner_ratio):
    calendar = exchange_calendars.get_calendar("XSHG")
    start = min(start, end)
    opening = calendar.date_to_session((start - timedelta(days=1)).isoformat(), direction="previous").date()
    sessions = [session.date() for session in calendar.sessions_in_range(opening.isoformat(), end.isoformat())]
    groups = defaultdict(list)
    observed = defaultdict(lambda: defaultdict(set))
    issues = []
    for row in trades:
        if _trade_day(row) > end:
            continue
        asset = assets.get(row["asset_id"])
        code = asset["primary_symbol"] if asset else row["symbol"]
        if code is None:
            candidates = {item["primary_symbol"] for item in assets.values()
                          if item["canonical_name"] == row.get("name")}
            code = next(iter(candidates)) if len(candidates) == 1 else None
        if not asset or row["currency"] != "CNY" or (row["symbol"] and row["symbol"] != code):
            issues.append(_issue(row["account_code"], code, "fund_identity_unverified", row["trade_date"]))
            continue
        groups[row["account_code"], row["asset_id"]].append(row)
    for row in snapshots:
        key = (row["account_code"], row["asset_id"])
        if key[1] not in assets or row["symbol"] != assets[key[1]]["primary_symbol"]:
            issues.append(_issue(key[0], row["symbol"], "holding_identity_unverified", row["as_of"]))
            continue
        observed[key][row["as_of"]].add(_number(row["quantity"]))

    included = {}
    for key in sorted(groups.keys() | observed.keys()):
        account, asset_id = key
        code = assets[asset_id]["primary_symbol"]
        history, problem = _position_history(groups[key], observed[key], sessions)
        if problem:
            issues.append(_issue(account, code, *problem))
            continue
        units, _ = history
        held_days = [day for day in sessions if units[day] > 0]
        if held_days and not any(_number(marks.get((code, day))) for day in held_days):
            issues.append(_issue(account, code, "published_nav_history_missing", held_days[0]))
            continue
        included[key] = history

    # A constant set of instruments avoids showing the arrival of evidence as
    # profit. Across an unpriced interval any intervening flow breaks the chain.
    values = {}
    flows = {}
    price_gaps = defaultdict(set)
    accounts = {account for account, _ in included}
    owner_ratios = {account: owner_ratio(account) for account in sorted(accounts)}
    for index, day in enumerate(sessions):
        value = flow = Decimal(0)
        known = bool(included)
        previous_session = sessions[index-1] if index else opening
        for (account, asset_id), (units, movements) in included.items():
            code = assets[asset_id]["primary_symbol"]
            ratio = owner_ratios[account]
            flow += sum(amount for flow_day, amount in movements.items()
                        if previous_session < flow_day <= day) * ratio
            if units[day] == 0:
                continue
            price = _number(marks.get((code, day)))
            if price is None or price <= 0:
                known = False
                price_gaps[account, code].add(day)
            else:
                value += units[day] * price * ratio
        values[day], flows[day] = (value if known else None), flow

    # Start at the first usable anchor. Leave missing marks empty. An interval
    # with a flow and no price cannot supply a full-period return after the gap.
    priced = []
    pending_flow = False
    for index, day in enumerate(sessions):
        if values[day] is None:
            if priced and flows[day] != 0:
                pending_flow = True
            continue
        if pending_flow:
            break
        if priced and sessions[index-1] != priced[-1] and flows[day] != 0:
            break
        if priced or values[day] > 0:
            priced.append(day)
    rates = {row["date"]: row for row in cash_flow_adjusted_return_rows(
        dates=priced, values={day: values[day] for day in priced}, flows=flows,
    )}
    days = [day for day in sessions if day >= start]
    if interval == "weekly":
        weeks = {(day.isocalendar().year, day.isocalendar().week): day for day in days}
        days = list(weeks.values())
    rows = [{"date": day.isoformat(), "market_value": norm(values[day]), "net_flow": norm(flows[day]),
             "display_return_rate": norm(rates.get(day, {}).get("time_weighted_return_rate"))}
            for day in days]
    gaps = [dict(_issue(account, code, "published_nav_dates_missing"),
                 dates=[day.isoformat() for day in sorted(gap)])
            for (account, code), gap in sorted(price_gaps.items())]
    return rows, issues + gaps, priced[0] if priced else None, len(included)


def build_fund_return_history(cur, *, start, end, interval, owner_ratio, excluded_accounts):
    assets, trades, snapshots = load_fund_history_inputs(cur, end=end, excluded_accounts=excluded_accounts)
    marks = load_fund_nav_marks(cur, assets=assets, start=min(start, end) - timedelta(days=20), end=end)
    rows, issues, anchor, included = reconstruct_fund_series(
        assets=assets, trades=trades, snapshots=snapshots, marks=marks,
        start=start, end=end, interval=interval, owner_ratio=owner_ratio,
    )
    usable = [row for row in rows if row["display_return_rate"] is not None]
    # Individual confirmations do not independently prove complete account scope.
    payload = {
        "section": "a_fund", "label": "基金", "start": norm(min(start, end)), "end": norm(end),
        "interval": interval, "base_currency": "CNY", "method": METHOD,
        "status": "unavailable", "reason": "fund_period_valuation_unverified",
        "data_completeness": "partial", "history_issues": issues,
        "coverage_notes": ["曲线按可核对的基金份额和正式净值重建，收益扣除基金申赎。完整期初及交易覆盖仍待核验。"],
        "summary": {"total_return": None, "total_return_rate": None, "annualized_return_rate": None},
        "rows": [{"date": row["date"], "market_value": None, "net_flow": None, "display_return_rate": None}
                 for row in rows],
    }
    if usable:
        estimate = payload["estimated_series"] = {
            "label": "基金历史估值（部分）", "method": METHOD, "rows": rows,
            "source_from": norm(anchor), "source_through": usable[-1]["date"], "instrument_count": included,
        }
        if interval == "daily":
            calendar = exchange_calendars.get_calendar("XSHG")
            observed_dates = [date.fromisoformat(row["date"]) for row in usable]
            estimate["chart_gap_dates"] = return_chart_gap_dates(observed_dates, is_session=calendar.is_session)
            first_day = observed_dates[0]
            if anchor and anchor < start < first_day and calendar.next_session(anchor.isoformat()).date() == first_day:
                # Carry the verified opening return across the exchange holiday.
                estimate["chart_zero_before"] = first_day.isoformat()
    return payload
