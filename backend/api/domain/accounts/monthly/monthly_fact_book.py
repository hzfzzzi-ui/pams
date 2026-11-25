"""Monthly personal NAV from an opening inventory and business events. Evidence closes are comparators."""

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.accounts.balances.transfer_settlement import cash_in_transit

from domain.brokers.ibkr.fact_replay import monthly_values as broker_values
from domain.investments.valuation.account_total_evidence import load as load_totals
from domain.investments.valuation.broker_account_history import reconstructed_history_rows
from domain.investments.holdings.fact_position_book import dated_fund_book
from domain.investments.trades.installment_principal import load_unbilled_principal
from domain.investments.trades.trade_read_v2 import load_trade_rows
from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows

ZERO = Decimal(0)


def cash_book(opening, currency, rows, start, dates, *, transaction_opening=False):
    """Post each verified leg once. A trade-date opening already includes outstanding prior-year items."""
    events = defaultdict(Decimal)
    invalid = None
    for row in rows:
        event = row["transaction_date"]
        day = row.get("posting_date") or event
        if day <= start or (transaction_opening and event <= start):
            continue
        if row["currency"] != currency or row["source_evidence_status"] != "verified":
            invalid = min(invalid or day, day)
        else:
            events[day] += row["amount"]
    amounts = {}
    balance = opening
    movements = iter(sorted(events.items()))
    item = next(movements, None)
    for day in dates:
        while item and item[0] <= day:
            if balance is not None:
                balance += item[1]
            item = next(movements, None)
        amounts[day] = None if invalid and day >= invalid else balance
    return amounts


def employee_values(cur, code, start, dates, opening):
    if opening is None:
        return {}
    trades = load_trade_rows(
        cur, account_code=code, start=start + timedelta(days=1), end=dates[-1]
    )
    if any(r["source_evidence_status"] != "verified" for r in trades):
        return {}
    from domain.accounts.balances.recorded_positions import recorded_components

    cur.execute(
        """SELECT f.trade_id, receipt.cashflow_id FROM pams_investment_trade_fact f
        JOIN source_items i ON i.source_item_id=f.source_item_id
        JOIN pams_cashflow_fact receipt ON receipt.cashflow_id::text=i.raw_fields#>>'{bank_receipt,cashflow_id}'
        JOIN source_items ri ON ri.source_item_id=receipt.source_item_id
        JOIN source_records rs ON rs.source_id=ri.source_id
        WHERE f.account_code=%s AND f.trade_type='dividend' AND f.trade_date BETWEEN %s AND %s
          AND receipt.account_code=i.raw_fields#>>'{bank_receipt,account_code}' AND receipt.account_code<>f.account_code
          AND receipt.event_date=f.trade_date AND receipt.amount=f.net_cash AND receipt.currency=f.currency
          AND rs.source_id::text=i.raw_fields#>>'{bank_receipt,source_id}' AND rs.sha256=i.raw_fields#>>'{bank_receipt,source_hash}'
          AND ri.source_item_id::text=i.raw_fields#>>'{bank_receipt,source_key}'""",
        (code, start, dates[-1]),
    )
    links = {row["trade_id"]: row["cashflow_id"] for row in cur.fetchall()}
    receipts = (
        load_cash_rows(
            cur,
            start=start,
            end=dates[-1],
            base_currency="CNY",
            fact_ids=list(links.values()),
        )
        if links
        else []
    )
    verified = {
        row["fact_id"]
        for row in receipts
        if row["source_evidence_status"] == "verified"
    }
    seen = set()
    results = {}
    origin_debt = recorded_components(cur, code, start)[1]
    if origin_debt is None:
        return {}
    debt = sum((abs(d["market_value"]) for d in origin_debt), ZERO)
    amount, pending = opening, ZERO
    events = iter(sorted(trades, key=lambda r: (r["trade_date"], r["trade_id"])))
    trade = next(events, None)
    for day in dates:
        while trade and trade["trade_date"] <= day:
            if trade["trade_type"] == "deposit" and trade["net_cash"] is not None:
                pending += trade["net_cash"]
            elif trade["trade_type"] == "buy" and trade["gross_amount"] is not None:
                _, debts = recorded_components(cur, code, trade["trade_date"])
                if debts is None:
                    return results
                next_debt = sum((abs(d["market_value"]) for d in debts), ZERO)
                if abs(trade["gross_amount"] - pending - (next_debt - debt)) > Decimal(
                    ".005"
                ):
                    return results
                amount += pending
                debt, pending = next_debt, ZERO
            elif trade["trade_type"] == "dividend":
                receipt = links.get(trade["trade_id"])
                if receipt not in verified or receipt in seen:
                    return results
                seen.add(receipt)
            else:
                return results
            trade = next(events, None)
        results[day] = (amount + pending, amount + pending + debt, debt)
    return results


