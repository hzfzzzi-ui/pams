"""Technical logging must not expose business data or change reads."""
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.operations import runtime_events, run_status


def test_observation_is_allowlisted_and_missing_status_is_unknown(caplog):
    with caplog.at_level(logging.INFO, logger='pams.operations'):
        runtime_events.emit_status_observation({'status': 'failed', 'error_text': 'private-secret',
                                                'summary_json': {'account': 'private-account'}})
        runtime_events.emit_status_observation({'status': 'private-secret'})
        runtime_events.emit_status_observation(None)
    events = [json.loads(record.message) for record in caplog.records]
    assert events == [
        {'schema': 'pams.runtime.v1', 'event': 'sync_status_observed', 'status': 'failed'},
        {'schema': 'pams.runtime.v1', 'event': 'sync_status_observed', 'status': 'unknown'},
        {'schema': 'pams.runtime.v1', 'event': 'sync_status_observed', 'status': 'unknown'},
    ]
    assert 'private-secret' not in caplog.text and 'private-account' not in caplog.text


def test_logging_failure_does_not_change_status_response(monkeypatch):
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
