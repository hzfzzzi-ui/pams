"""Forward value original Flex executions and currency cash legs, independently of statement NAV."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from xml.etree import ElementTree

from domain.brokers.ibkr import ibkr_daily_close_read
from domain.brokers.ibkr.ibkr_daily_holdings import ACCOUNT


def monthly_values(cur, code, opening_day, opening, dates):
    if code != ACCOUNT or opening is None:
        return {}
    archive = ibkr_daily_close_read.read_published_portfolio_series(cur)
    if not archive or archive.get("status") != "ok":
        return {}
    digest = archive["source_sha256"]
    cur.execute("SELECT content FROM ibkr_source_originals WHERE sha256=%s", (digest,))
    row = cur.fetchone()
    if not row or sha256(row["content"]).hexdigest() != digest:
        return {}
    return replay_xml(
        bytes(row["content"]), archive, opening_day, dates, opening=opening
    )


def replay_xml(content, archive, opening_day, dates, *, opening=Decimal(0)):
    statements = sorted(
        ElementTree.fromstring(content).iter("FlexStatement"),
        key=lambda s: s.get("toDate"),
    )
    cash, units = defaultdict(Decimal), defaultdict(Decimal)
    interest = Decimal(0)
    dividends = Decimal(0)
    seen_trades = set()
    result = {}
    from domain.brokers.ibkr.ibkr_report_state import _calendar

    calendar = _calendar()
    if not statements:
        return {}
    first = datetime.strptime(statements[0].attrib["fromDate"], "%Y%m%d").date()
    last = first - timedelta(days=1)
    initial = next(
        (
            p
            for p in statements[0].iter("EquitySummaryByReportDateInBase")
            if p.get("reportDate") == last.strftime("%Y%m%d")
        ),
        None,
    )
    if initial is None or any(
        Decimal(initial.attrib[k]) for k in ("totalLong", "totalShort")
    ):
        return {}
    if last > opening_day:
        return {}
    result[last] = (Decimal(0), Decimal(0), Decimal(0))
    requested = sorted(dates)
    for statement in statements:
        day = datetime.strptime(statement.attrib["toDate"], "%Y%m%d").date()
        if day > requested[-1]:
            break
        close = archive["daily_closes"].get(day.isoformat(), {})
        if (
            statement.get("fromDate") != statement.get("toDate")
            or day <= last
            or statement.get("accountId") != archive["account_id"]
            or close.get("identity_evidence", {}).get("status") != "ok"
        ):
            break
        if day > last + timedelta(days=1) and len(
            calendar.sessions_in_range(
                (last + timedelta(days=1)).isoformat(),
                (day - timedelta(days=1)).isoformat(),
            )
        ):
            break
        for item in statement.iter("StatementOfFundsLine"):
            if item.get("levelOfDetail") == "Currency" and item.get("activityCode"):
                cash[item.attrib["currency"]] += Decimal(item.attrib["amount"])
        for item in statement.iter("Trade"):
            if (
                item.get("levelOfDetail") != "EXECUTION"
                or item.get("assetCategory") == "CASH"
            ):
                continue
            ident = item.attrib["tradeID"]
            if ident in seen_trades or item.get("assetCategory") != "STK":
                return {}
            seen_trades.add(ident)
            units[item.attrib["conid"]] += Decimal(item.attrib["quantity"])
        changes = statement.find(".//ChangeInNAV")
        if changes is None:
            break
        # Flex translates the two outstanding accrual balances separately from its
        # marked securities and currency cash. The interest subreport identifies
        # its portion of the reported translation movement.
        interest_fx = sum(
            Decimal(e.get("fxTranslation") or "0")
            for e in statement.iter("InterestAccrualsCurrency")
            if e.get("currency") == "BASE_SUMMARY"
        )
        interest += (
            Decimal(changes.get("changeInInterestAccruals") or "0") + interest_fx
        )
        dividends += (
            Decimal(changes.get("changeInDividendAccruals") or "0")
            + Decimal(changes.get("fxTranslation") or "0")
            - interest_fx
        )
        base = close["base_currency"]
        fx = {
            e.get("fromCurrency"): Decimal(e.attrib["rate"])
            for e in statement.iter("ConversionRate")
            if e.get("toCurrency") == base
            and e.get("reportDate") == statement.get("toDate")
        }
        fx[base] = Decimal(1)
        prices = {
            p.attrib["conid"]: (
                Decimal(p.attrib["markPrice"]),
                Decimal(p.attrib["fxRateToBase"]),
            )
            for p in statement.iter("OpenPosition")
            if p.get("levelOfDetail") == "SUMMARY"
        }
        active = {k: v for k, v in units.items() if v}
        amount = None
        if (
            all(k in fx for k, v in cash.items() if v)
            and active.keys() <= prices.keys()
        ):
            legs = [v * fx[k] for k, v in cash.items() if v]
            legs.extend(
                (q * prices[k][0]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                * prices[k][1]
                for k, q in active.items()
            )
            legs.append(interest)
            legs.append(dividends)
            assets = sum((v for v in legs if v > 0), Decimal(0))
            debt = -sum((v for v in legs if v < 0), Decimal(0))
            amount = (assets - debt, assets, debt)
        result[day] = amount
        last = day

    def session(d):
        return (
            d
            if d in result
            else calendar.date_to_session(d.isoformat(), direction="previous").date()
        )

    anchor = result.get(session(opening_day))
    if anchor is None or abs(anchor[0] - opening) > Decimal(".005"):
        return {}
    return {
        d: (*result[session(d)], session(d))
        for d in dates
        if result.get(session(d)) is not None
    }