def _independent(point, day):
    if (
        point.get("coverage_break")
        or point.get("quality") != "confirmed"
        or point.get("observed_on") != day
        or point.get("reference_basis") == "contributed_principal"
    ):
        return None
    return point.get("signed_balance")


def transaction_opening_accounts(cur, origin):
    """Credit accounts whose evidenced opening already includes pre-opening transactions."""
    cur.execute(
        """SELECT DISTINCT h.account_code FROM pams_holdings_snapshot h JOIN source_items i USING(source_item_id)
                WHERE h.as_of=%s AND i.parsed_fields#>>'{reconstruction_evidence,method}'='credit_statement_bridge'""",
        (origin,),
    )
    return {row["account_code"] for row in cur.fetchall()}


def build_months(cur, values, included, config, start, end, *, rate_at, owner_ratio):
    months = [
        date(y, m, monthrange(y, m)[1])
        for y in range(start.year, end.year + 1)
        for m in range(1, 13)
    ]
    months = [d for d in months if start <= d <= end]
    if not months:
        return {}
    results = {d: {} for d in months}
    first = date(months[0].year - 1, 12, 31)
    cur.execute(
        """SELECT cashflow_id FROM pams_cashflow_fact WHERE account_code=ANY(%s)
                AND coalesce(posting_date,event_date)>%s AND coalesce(posting_date,event_date)<=%s""",
        (list(included), first, months[-1]),
    )
    ids = [r["cashflow_id"] for r in cur.fetchall()]
    cash = defaultdict(list)
    for r in (
        load_cash_rows(
            cur, start=date.min, end=date.max, base_currency="CNY", fact_ids=ids
        )
        if ids
        else ()
    ):
        cash[r["account_code"]].append(r)
    cards = [
        c
        for c in included
        if config.get("accounts", {}).get(c, {}).get("account_type")
        in ("credit_card", "liability")
    ]
    for year in sorted({d.year for d in months}):
        days = [d for d in months if d.year == year]
        origin = date(year - 1, 12, 31)
        totals = load_totals(cur, dates=[origin, *days], accounts=set(included))
        principal = (
            load_unbilled_principal(cur, cards, [origin, *days]) if cards else {}
        )
        transaction_openings = transaction_opening_accounts(cur, origin)
        for code, account in included.items():
            cfg = {**account, **config.get("accounts", {}).get(code, {})}
            history = values[code]["rows"]
            opening = history.get(origin, {}).get("signed_balance")
            total = totals.get((origin, code))
            unit = values[code]["currency"]
            if (
                total
                and total["currency"] == unit
                and set(total["covered_account_codes"]) == {code}
            ):
                opening = Decimal(total["net_asset"])
            mode = cfg.get("portfolio_value_evidence")
            dates_used = {}
            gross = {}
            if mode == "reconciled_trade_cash":
                points = reconstructed_history_rows(
                    cur,
                    account_code=code,
                    start_date=origin,
                    end_date=days[-1],
                    currency=unit,
                )
                marked = {r["date"]: r["net_value"] for r in points}
                from exchange_calendars import get_calendar

                cal = get_calendar("XSHG")
                balances = {
                    d: marked.get(
                        d,
                        marked.get(
                            cal.date_to_session(
                                d.isoformat(), direction="previous"
                            ).date()
                        ),
                    )
                    for d in days
                }
                dates_used = {
                    d: {
                        "market_close": cal.date_to_session(
                            d.isoformat(), direction="previous"
                        ).date()
                    }
                    for d in days
                }
                method = "trade_cash_and_shares"
            elif mode == "official_broker_close":
                gross = broker_values(cur, code, origin, opening, days)
                balances = {d: v[0] for d, v in gross.items()}
                dates_used = {d: {"market_close": v[3]} for d, v in gross.items()}
                method = "broker_cash_legs_and_executions"
            elif cfg.get("latest_position_evidence") == "recorded_equity_and_debt":
                gross = employee_values(cur, code, origin, days, opening)
                balances = {d: v[0] for d, v in gross.items()}
                method = "subscription_and_financing"
            elif cfg.get("reconcile_method", "balance") == "position_nav":
                marks = dated_fund_book(cur, code, origin, days)
                balances = {d: p[0] for d, p in marks.items()}
                dates_used = {d: p[1] for d, p in marks.items()}
                method = "trade_units_at_market"
            else:
                balances = cash_book(
                    opening,
                    unit,
                    cash[code],
                    origin,
                    days,
                    transaction_opening=code in transaction_openings,
                )
                method = "opening_and_cash_movements"
                if code in cards:

                    def debt(d):
                        return sum(
                            v[0]
                            for k, v in principal.get((d, code), {}).items()
                            if k[1] == unit
                        )

                    balances = {
                        d: v + debt(origin) - debt(d) if v is not None else None
                        for d, v in balances.items()
                    }
            for d in days:
                point = history.get(d, {})
                n = balances.get(d)
                ratio = point.get("owner_ratio")
                if ratio is None and not cfg.get("funding_history"):
                    ratio = owner_ratio(code)
                rate = rate_at(unit, d)
                personal = (
                    n * ratio * rate
                    if None not in (n, ratio, rate)
                    else ZERO
                    if n == 0 and ratio is not None
                    else None
                )
                observed = _independent(point, d)
                observed_day = d if observed is not None else None
                if (
                    method == "broker_cash_legs_and_executions"
                    and observed is None
                    and d in dates_used
                ):
                    session = dates_used[d]["market_close"]
                    close = history.get(session, {})
                    if close.get("daily_close", {}).get("status") == "ok":
                        observed = close.get("signed_balance")
                        observed_day = session
                proof = totals.get((d, code))
                if (
                    proof
                    and proof["currency"] == unit
                    and set(proof["covered_account_codes"]) == {code}
                ):
                    observed = Decimal(proof["net_asset"])
                    observed_day = d
                ref = point.get("signed_balance")
                refdate = point.get("observed_on")
                if ref is None:
                    ref = point.get("known_component_value")
                principal_ref = any(
                    p.get("reference_basis") == "contributed_principal"
                    for p in (history.get(origin, {}), point)
                )
                negative_cash = (
                    n is not None
                    and n < 0
                    and method == "opening_and_cash_movements"
                    and code not in cards
                )
                actual_gross = gross.get(d)
                if actual_gross is None and n is not None and not negative_cash:
                    actual_gross = (n, max(n, ZERO), max(-n, ZERO))
                difference = (
                    n - observed if observed is not None and n is not None else None
                )
                status = (
                    "unpriced"
                    if personal is None
                    else "negative_cash"
                    if negative_cash
                    else "principal_only"
                    if principal_ref
                    else "unmatched"
                    if difference is not None and abs(difference) >= Decimal(".005")
                    else "matched"
                    if difference is not None
                    else "book_only"
                )
                results[d][code] = {
                    "currency": unit,
                    "native": n,
                    "personal": personal,
                    "method": method,
                    "observed_date": observed_day,
                    "observed": observed,
                    "difference": difference,
                    "status": status,
                    "reference": ref,
                    "reference_date": refdate,
                    "reference_basis": point.get("basis"),
                    "native_debt": actual_gross[2] if actual_gross else None,
                    "fx_date": None,
                    "owner_ratio": ratio,
                    "rate": rate,
                    "personal_assets": actual_gross[1] * ratio * rate
                    if actual_gross and None not in (rate, ratio)
                    else None,
                    "personal_debt": actual_gross[2] * ratio * rate
                    if actual_gross and None not in (rate, ratio)
                    else None,
                    "opening_date": origin,
                    "price_dates": dates_used.get(d, {}),
                }
    return results


