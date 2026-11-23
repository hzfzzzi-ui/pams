"""Reporting-date marks preserve both the valuation day and its independent fixing day."""
from calendar import monthrange

from domain.market.valuation_fx import fx_rate_at


def is_month_end(day):
    return day.day == monthrange(day.year, day.month)[1]


def month_end_fx(timelines, currency, day, base_currency):
    rate = fx_rate_at(timelines, currency, day, base_currency)
    if rate is not None:
        return day, rate
    if is_month_end(day) and day.weekday() >= 5:
        for prior, rate in reversed(timelines.get(currency.upper(), ())):
            if day.replace(day=1) <= prior < day:
                if rate.is_finite() and rate > 0:
                    return prior, rate
                break
    return None
