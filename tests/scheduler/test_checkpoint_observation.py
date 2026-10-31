"""Scheduler technical events do not leak checkpoint payloads or affect writes."""
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/scheduler'))
import refresh_state


def test_checkpoint_event_excludes_sensitive_payload_and_never_fakes_success(tmp_path, caplog):
    path = tmp_path / 'state.json'
    with caplog.at_level(logging.INFO, logger='pams.scheduler.events'):
        refresh_state.store_job(path, 'synthetic', {'status': 'blocked', 'issues': ['private-secret']})
        refresh_state.store_job(path, 'synthetic', {'status': 'private-secret'})
    events = [json.loads(record.message) for record in caplog.records]
    assert events == [
        {'schema': 'pams.runtime.v1', 'event': 'scheduler_checkpoint_saved', 'status': 'blocked'},
        {'schema': 'pams.runtime.v1', 'event': 'scheduler_checkpoint_saved', 'status': 'unknown'},
    ]
    assert 'private-secret' not in caplog.text


def test_checkpoint_survives_logging_handler_failure(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('collector unavailable')
    monkeypatch.setattr(refresh_state._event_logger, 'info', broken)
    path = tmp_path / 'state.json'
    refresh_state.store_job(path, 'synthetic', {'status': 'success'})
    assert refresh_state.read_state(path)['jobs']['synthetic']['status'] == 'success'
