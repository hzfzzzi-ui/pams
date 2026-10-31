"""Privacy-limited technical events for an external log collector.

These events are observations, not transaction commits or business evidence.
The application does not configure or assume a particular log collector.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger('uvicorn.error')
_KNOWN_STATUS = frozenset({'running', 'success', 'partial', 'failed', 'skipped', 'blocked'})


def _emit(event_name: str, row: Any) -> None:
    status = row.get('status') if isinstance(row, dict) else None
    if not isinstance(status, str) or status not in _KNOWN_STATUS:
        status = 'unknown'
    event = {'schema': 'pams.runtime.v1', 'component': 'api', 'event': event_name,
             'status': status, 'time': datetime.now(timezone.utc).isoformat()}
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


def emit_sync_commit_failed() -> None:
    """Expose a failed persistence attempt without claiming a committed run."""
    _emit('ibkr_sync_commit_failed', {'status': 'failed'})
