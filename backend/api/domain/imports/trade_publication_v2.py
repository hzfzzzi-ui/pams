"""Signed, source-bound preview and publication of reviewed investment facts."""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import time

from domain.imports import native_source, native_source_http, source_records_v2


def _token(plan: dict, bucket: int) -> str:
    message = json.dumps({'plan': plan, 'bucket': bucket}, ensure_ascii=True,
                         sort_keys=True, separators=(',', ':')).encode('ascii')
    return hmac.new(native_source_http._token_key(), b'pams-source-record-trade-v1:' + message, sha256).hexdigest()


def preview(cur, *, source_id, revision: int) -> dict:
    plan = source_records_v2.publish_trade_source(cur, source_id=source_id, expected_revision=revision, preview_only=True)
    return {**plan, 'preview_token': _token(plan, int(time.time() // 30))}


def publish(cur, *, source_id, revision: int, preview_token: str) -> dict:
    if not native_source.enabled():
        raise ValueError('import publication is disabled')
    if not isinstance(preview_token, str) or len(preview_token) != 64:
        raise ValueError('invalid preview token')
    plan = source_records_v2.publish_trade_source(cur, source_id=source_id, expected_revision=revision, preview_only=True)
    now = int(time.time() // 30)
    if not any(hmac.compare_digest(preview_token, _token(plan, now - n)) for n in range(31)):
        raise ValueError('preview expired or source changed')
    return source_records_v2.publish_trade_source(cur, source_id=source_id, expected_revision=revision)