def apply_month(row, values, entries, proofs, transit=()):
    """Publish the same account contributions in the monthly series, account details and portfolio."""
    day = row["date"]
    extra = {
        code: value
        for code, value in row["_account_contributions"].items()
        if code not in entries
    }
    pending_transit = any(v["personal"] is None for v in transit)
    if transit and not pending_transit:
        extra["internal_transfers_in_transit"] = sum(v["personal"] for v in transit)
        row["_comparison_basis"]["internal_transfers_in_transit"] = "confirmed_transfer"
    contributions = dict(extra)
    missing = ["internal_transfers_in_transit"] if pending_transit else []
    for code, entry in entries.items():
        point = values[code]["rows"].setdefault(day, {"date": day})
        point["monthly_book"] = entry
        amount = entry["personal"]
        if amount is None:
            missing.append(code)
        else:
            contributions[code] = amount
        point.update(
            signed_balance=entry["native"],
            personal_contribution=amount,
            contribution_scope="account",
            observed_on=day,
            quality="estimated" if amount is not None else "unavailable",
            basis=entry["method"],
        )
        if "net_value" in values[code]["series"]:
            point.update(
                net_value=entry["native"],
                net_value_as_of=day,
                net_value_status=point["quality"],
            )
        if "liabilities" in values[code]["series"] and entry["native_debt"] is not None:
            point.update(
                liabilities=entry["native_debt"],
                liabilities_as_of=day,
                liabilities_status=point["quality"],
            )
    known = sum(contributions.values(), ZERO) if contributions else None
    complete = not missing and known is not None
    assets = debts = None
    if complete and all(
        e["personal_assets"] is not None and e["personal_debt"] is not None
        for e in entries.values()
    ):
        assets = sum(e["personal_assets"] for e in entries.values()) + sum(
            max(v, ZERO) for v in extra.values()
        )
        debts = sum(e["personal_debt"] for e in entries.values()) + sum(
            max(-v, ZERO) for v in extra.values()
        )
    families = []
    for proof in proofs:
        members = proof["covered_account_codes"]
        if len(members) < 2 or not all(
            c in entries
            and entries[c]["currency"] == proof["currency"]
            and entries[c]["native"] is not None
            for c in members
        ):
            continue
        value = sum(entries[c]["native"] for c in members)
        observed = Decimal(proof["net_asset"])
        families.append(
            {
                "account": proof["account_code"],
                "members": members,
                "currency": proof["currency"],
                "book": value,
                "observed_date": day,
                "observed": observed,
                "difference": value - observed,
            }
        )
    book = {
        "accounts": entries,
        "other_contributions": extra,
        "known_nav": known,
        "missing": sorted(missing),
        "complete": complete,
        "family_comparisons": families,
        "in_transit": list(transit),
    }
    row.update(
        monthly_book=book,
        net_worth=known if complete else None,
        available_net_worth=known,
        assets=assets,
        liabilities=debts,
        _account_contributions=contributions,
        unavailable_accounts=sorted(missing),
        available_accounts=sorted(contributions),
        estimated_accounts=sorted(c for c in entries if c not in missing),
        valuation_quality="estimated",
    )
    for code, e in entries.items():
        row["_comparison_basis"][code] = e["method"]
        row["latest_known_components"][code] = {
            "basis": e["method"],
            "from": str(e["opening_date"]),
            "through": str(day),
            "native_balance": str(e["native"]) if e["native"] is not None else None,
            "currency": e["currency"],
            "owner_ratio": str(e["owner_ratio"]),
            "fx_rate": str(e["rate"]),
            "fx_as_of": e["fx_date"],
            "price_dates": {k: str(v) for k, v in e["price_dates"].items()},
        }
