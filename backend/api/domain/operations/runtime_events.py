"""Privacy-limited technical events for an external log collector.

These events are observations, not transaction commits or business evidence.
The application does not configure or assume a particular log collector.
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from pathlib import Path
import sys
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger('uvicorn.error')
_KNOWN_STATUS = frozenset({'running', 'success', 'partial', 'failed', 'skipped', 'blocked'})


def _emit(event_name: str, row: Any, *, exception: BaseException | None = None) -> None:
    status = row.get('status') if isinstance(row, dict) else None
    if not isinstance(status, str) or status not in _KNOWN_STATUS:
        status = 'unknown'
    event = {'schema': 'pams.runtime.v1', 'component': 'api', 'event': event_name,
             'status': status, 'time': datetime.now(timezone.utc).isoformat()}
    if exception is not None:
        name = type(exception).__name__
        event['error_type'] = name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name) else 'Exception'
        frames = traceback.extract_tb(exception.__traceback__, limit=8)
        event['stack'] = [{'file': Path(frame.filename).name if re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', Path(frame.filename).name) else 'unknown', 'line': frame.lineno,
                           'function': frame.name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', frame.name) else 'unknown'}
                          for frame in frames]
    try:
        log = logger.warning if status in {'failed', 'blocked', 'partial'} else logger.info
        log('%s', json.dumps(event, sort_keys=True, separators=(',', ':')))
    except Exception:
        # Do not disclose an exception or business payload in the fallback.
        try:
            sys.stderr.write('{"schema":"pams.runtime.v1","component":"api","event":"logging_failed"}\n')
            sys.stderr.flush()
        except Exception:
            pass  # If stderr is also unavailable, no in-process sink remains.


def emit_status_observation(row: Any) -> None:
    """Emit a bounded read observation; this does not prove a commit."""
    _emit('sync_status_observed', row)


def emit_sync_run_committed(status: str) -> None:
    """Report a technical result only after the existing database commit."""
    _emit('ibkr_sync_run_committed', {'status': status})


def emit_sync_commit_failed(*, exception: BaseException | None = None) -> None:
    """Expose a failed commit without logging data or claiming publication."""
    _emit('ibkr_sync_commit_failed', {'status': 'failed'}, exception=exception)


def emit_sync_attempt_result(status: str, *, exception: BaseException | None = None) -> None:
    """Log an unsuccessful attempt with sanitized type and stack locations."""
    _emit('ibkr_sync_attempt', {'status': status}, exception=exception)


def emit_cash_progress_result(status: str, *, exception: BaseException | None = None) -> None:
    """Log cash-processing outcome without source or exception content."""
    _emit('ibkr_cash_processing', {'status': status}, exception=exception)
