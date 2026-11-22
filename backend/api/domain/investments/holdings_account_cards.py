"""Account metrics for the holdings cards, with independent dates and evidence."""

from decimal import Decimal, InvalidOperation


def _decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _metric(value, day=None, *, status="confirmed", basis=None, period_start=None):
    amount = _decimal(value)
    return {
        "value": str(amount) if amount is not None else None,
        "as_of": str(day)[:10] if amount is not None and day else None,
        "status": status if amount is not None else "unavailable",
        "basis": basis if amount is not None else None,
        "period_start": str(period_start)[:10] if amount is not None and period_start else None,
    }


def _cny_balance(accounts, code):
    account = accounts.get(code, {})
    balance = account.get("primary_balance") or account.get("platform_balance") or {}
    if str(balance.get("currency") or account.get("base_currency") or "").upper() != "CNY":
        return {}
    return balance


FUND_CASH_ACCOUNT = "tencent_licaitong_current_plus"


def fund_overview_members(funds):
    codes = {r.get("account") or r.get("account_code") for r in funds.get("rows", [])
             if not r.get("is_closed") and not r.get("reference_as_of")}
    return codes | {FUND_CASH_ACCOUNT}


def _fund_overview_card(funds, cash, closed_returns):
    """Estimate the fund section's value from its active holdings and verified cash."""
    rows = [row for row in funds.get("rows", []) if not row.get("is_closed") and not row.get("reference_as_of")]
    amounts = [_decimal(row.get("market_value_cny")) for row in rows]
    market = sum(amounts, Decimal(0)) if amounts and None not in amounts else None
    cash_amount = _decimal(cash["value"])
    market_day = funds.get("as_of")
    net = market + cash_amount if market is not None and cash_amount is not None else None
    net_day = max((day for day in (str(market_day)[:10] if market_day else None, cash["as_of"]) if day), default=None)
    performance = funds.get("holding_performance") or {}
    return {
        "base_currency": "CNY",
        "net_value": _metric(net, net_day, status="estimated", basis="fund_holdings_and_known_cash"),
        "market_value": _metric(market, market_day, status="partial", basis="holdings"),
        "cash": cash,
        "liabilities": _metric(None),
        "leverage": _metric(None),
        "portfolio_share": _metric(None),
        "recent_daily_pnl": _metric(performance.get("recent_daily_pnl_cny"), performance.get("recent_daily_as_of"),
                                    basis="holding_daily"),
        "cumulative_pnl": _metric(closed_returns.get("known_holding_and_closed_pnl"), market_day,
                                  status="partial", basis="holding_and_closed"),
    }


def account_center_balances(positions, funds, cards):
    """Account-scoped values. The cross-account fund overview is not an account balance."""
    balances = {row["account_code"]: row["account_center_balance"] for row in positions.get("by_account", [])
                if row.get("account_center_balance")}
    fund_rows = {}
    for row in funds.get("rows", []):
        code = row.get("account_code") or row.get("account")
        if code and not row.get("is_closed") and not row.get("reference_as_of"):
            fund_rows.setdefault(code, []).append(row)
    for code, rows in fund_rows.items():
        amounts = [_decimal(row.get("market_value_cny")) for row in rows]
        dates = [row.get("snapshot_as_of") or row.get("as_of") for row in rows]
        if None in amounts or not all(dates):
            continue
        day = max(str(value)[:10] for value in dates)
        existing = balances.get(code) or {}
        if existing.get("balance_amount") is None or str(existing.get("balance_as_of") or "")[:10] < day:
            balances[code] = {"balance_amount": str(sum(amounts)), "balance_currency": "CNY",
                              "balance_as_of": day, "balance_label": "持仓市值"}
    for card in cards.values():
        code = card.get("account_code")
        net = card["net_value"]
        if code and net["value"] is not None and net["basis"] in {"cash_carried_forward", "reconciled_cash", "official_close"}:
            if str((balances.get(code) or {}).get("balance_as_of") or "")[:10] > str(net["as_of"] or ""):
                continue
            balances[code] = {"balance_amount": net["value"],
                              "balance_currency": card["base_currency"], "balance_as_of": net["as_of"],
                              "balance_label": "组合估值" if net["status"] == "estimated" else "组合价值"}
    return balances


