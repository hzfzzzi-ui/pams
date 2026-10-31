"""Privacy-limited technical events for an external log collector.

These events are observations, not transaction commits or business evidence.
The application does not configure or assume a particular log collector.
"""
from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger('pams.operations')
_KNOWN_STATUS = frozenset({'running', 'success', 'partial', 'failed', 'skipped', 'blocked'})


def emit_status_observation(row: Any) -> None:
    """Emit a bounded observation without error text, payloads or identifiers.

    A broken handler must not change the sync-center API result. Missing or
    unrecognized statuses remain unknown, never a fabricated success.
    """
    status = row.get('status') if isinstance(row, dict) else None
    if not isinstance(status, str) or status not in _KNOWN_STATUS:
        status = 'unknown'
    event = {'schema': 'pams.runtime.v1', 'event': 'sync_status_observed', 'status': status}
    try:
        logger.info('%s', json.dumps(event, sort_keys=True, separators=(',', ':')))
    except Exception:
        # A logging backend is independent from the database read contract.
        pass
