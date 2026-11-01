from __future__ import annotations

import types

import pytest

from tests.scheduler.test_asset_refresh_scheduler import _load_scheduler


class StopLoop(Exception):
    pass


@pytest.mark.parametrize("skip", [None, "false", "true"])
def test_startup_option_only_controls_immediate_jobs(monkeypatch, skip):
    scheduler = _load_scheduler(monkeypatch)
    if skip is None:
        monkeypatch.delenv("PAMS_SCHEDULER_SKIP_STARTUP_REFRESH", raising=False)
    else:
        monkeypatch.setenv("PAMS_SCHEDULER_SKIP_STARTUP_REFRESH", skip)

    calls = []
    for name in (
        "refresh_us_quotes", "refresh_overseas_quotes", "refresh_crypto",
        "run_flex_confirmation", "run_daily_close_confirmation", "run_cash_event_confirmation",
    ):
        monkeypatch.setattr(scheduler, name, lambda name=name: calls.append(name))
    monkeypatch.setattr(scheduler, "run_due_window", lambda region: calls.append(f"due:{region}"))
    sleeps = []
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: sleeps.append(seconds))

    jobs = []

    class Job:
        def __init__(self, interval):
            self.interval = interval
            self.unit = None
            self.at_time = None

        @property
        def day(self):
            self.unit = "day"
            return self

        @property
        def minutes(self):
            self.unit = "minutes"
            return self

        def at(self, value, timezone):
            self.at_time = (value, timezone)
            return self

        def do(self, callback):
            jobs.append((self.interval, self.unit, self.at_time, callback))

    def stop():
        raise StopLoop

    monkeypatch.setattr(scheduler, "schedule", types.SimpleNamespace(
        every=lambda interval=1: Job(interval), run_pending=stop,
    ))
    with pytest.raises(StopLoop):
        scheduler.main()

    assert sleeps == [10]
    startup = [
        "refresh_us_quotes", "refresh_overseas_quotes", "refresh_crypto",
        "run_flex_confirmation", "run_daily_close_confirmation", "run_cash_event_confirmation",
        "due:asia", "due:north_america",
    ]
    assert calls == ([] if skip == "true" else startup)
    assert [(interval, unit, at) for interval, unit, at, _ in jobs] == [
        (1, "day", (scheduler.ASIA_PACIFIC_REFRESH_TIME, scheduler.ASIA_PACIFIC_REFRESH_TZ)),
        (1, "day", (scheduler.NORTH_AMERICA_REFRESH_TIME, scheduler.NORTH_AMERICA_REFRESH_TZ)),
        (30, "minutes", None), (5, "minutes", None), (5, "minutes", None),
        (30, "minutes", None), (30, "minutes", None), (30, "minutes", None),
        (30, "minutes", None), (30, "minutes", None),
    ]

    calls.clear()
    for _, _, _, callback in jobs:
        callback()
    assert calls == [
        "due:asia", "due:north_america", "refresh_crypto", "refresh_us_quotes",
        "refresh_overseas_quotes", "due:asia", "due:north_america",
        "run_flex_confirmation", "run_daily_close_confirmation", "run_cash_event_confirmation",
    ]


def test_development_mode_disables_scheduler_and_direct_calls(monkeypatch):
    scheduler = _load_scheduler(monkeypatch)
    monkeypatch.setenv('PAMS_SOURCE_MODE', 'development')
    def forbidden(*args, **kwargs):
        pytest.fail('development scheduler attempted network or scheduling')
    monkeypatch.setattr(scheduler.requests, 'get', forbidden)
    monkeypatch.setattr(scheduler.requests, 'post', forbidden)
    monkeypatch.setattr(scheduler.schedule, 'every', forbidden)
    assert scheduler._get('anything', 'synthetic') is None
    assert scheduler._request_json('post', 'http://example.invalid', 'synthetic', failure_details=True) == {
        'success': False, 'status': 'disabled', 'reason': 'development_mode'
    }
    assert scheduler.main() is None
