"""Personal investment totals built from the holdings page's existing snapshot."""
from collections import defaultdict
from decimal import Decimal

from domain.accounts.account_policy import excluded_accounts, is_investment_account
from domain.investments.holdings_account_cards import _decimal, _metric, fund_overview_members
from domain.investments.personal_holdings import holding_filter_weights


def _number_text(value):
    return str(value) if value is not None else None


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
    account_nets = {}
    position_groups = {}
    financing_items = []
    account_returns = []
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
        # Keep the list and its subtotal on the same personal-account cash/position evidence.
        components = []
        for row in loans:
            amount = _decimal(row.get("market_value_cny"))
            components.append(("loan", row.get("name") or row.get("symbol"),
                               abs(amount) if amount is not None else None,
                               row.get("as_of") or row.get("snapshot_as_of")))
        if cash is None or cash < 0:
            cash_day = cash_metric.get("as_of") if broker else max(
                (str(r["as_of"]) for r in money if r.get("as_of")), default=None)
            components.append(("negative_cash", None, -cash if cash is not None else None, cash_day))
        for kind, name, amount, day in components:
            if amount != 0:
                financing_items.append({
                    "account_code": code, "account_name": accounts.get(code, {}).get("display_name") or code,
                    "kind": kind, "name": name, "amount_cny": str(amount) if amount is not None else None,
                    "as_of": str(day)[:10] if day else None,
                })
        totals["financing"].extend([amount for _, _, amount, _ in components] or [Decimal(0)])
        net = market + cash - loan if None not in (market, cash, loan) else None
        if official:
            net = _decimal(net_metric.get("value"))
            ratio = _decimal(card.get("leverage", {}).get("value"))
            exposure = net * ratio if net is not None and ratio is not None else None
        cfg = settings.get(code) or {}
        ratios = {_decimal(r.get("owner_ratio")) for r in positions}
        owner = next(iter(ratios)) if len(ratios) == 1 else None
        shared = owner is not None and 0 < owner < 1
        dedicated = cfg.get("latest_position_evidence") == "recorded_equity_and_debt"
        funding = cfg.get("funding_history") or {}
        evidence_day = max((str(r.get("snapshot_as_of") or r.get("as_of"))[:10] for r in positions
                            if r.get("snapshot_as_of") or r.get("as_of")), default=None)
        cash_only = bool(money) and not positions and not loans
        account_returns.append({
            "account_code": code, "name": accounts.get(code, {}).get("display_name") or cfg.get("display_name") or code,
            "shared": shared, "dedicated_loan": dedicated, "official_close": official, "cash_only": cash_only,
            "owner_ratio": _number_text(owner), "market_value_cny": _number_text(market),
            "remaining_cost_cny": _number_text(_total(positions, "cost_cny")),
            "personal_capital": cfg.get("personal_capital") if shared and funding.get("complete_personal_flows") else None,
            "account_capital": cfg.get("capital_base") if shared and funding.get("complete_personal_flows") else None,
            "net_value": net_metric if official or broker else None,
            "cash": cash_metric if broker else _metric(cash) if cash_only else None,
            "leverage": card.get("leverage") if official else None,
            "position_equity_cny": positions[0].get("position_net_value_cny") if dedicated and len(positions) == 1 else None,
            "dedicated_debt_cny": _number_text(loan) if dedicated and loans else None,
            "dividends_cny": _number_text(_total(positions, "annual_dividend_income_cny")) if dedicated else None,
            "as_of": net_metric.get("as_of") or evidence_day,
            "reported_pnl": card.get("cumulative_pnl") if official else None,
        })
        account_nets[code] = net
        position_groups[code] = {r["position_group"] for r in positions}
        totals["net_value"].append(net)
        totals["market_value"].append(market)
        totals["cash"].append(max(cash, 0) if cash is not None else None)
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
    financing = result["financing"]
    denominator = _decimal(financing["value"])
    for item in financing_items:
        amount = _decimal(item["amount_cny"])
        item["share"] = (str(amount / denominator)
                         if amount is not None and denominator and financing["status"] != "partial" else None)
    net = _decimal(result["net_value"]["value"])
    comparable = net and net > 0 and None not in totals["net_value"] and None not in totals["exposure"]

    def net_share(codes, display_metric=None):
        values = [account_nets.get(code) for code in codes]
        amount = sum(values) if values and None not in values else None
        if display_metric is not None:
            display = _decimal(display_metric.get("value"))
            if amount is None or display is None or abs(amount - display) >= Decimal("0.01"):
                amount = None
        value = (amount / net if amount is not None and net and net > 0
                 and result["net_value"]["status"] != "partial" else None)
        return _metric(value, holdings.get("as_of"), status="estimated", basis="investment_net_value")

    account_shares = {}
    for key, card in page["account_cards"].items():
        if code := card.get("account_code"):
            members = {code}
        elif key == "fund_overview":
            # The existing fund card includes open OTC funds and its cash-management subaccount.
            members = fund_overview_members(page.get("funds", {}))
            if any(position_groups.get(code, set()) - {"基金"} for code in members):
                members = set()
        else:
            continue
        account_shares[key] = net_share(members, card.get("net_value") or {})
    group_shares = {}
    for group in {g for groups_in_account in position_groups.values() for g in groups_in_account}:
        members = [code for code, groups_in_account in position_groups.items() if group in groups_in_account]
        if all(position_groups[code] == {group} for code in members):
            group_shares[group] = net_share(members)
    day = max((item[0] for item in daily_results if item), default=None)
    same_day = [item for item in daily_results if item and item[0] == day]
    partial = len(same_day) != len(daily_results) or any(item[2] for item in same_day)
    known_profit = [v for v in profits if v is not None]
    personal = holdings.get("personal_summary") or {}
    has_positions = (_decimal(personal.get("market_value_cny")) or 0) > 0
    return {
        **result,
        "complete_position_weights": personal.get("missing_values") == 0 and has_positions,
        "filter_weights": holding_filter_weights(holdings),
        "group_net_shares": group_shares,
        "account_card_net_shares": account_shares,
        "financing_items": financing_items,
        "account_returns": account_returns,
        "leverage": _metric(sum(totals["exposure"]) / net if comparable else None,
                            status="estimated", basis="gross_exposure_over_equity"),
        "recent_daily_pnl": _metric(sum(item[1] for item in same_day) if same_day else None, day,
                                    status="partial" if partial else "confirmed", basis="holding_daily"),
        "cumulative_pnl": _metric(sum(known_profit) if known_profit else None,
                                  status="partial" if None in profits else "confirmed",
                                  basis="holding_cumulative_partial" if None in profits else "holding_cumulative"),
    }
