"""Adjacent mainland sessions for holiday estimates using the last disclosed price."""
from functools import lru_cache

import exchange_calendars

from domain.market.month_end_marks import is_month_end


@lru_cache(maxsize=2048)
def surrounding_sessions(day):
    calendar = exchange_calendars.get_calendar('XSHG')
    if not calendar.first_session.date() < day < calendar.last_session.date() or calendar.is_session(day):
        return None
    return (calendar.date_to_session(day, direction='previous').date(),
            calendar.date_to_session(day, direction='next').date())


def mainland_mark_floor(day):
    if is_month_end(day):
        sessions = surrounding_sessions(day)
        if sessions:
            return sessions[0]
    return day
