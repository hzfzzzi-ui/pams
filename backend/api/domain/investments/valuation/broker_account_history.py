"""Account net value and financing debt from the same verified broker close."""

from datetime import date, timedelta
from decimal import Decimal

from domain.brokers.ibkr import ibkr_daily_close_read as daily_close
from domain.brokers.ibkr.ibkr_daily_holdings import ACCOUNT
from domain.investments.trades.nav_trade_source_v2 import project
from domain.investments.valuation.valuation_nav import ValuationEvidenceSeries, daily_close_account_identity, daily_close_nav_bucket, series_dates


def broker_history_rows(cur, *, account_code, start_date, end_date, currency):
    # The Flex reader is bound to one published account, including in legacy mode.
    if account_code != ACCOUNT:
        return []
    evidence = daily_close.read_published_portfolio_series(cur)
    published = evidence is not None
    if not published:
        if not daily_close.configured():
            return []
        evidence = daily_close.read_series()

    rows = {}
    low = start_date or date.min
    high = end_date
    while evidence and evidence.get("daily_closes"):
        closes = evidence["daily_closes"]
        for key, close in closes.items():
            day = date.fromisoformat(key)
            if not low <= day <= high or close.get("status") != "ok":
                continue
            identity = (close["identity_evidence"] if published else
                        daily_close_account_identity(cur, source_account_id=close["account_id"], as_of=day))
            if identity.get("status") != "ok":
                continue
            rate = daily_close.historical_rate(cur, currency=close["base_currency"], base_currency=currency, as_of=day)
            if rate is not None:
                # Official NAV already includes negative cash. The debt series
                # reports that borrowing as a positive amount alongside NAV.
                rows[day] = {"date": day, "net_value": close["nav_base"] * rate,
                             "liabilities": max(-close["cash_base"], Decimal(0)) * rate}

                gross = daily_close_nav_bucket(close, rate=rate, owner_ratio=Decimal(1))
                if gross is not None:
                    rows[day].update(gross_assets=gross['assets'], gross_debt=gross['liabilities'])
                rows[day]['daily_close'] = {'status': 'ok', 'as_of': key, 'source_sha256': close.get('source_sha256'),
                                           'nav_basis': 'official_base_nav_at_internal_historical_fx',
                                           'identity_evidence': identity}

        first = date.fromisoformat(min(closes))
        if not published or first <= low:
            break
        # New archives can cover only the current year and its opening date.
        # Consult older archives for earlier points, keeping the newer overlap.
        high = min(high, first - timedelta(days=1))
        publication = date.fromisoformat(evidence["as_of"])
        through = min(first, publication - timedelta(days=1))
        evidence = daily_close.read_published_portfolio_series(cur, through=through)
    return [rows[day] for day in sorted(rows)]


def reconstructed_history_rows(cur, *, account_code, start_date, end_date, currency):
    # Reconciled CNY trade cash and daily unadjusted security prices value this
    # whole account. Personal funding ratios belong to portfolio-level NAV.
    if currency != "CNY":
        return []
    history = project(cur, dates=series_dates(start_date, end_date, "daily"), owner_ratio=Decimal(1),
                      series_type=ValuationEvidenceSeries, account_code=account_code, require_new_mark_after_ledger=True)
    from exchange_calendars import get_calendar
    calendar = get_calendar("XSHG")
    rows = []
    for day, value in sorted(history.preview_by_date.items()):
        point = {"date": day, "net_value": value, "net_value_status": "estimated"}
        if calendar.first_session.date() <= day <= calendar.last_session.date() and not calendar.is_session(day.isoformat()):
            price_day = calendar.date_to_session(day.isoformat(), direction="previous").date()
            point["price_dates"] = {"market_close": price_day}
        rows.append(point)
    return rows
