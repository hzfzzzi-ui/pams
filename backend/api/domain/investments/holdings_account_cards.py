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


def build_holdings_account_cards(*, positions, overview, stock_performance, stock, funds, overseas,
                                 closed_returns, summary):
    accounts = {row["account_code"]: row for row in overview.get("accounts", [])}
    by_account = {row["account_code"]: row for row in positions.get("by_account", [])}
    stock_position = by_account.get("tonghuashun", {})
    fund_position = by_account.get("tencent_licaitong", {})
    stock_day = stock.get("as_of")
    stock_value = stock_position.get("portfolio_value") or {}
    if positions.get("as_of") != stock_day or stock_value.get("as_of") != stock_day:
        stock_value = {}
    stock_account = stock_performance if (stock_performance or {}).get("status") == "ok" else {}
    stock_holding = stock.get("holding_performance") or {}
    fund_holding = funds.get("holding_performance") or {}
    fund_value = fund_position.get("fund_card_valuation") or fund_position.get("portfolio_value") or {}
    fund_balance = _cny_balance(accounts, "tencent_licaitong")
    fund_cash = _cny_balance(accounts, "tencent_licaitong_current_plus")
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
    stock_market = stock_position.get("known_value_base") if positions.get("as_of") == stock_day else None
    if stock_market is None:
        stock_market = stock_account.get("position_market_value_cny")
    if stock_market is None:
        stock_rows = [row for row in stock.get("rows", []) if not row.get("is_closed")]
        amounts = [_decimal(row.get("market_value_cny", row.get("market_value") if row.get("currency") == "CNY" else None))
                   for row in stock_rows]
        if amounts and None not in amounts:
            stock_market = sum(amounts)
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
            "cash": _metric(stock_value.get("cash_base"), stock_value.get("cash_as_of"),
                            status="carried_forward" if stock_value.get("status") == "cash_carried_forward" else "confirmed",
                            basis="reconciled_cash"),
            "liabilities": _metric(stock_account.get("other_liabilities_cny"), stock_account.get("as_of")),
            "leverage": _metric(stock_value.get("leverage_ratio"), stock_value.get("as_of"),
                                status=stock_net["status"], basis=stock_net["basis"]),
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
    portfolio_value = _decimal((summary.get("totals") or {}).get("market_value"))
    for code, card in cards.items():
        net = card["net_value"]
        amount = _decimal(net["value"])
        if code == "tencent_licaitong" and net["status"] == "estimated":
            amount = _decimal(fund_balance.get("amount"))
        share = (amount / portfolio_value if amount is not None and portfolio_value and portfolio_value > 0
                 and summary.get("snapshot_scope_status") != "incomplete" else None)
        card.update(account_code=code, base_currency="CNY",
                    portfolio_share=_metric(share, summary.get("as_of"), basis="portfolio_value"))
    return cards
