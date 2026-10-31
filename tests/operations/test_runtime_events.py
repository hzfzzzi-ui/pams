"""Technical logging must not expose business data or change reads."""
import json
import logging
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.operations import runtime_events, run_status


def test_observation_is_allowlisted_and_missing_status_is_unknown(caplog):
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        runtime_events.emit_status_observation({'status': 'failed', 'error_text': 'private-secret',
                                                'summary_json': {'account': 'private-account'}})
        runtime_events.emit_status_observation({'status': 'private-secret'})
        runtime_events.emit_status_observation(None)
    events = [json.loads(record.message) for record in caplog.records]
    for event in events:
        assert datetime.fromisoformat(event.pop('time')).utcoffset() is not None
    assert events == [
        {'schema': 'pams.runtime.v1', 'component': 'api', 'event': 'sync_status_observed', 'status': 'failed'},
        {'schema': 'pams.runtime.v1', 'component': 'api', 'event': 'sync_status_observed', 'status': 'unknown'},
        {'schema': 'pams.runtime.v1', 'component': 'api', 'event': 'sync_status_observed', 'status': 'unknown'},
    ]
    assert caplog.records[0].levelno == logging.WARNING
    assert 'private-secret' not in caplog.text and 'private-account' not in caplog.text


def test_logging_failure_does_not_change_status_response(monkeypatch, capsys):
    class Cursor:
        def execute(self, query, params):
            self.params = params

        def fetchone(self):
            return {'status': 'success', 'summary_json': {'publication': 'preserved'}}

    def broken(*args, **kwargs):
        raise RuntimeError('unavailable')

    monkeypatch.setattr(runtime_events.logger, 'info', broken)
    cursor = Cursor()
    result = run_status.latest_sync_run(cursor, 'synthetic-job', 'synthetic-scope')
    assert result == {'status': 'success', 'summary_json': {'publication': 'preserved'}}
    assert cursor.params == ['synthetic-job', 'synthetic-scope']
    assert json.loads(capsys.readouterr().err)['event'] == 'logging_failed'


def test_sync_run_event_occurs_only_after_commit_and_commit_failure_is_visible(monkeypatch):
    from collectors import ibkr_flex

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, *args): pass
        def fetchone(self):
            self.count += 1
            return {'id': 1, 'status': 'success'} if self.count == 1 else {'exists': False}
        count = 0

    class Connection:
        def __init__(self, fail=False): self.fail = fail
        def cursor(self, **kwargs): return Cursor()
        def commit(self):
            events.append('commit')
            if self.fail: raise RuntimeError('synthetic failure')

    events = []
    monkeypatch.setattr(ibkr_flex, 'ensure_sync_table', lambda conn: None)
    monkeypatch.setattr(ibkr_flex, '_sync_row_to_status', lambda row: row)
    monkeypatch.setattr(runtime_events, 'emit_sync_run_committed', lambda status: events.append(('committed', status)))
    monkeypatch.setattr(runtime_events, 'emit_sync_commit_failed', lambda: events.append('commit_failed'))
    ibkr_flex.record_sync_run(Connection(), status='success', started_at=datetime.now())
    assert events == ['commit', ('committed', 'success')]
    events.clear()
    import pytest
    with pytest.raises(RuntimeError, match='synthetic failure'):
        ibkr_flex.record_sync_run(Connection(fail=True), status='success', started_at=datetime.now())
    assert events == ['commit', 'commit_failed']
