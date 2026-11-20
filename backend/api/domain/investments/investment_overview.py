"""Personal investment totals built from the holdings page's existing snapshot."""
from collections import defaultdict
from decimal import Decimal

from domain.accounts.account_policy import excluded_accounts, is_investment_account
from domain.investments.holdings_account_cards import _decimal, _metric


def _dated(amount, day):
    value = _decimal(amount)
    return (str(day)[:10], value) if day and value is not None else None


def _daily_result(rows, card, coverage):
    dated = []
    for row in rows:
        recent = row.get("recent_confirmed_return") or {}
        candidates = []
        if recent.get("status") == "confirmed":
            candidates.append(_dated((recent.get("fields") or {}).get("daily_total_pnl_cny"), recent.get("as_of")))
        if row.get("daily_return_status") == "confirmed" and not row.get("daily_pnl_stale"):
            candidates.append(_dated(row.get("daily_total_pnl_cny"), row.get("daily_return_status_as_of") or row.get("daily_pnl_as_of")))
        latest = max((item for item in candidates if item), key=lambda item: item[0], default=None)
        if latest:
            dated.append(latest)
    day = max((d for d, _ in dated), default="")
    daily = card.get("recent_daily_pnl") or {}
    account = _dated(daily.get("value"), daily.get("as_of"))
    if account and (account[0] > day or (account[0] == day and daily.get("basis") == "account_daily")):
        partial = daily.get("status") == "partial" or (coverage.get("as_of") == account[0] and coverage.get("partial"))
        return (*account, bool(partial))
    if day:
        amounts = [v for d, v in dated if d == day]
        return day, sum(amounts), len(amounts) != len(rows)
    return None


def _total(rows, field, *, absolute=False):
    values = [_decimal(row.get(field)) for row in rows]
    if None in values:
        return None
    return sum((abs(v) if absolute else v for v in values), Decimal(0))


def holdings_overview_card(page, config):
    holdings = page["holdings"]
    accounts = {a["account_code"]: a for a in page["accounts"]["overview"]["accounts"]}
    settings = config.get("accounts") or {}
    excluded = excluded_accounts(config)

    def included(code):
        return (code in accounts or code in settings) and code not in excluded and is_investment_account(
            {**accounts.get(code, {}), **(settings.get(code) or {})})

    cards = {c["account_code"]: c for c in page["account_cards"].values() if c.get("account_code")
             and included(c["account_code"])}
    groups = defaultdict(list)
    for row in holdings["rows"]:
        if included(row["account"]) and not row.get("is_closed") and not row.get("reference_as_of"):
            groups[row["account"]].append(row)

    totals = defaultdict(list)
    profits, daily_results = [], []
    coverage = (holdings.get("recent_confirmed_returns") or {}).get("by_account") or {}
    for code in sorted(set(groups) | set(cards)):
        card = cards.get(code, {})
        rows = groups[code]
        positions = [r for r in rows if r["position_group"] not in {"现金/活期", "负债"}]
        money = [r for r in rows if r["position_group"] == "现金/活期"]
        loans = [r for r in rows if r["position_group"] == "负债"]
        cash_metric = card.get("cash", {})
        net_metric = card.get("net_value", {})
        official = net_metric.get("basis") == "official_close"
        account_type = (settings.get(code) or {}).get("account_type") or accounts.get(code, {}).get("account_type")
        broker = bool(card and account_type == "brokerage")
        # Fund-section cash includes the separately held cash-management subaccount.
        if not rows and not broker:
            continue
        market = _decimal(card["market_value"]["value"]) if official else _total(positions, "market_value_cny")
        exposure = _total(positions, "market_value_cny", absolute=True)
        cash = _decimal(cash_metric.get("value")) if broker else _total(money, "market_value_cny")
        loan = _total(loans, "market_value_cny", absolute=True)
        financing = loan + max(-cash, 0) if cash is not None and loan is not None else None
        net = market + cash - loan if None not in (market, cash, loan) else None
        if official:
            net = _decimal(net_metric.get("value"))
            ratio = _decimal(card.get("leverage", {}).get("value"))
            exposure = net * ratio if net is not None and ratio is not None else None
        totals["net_value"].append(net)
        totals["market_value"].append(market)
        totals["cash"].append(max(cash, 0) if cash is not None else None)
        totals["financing"].append(financing)
        totals["exposure"].append(exposure)
        profits.extend(_decimal(r.get("holding_pnl_cny")) for r in positions)
        if positions:
            daily_results.append(_daily_result(positions, card, coverage.get(code, {})))

    def subtotal(key):
        values = totals[key]
        known = [v for v in values if v is not None]
        return _metric(sum(known) if known else None, status="partial" if None in values else "estimated",
                       basis="investment_accounts")

    result = {key: subtotal(key) for key in ("net_value", "market_value", "cash", "financing")}
    net = _decimal(result["net_value"]["value"])
    comparable = net and net > 0 and None not in totals["net_value"] and None not in totals["exposure"]
    day = max((item[0] for item in daily_results if item), default=None)
    same_day = [item for item in daily_results if item and item[0] == day]
    partial = len(same_day) != len(daily_results) or any(item[2] for item in same_day)
    known_profit = [v for v in profits if v is not None]
    return {
        **result,
        "leverage": _metric(sum(totals["exposure"]) / net if comparable else None,
                            status="estimated", basis="gross_exposure_over_equity"),
        "recent_daily_pnl": _metric(sum(item[1] for item in same_day) if same_day else None, day,
                                    status="partial" if partial else "confirmed", basis="holding_daily"),
        "cumulative_pnl": _metric(sum(known_profit) if known_profit else None,
                                  status="partial" if None in profits else "confirmed",
                                  basis="holding_cumulative_partial" if None in profits else "holding_cumulative"),
    }
