"""Adjacent mainland sessions for holiday estimates using the last disclosed price."""
from functools import lru_cache

import exchange_calendars


@lru_cache(maxsize=2048)
def surrounding_sessions(day):
    calendar = exchange_calendars.get_calendar('XSHG')
    if not calendar.first_session.date() < day < calendar.last_session.date() or calendar.is_session(day):
        return None
    return (calendar.date_to_session(day, direction='previous').date(),
            calendar.date_to_session(day, direction='next').date())
