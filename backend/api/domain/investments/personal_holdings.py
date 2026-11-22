"""Personal open-position allocation from owner-adjusted holding valuations."""
from decimal import Decimal
from functools import lru_cache

from domain.assets.lookthrough_state import load_lookthrough_config
from domain.investments.esop_dividends import dividend_yield_on_cost


@lru_cache(maxsize=1)
def instrument_classes():
    return {str(item["symbol"]).upper(): item["asset_class"]
            for item in load_lookthrough_config()["asset_mapping"] if item.get("symbol")}


def position_category(row, classes):
    major = row.get("major_category") or row.get("category")
    minor = row.get("minor_category") or major
    instrument = classes.get(str(row.get("symbol") or "").upper(), "")
    if major == "负债" or row.get("asset_liability") in {"负债", "liability", "debt"}:
        return "负债", None
    if major in {"现金", "现金/活期"}:
        return "现金/活期", None
    if instrument.startswith("etf_") or minor in {"ETF", "场内基金"}:
        return "基金", "场内"
    if minor in {"A股基金", "场外基金"} or instrument in {"active_fund_cn", "fund_cn", "gold_fund"}:
        return "基金", "场外"
    if minor and "基金" in minor:
        return "基金", None
    if major in {"A股", "港股", "美股", "韩股", "瑞典", "日股", "英股", "股票"}:
        return "股票", minor if minor != "股票" else None
    if major in {"ESOP", "内部股份", "员工持股"}:
        return "员工持股", None
    if major in {"理财", "银行理财", "理财产品"}:
        return "银行理财", None
    return major or "其他", None


def attach_position_net_values(rows, accounts):
    financed = {}
    for row in rows:
        row["position_net_value_cny"] = row.get("market_value_cny")
        code = row.get("account") or row.get("account_code")
        if (accounts.get(code, {}).get("latest_position_evidence") == "recorded_equity_and_debt"
                and not row.get("is_closed")):
            financed.setdefault(code, []).append(row)
    for members in financed.values():
        assets = [r for r in members if r["position_group"] not in {"负债", "现金/活期"}]
        debts = [r for r in members if r["position_group"] == "负债"]
        for row in assets:
            row["position_net_value_cny"] = None
        # Only a single recorded equity can own the account's recorded loan.
        if (len(assets) == 1 and assets[0]["position_group"] == "员工持股" and debts
                and all(r.get("market_value_cny") is not None and r.get("snapshot_scope_member") in
                        {"recorded", "verified"} for r in [*assets, *debts])):
            debt = sum(abs(Decimal(str(r["market_value_cny"]))) for r in debts)
            assets[0]["position_net_value_cny"] = float(Decimal(str(assets[0]["market_value_cny"])) - debt)


def position_dividend_return(members):
    if (members[0]["position_group"] != "员工持股" or members[0].get("is_closed")
            or members[0].get("reference_as_of")):
        return None
    annual = cost = None
    if all(r.get("realized_income_status") == "confirmed" and r.get("annual_dividend_income_cny") is not None
           for r in members):
        annual = sum(Decimal(str(r["annual_dividend_income_cny"])) for r in members)
    if all(r.get("cost_cny") is not None for r in members):
        cost = sum(Decimal(str(r["cost_cny"])) for r in members)
    rate = dividend_yield_on_cost(annual, cost)
    return {"amount_cny": str(annual) if annual is not None else None,
            "yield_on_cost": str(rate) if rate is not None else None}


def attach_combined_positions(rows):
    groups = {}
    for row in rows:
        identity = row.get("asset_id") or row.get("symbol")
        if identity and not row.get("is_closed") and row["position_group"] not in {"现金/活期", "负债"}:
            key = (row["position_group"], row["position_segment"], row.get("currency"), identity)
            groups.setdefault(key, []).append(row)
    for group_index, members in enumerate(groups.values()):
        if len(members) < 2:
            continue
        combined = {"position_dividend_return": position_dividend_return(members)}
        for field in ("quantity", "portfolio_weight", "cost", "cost_cny", "market_value", "market_value_cny",
                      "position_net_value_cny", "holding_pnl", "holding_pnl_cny", "realized_trade_pnl_cny", "net_distributions_cny",
                      "daily_total_pnl_cny", "daily_pnl_cny", "daily_fx_pnl_cny", "daily_pnl_native"):
            values = [row.get(field) for row in members]
            combined[field] = float(sum(Decimal(str(v)) for v in values)) if None not in values else None
        for field in ("buy_price", "current_price", "previous_price", "price_change_pct"):
            values = {row.get(field) for row in members}
            combined[field] = values.pop() if len(values) == 1 else None
        dates = {row.get("daily_pnl_as_of") for row in members}
        pending = (len(dates) != 1 or None in dates or combined["daily_total_pnl_cny"] is None
                   or any(row.get("daily_pnl_stale") or row.get("daily_return_status") == "pending" for row in members))
        combined["daily_pnl_stale"] = pending
        if pending:
            combined["daily_return_status"] = "pending"
        for row in members:
            row["combined_key"] = str(group_index)
        members[0]["combined_position"] = combined
        members[0]["combined_count"] = len(members)


def personal_holdings(payload, classes=None, *, accounts=None):
    classes = instrument_classes() if classes is None else classes
    total = Decimal("0")
    missing = valued = 0
    rows = []
    for source in payload.get("rows", []):
        group, segment = position_category(source, classes)
        row = dict(source, position_group=group, position_segment=segment, portfolio_weight=None)
        row["position_dividend_return"] = position_dividend_return([row])
        rows.append(row)
        if group in {"现金/活期", "负债"} or row.get("reference_as_of") or row.get("is_closed"):
            continue
        amount = row.get("market_value_cny")
        if amount is None:
            missing += 1
        else:
            valued += 1
            total += Decimal(str(amount))
            row["portfolio_weight"] = Decimal(str(amount))
    for row in rows:
        amount = row["portfolio_weight"]
        row["portfolio_weight"] = float(amount / total) if amount is not None and total > 0 else None
    attach_position_net_values(rows, accounts or {})
    attach_combined_positions(rows)
    for item in [*rows, *(r["combined_position"] for r in rows if r.get("combined_position"))]:
        cost, pnl = item.get("cost_cny"), item.get("holding_pnl_cny")
        market = item.get("market_value_cny")
        floating = None
        if market is not None and cost is not None and not item.get("is_closed") and not item.get("reference_as_of"):
            floating = Decimal(str(market)) - Decimal(str(cost))
        item["floating_pnl_cny"] = float(floating) if floating is not None else None
        item["floating_pnl_ratio"] = (float(floating / Decimal(str(cost)))
                                      if floating is not None and Decimal(str(cost)) > 0 else None)
        item["cumulative_pnl_ratio"] = (float(Decimal(str(pnl)) / Decimal(str(cost)))
                                        if pnl is not None and cost is not None and Decimal(str(cost)) > 0 else None)
    references = []
    for reference in payload.get("historical_references", []):
        group, segment = position_category(reference, classes)
        references.append(dict(reference, position_group=group, position_segment=segment))
    return dict(payload, rows=rows, historical_references=references,
                personal_summary={"market_value_cny": float(total) if valued or not missing else None,
                                  "missing_values": missing})
