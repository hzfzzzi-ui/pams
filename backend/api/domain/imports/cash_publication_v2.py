"""Signed previews and transactional publication for redesigned cash sources."""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import time

from domain.imports import native_source, native_source_http, source_records_v2
from domain.imports.cash_preview_v2 import plan_cash, preview_cash


def _token(preview: dict, mode: str, bucket: int) -> str:
    payload = {'source_id': preview['source_id'], 'revision': preview['revision'],
               'counts': preview['counts'], 'rows': preview['rows'], 'mode': mode, 'bucket': bucket}
    message = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return hmac.new(native_source_http._token_key(), b'pams-source-record-cash-v1:' + message, sha256).hexdigest()


def preview(cur, *, source_id, revision: int, mode: str) -> dict:
    if mode not in {'all', 'ready'}:
        raise ValueError('invalid publication mode')
    value = preview_cash(cur, source_id=source_id, expected_revision=revision)
    allowed = value['can_publish'] if mode == 'all' else value['can_publish_ready']
    value['publication_mode'] = mode
    value['preview_token'] = _token(value, mode, int(time.time() // 30)) if allowed else None
    return value


def publish(cur, *, source_id, revision: int, mode: str, preview_token: str) -> dict:
    if not native_source.enabled():
        raise ValueError('import publication is disabled')
    if mode not in {'all', 'ready'} or not isinstance(preview_token, str) or len(preview_token) != 64:
        raise ValueError('invalid publication request')
    plan = plan_cash(cur, source_id=source_id, expected_revision=revision, for_write=True)
    value = plan['preview']
    allowed = value['can_publish'] if mode == 'all' else value['can_publish_ready']
    now = int(time.time() // 30)
    if not allowed or not any(hmac.compare_digest(preview_token, _token(value, mode, now - n)) for n in range(31)):
        raise ValueError('preview expired or source changed')
    return source_records_v2.apply_cash_plan(cur, source_id=source_id, plan=plan, defer_unresolved=mode == 'ready')