def build_holdings_account_cards(*, positions, overview, stock_performance, stock, funds, overseas,
                                 closed_returns):
    accounts = {row["account_code"]: row for row in overview.get("accounts", [])}
    by_account = {row["account_code"]: row for row in positions.get("by_account", [])}
    stock_position = by_account.get("tonghuashun", {})
    fund_position = by_account.get("tencent_licaitong", {})
    stock_day = stock.get("as_of")
    stock_value = stock_position.get("portfolio_value") or {}
    stock_account = stock_performance if (stock_performance or {}).get("status") == "ok" else {}
    stock_holding = stock.get("holding_performance") or {}
    fund_holding = funds.get("holding_performance") or {}
    fund_value = fund_position.get("fund_card_valuation") or fund_position.get("portfolio_value") or {}
    fund_balance = _cny_balance(accounts, "tencent_licaitong")
    fund_cash = _cny_balance(accounts, FUND_CASH_ACCOUNT)
    ibkr_value = by_account.get("ibkr", {}).get("portfolio_value") or {}
    if ibkr_value.get("status") != "official_close":
        ibkr_value = {}
    ibkr_daily = (overseas.get("recent_confirmed_returns") or {}).get("by_account", {}).get("ibkr") or {}
    if ibkr_daily.get("partial") is not False:
        ibkr_daily = {}

    stock_net = _metric(stock_value.get("amount_base"), stock_value.get("as_of"),
                        status="estimated" if stock_value.get("status") == "cash_carried_forward" else "confirmed",
                        basis=stock_value.get("status"))
    if stock_net["value"] is None:
        stock_net = _metric(stock_account.get("net_liquidation_cny"), stock_account.get("as_of"),
                            basis=stock_account.get("net_liquidation_source"))
    stock_rows = [row for row in stock.get("rows", []) if not row.get("is_closed") and not row.get("reference_as_of")
                  and (row.get("account") or row.get("account_code")) == "tonghuashun"]
    stock_market = None
    if stock_rows:
        amounts = [_decimal(row.get("market_value_cny", row.get("market_value")
                           if row.get("currency") == "CNY" and _decimal(row.get("owner_ratio", 1)) == 1 else None))
                   for row in stock_rows]
        if amounts and None not in amounts:
            stock_market = sum(amounts)
    else:
        stock_market = stock_position.get("known_value_base") if positions.get("as_of") == stock_day else None
        if stock_market is None:
            stock_market = stock_account.get("position_market_value_cny")
        if stock_market is None:
            unscoped = [row for row in stock.get("rows", []) if not row.get("is_closed")]
            if unscoped and all(not (row.get("account") or row.get("account_code")) for row in unscoped):
                amounts = [_decimal(row.get("market_value_cny", row.get("market_value") if row.get("currency") == "CNY" else None))
                           for row in unscoped]
                if None not in amounts:
                    stock_market = sum(amounts)
    # Revalue holdings independently of the last reconciled cash date.
    cash_day = stock_value.get("cash_as_of")
    valuation_day = str(stock_day)[:10] if stock_day else None
    carried = cash_day and valuation_day and str(cash_day)[:10] < valuation_day
    stock_cash = _metric(stock_value.get("cash_base"), cash_day,
                         status="carried_forward" if carried else "confirmed", basis="reconciled_cash")
    stock_leverage = _metric(stock_value.get("leverage_ratio"), stock_value.get("as_of"),
                             status=stock_net["status"], basis=stock_net["basis"])
    if (stock_rows and len(stock_rows) == stock_position.get("holdings_count")
            and stock_market is not None and valuation_day and stock_cash["as_of"]
            and stock_value.get("status") in {"cash_carried_forward", "reconciled_cash"}
            and stock_cash["as_of"] <= valuation_day and stock_net["as_of"]
            and stock_net["as_of"] < valuation_day):
        net = stock_market + Decimal(stock_cash["value"])
        stock_net = _metric(net, stock_day, status="estimated", basis="cash_carried_forward")
        gross = sum(abs(amount) for amount in amounts)
        stock_leverage = _metric(gross / net if net > 0 else None, stock_day,
                                 status="estimated", basis="cash_carried_forward")
    stock_daily = _metric(stock_account.get("daily_pnl_cny") if stock_account.get("daily_pnl_effective") is not False else None,
                          stock_account.get("as_of"), basis="account_daily")
    if stock_daily["value"] is None:
        stock_daily = _metric(stock_holding.get("recent_daily_pnl_cny"), stock_holding.get("recent_daily_as_of"),
                              basis="holding_daily")
    stock_cumulative = _metric(stock_account.get("total_pnl_cny"), stock_account.get("as_of"),
                               basis="account_period", period_start=(stock_account.get("total_pnl_evidence") or {}).get("start"))
    if stock_cumulative["value"] is None:
        stock_cumulative = _metric(stock_holding.get("cumulative_pnl_cny"), stock_day, basis="holding_cumulative")

    fund_net = _metric(fund_value.get("amount_base"), fund_value.get("as_of"),
                       status="estimated" if fund_value.get("status") == "estimated_fund_and_cash" else "confirmed",
                       basis=fund_value.get("status"))
    if fund_net["value"] is None:
        fund_net = _metric(fund_balance.get("amount"), fund_balance.get("as_of"), basis=fund_balance.get("source"))
    fund_cash_metric = _metric(fund_value.get("cash_base"), fund_value.get("cash_as_of"), basis="verified_fund_cash")
    if fund_cash_metric["value"] is None:
        fund_cash_metric = _metric(fund_cash.get("amount"), fund_cash.get("as_of"), basis=fund_cash.get("source"))

    cards = {
        "tonghuashun": {
            "net_value": stock_net,
            "market_value": _metric(stock_market, stock_day, basis="holdings"),
            "cash": stock_cash,
            "liabilities": _metric(stock_account.get("other_liabilities_cny"), stock_account.get("as_of")),
            "leverage": stock_leverage,
            "recent_daily_pnl": stock_daily,
            "cumulative_pnl": stock_cumulative,
        },
        "tencent_licaitong": {
            "net_value": fund_net,
            "market_value": _metric(fund_position.get("known_value_base"), fund_position.get("holdings_as_of"),
                                    status="partial" if fund_position.get("snapshot_scope_status") == "incomplete" else "confirmed",
                                    basis="holdings"),
            "cash": fund_cash_metric,
            "liabilities": _metric(None),
            "leverage": _metric(fund_value.get("leverage_ratio"), fund_value.get("as_of"),
                                status=fund_net["status"], basis=fund_net["basis"]),
            "recent_daily_pnl": _metric(fund_holding.get("recent_daily_pnl_cny"), fund_holding.get("recent_daily_as_of"),
                                        basis="holding_daily"),
            "cumulative_pnl": _metric((closed_returns.get("a_fund") or {}).get("by_account", {}).get("tencent_licaitong"),
                                      fund_position.get("holdings_as_of"), status="partial", basis="holding_and_closed"),
        },
        "ibkr": {
            "net_value": _metric(ibkr_value.get("amount_base"), ibkr_value.get("as_of"), basis="official_close"),
            "market_value": _metric(ibkr_value.get("position_value_base"), ibkr_value.get("as_of"), basis="official_close"),
            "cash": _metric(ibkr_value.get("cash_base"), ibkr_value.get("cash_as_of"), basis="official_close"),
            "liabilities": _metric(None),
            "leverage": _metric(ibkr_value.get("leverage_ratio"), ibkr_value.get("as_of"), basis="official_close"),
            "recent_daily_pnl": _metric(ibkr_daily.get("amount_cny"), ibkr_daily.get("as_of"), basis="holding_daily"),
            "cumulative_pnl": _metric(ibkr_value.get("display_period_pnl_base"), ibkr_value.get("as_of"),
                                      basis="official_year_to_date", period_start=ibkr_value.get("period_start")),
        },
    }
    for code, card in cards.items():
        card.update(account_code=code, base_currency="CNY", portfolio_share=_metric(None))
    cards["fund_overview"] = _fund_overview_card(funds, fund_cash_metric, closed_returns.get("a_fund") or {})
    return cards
