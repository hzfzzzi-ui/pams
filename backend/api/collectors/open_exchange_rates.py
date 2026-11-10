"""Read a dated USD quote set from Open Exchange Rates."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ExchangeRatesUnavailable(ValueError):
    pass


def fetch_usd_rates(day: date, *, today: date, app_id: str, currencies: tuple[str, ...]):
    route = 'latest.json' if day == today else f'historical/{day.isoformat()}.json'
    query = urlencode({'app_id': app_id})
    request = Request(f'https://openexchangerates.org/api/{route}?{query}',
                      headers={'User-Agent': 'PAMS/2.0', 'Accept': 'application/json'})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(), parse_float=Decimal)
        if payload['base'] != 'USD' or type(payload['timestamp']) is not int:
            raise ValueError
        timestamp = datetime.fromtimestamp(payload['timestamp'], timezone.utc)
        if timestamp.date() != day:
            raise ValueError
        rates = {currency: Decimal(str(payload['rates'][currency])) for currency in currencies}
        if any(not rate.is_finite() or rate <= 0 for rate in rates.values()) or rates['USD'] != 1:
            raise ValueError
    except (URLError, OSError, ValueError, KeyError, TypeError, OverflowError, InvalidOperation):
        # Provider errors can contain the URL and App ID.
        raise ExchangeRatesUnavailable('Open Exchange Rates did not return a valid quote set for the requested date') from None
    return rates
