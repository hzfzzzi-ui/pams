"""Atomically publish ready THS records while retaining unresolved funding."""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import hmac
import time
from uuid import UUID, uuid4

from domain.imports import account_import_store as store
from domain.imports import native_source as native
from domain.imports import native_source_http as http
from domain.imports.unified_source import attach_file


def publish_ready(cur, batch_id: UUID, revision: int, preview_token: str) -> dict:
    native._require(cur)
    if not native.enabled():
        raise store.ImportProblem('publish_disabled', '导入发布未启用', 403)
    accounts = native.lock_publication_accounts(cur, batch_id)
    current = http.batch(cur, batch_id, lock=True)
    if current['status'] == 'published':
        cur.execute('SELECT published_source_hash,published_result,revision FROM source_batches WHERE batch_id=%s', (str(batch_id),))
        saved = cur.fetchone()
        partial = (saved['published_result'] or {}).get('partial') or {}
        if partial.get('request_revision') != revision or not hmac.compare_digest(partial.get('request_token_hash') or '', sha256(preview_token.encode()).hexdigest()):
            raise store.ImportProblem('preview_required', '请核对已发布批次的处理结果', 409)
        try:
            result = native._replay(cur, batch_id, saved['revision'], saved['published_source_hash'])
        except ValueError as exc:
            raise store.ImportProblem('source_evidence_changed', '已发布来源或事实已变化，请人工核对', 409) from exc
        result['records'] = result.pop('details')
        result['batch'] = current
        return result

    store.editable(current, revision)
    prepared = native.preview_native(cur, batch_id=batch_id, revision=revision, locked_accounts=accounts)
    preview = prepared['preview']
    now = int(time.time() // 30)
    if not any(hmac.compare_digest(preview_token, http._token(preview['preview_token'], prepared['source_sha256'], revision, now - delta))
               for delta in range(31)):
        raise store.ImportProblem('source_changed', '资料已变化或预检过期，请重新预检', 409)
    if not preview.get('can_publish_ready'):
        raise store.ImportProblem('partial_unavailable', '当前批次不能部分录入，请先处理阻断或其他待确认项', 409)

    pending_ids = {str(row['row_id']) for row in preview['rows'] if row['status'] == 'pending'}
    pending = [row for row in current['rows'] if row['row_id'] in pending_ids]
    if not pending or len(pending) != len(pending_ids):
        raise ValueError('deferred source selection changed')
    deferred_id = native.create_manual_batch(cur, account_code=current['account_code'], kind='trade',
        completeness='partial', institution=current['account_code'], mode='upload')
    for old_file_id in sorted({row['source']['file_id'] for row in pending}):
        cur.execute('SELECT * FROM source_files WHERE batch_id=%s AND file_id=%s FOR SHARE', (str(batch_id), old_file_id))
        original = cur.fetchone()
        if not original:
            raise ValueError('deferred original file missing')
        new_file = uuid4()
        digest = attach_file(cur, file_id=new_file, batch_id=deferred_id, filename=original['filename'],
                             file_format=original['file_format'], content_type=original['content_type'],
                             content=bytes(original['content']))
        if digest != str(original['sha256']).strip():
            raise ValueError('deferred original file hash differs')
        copied = []
        for row in pending:
            if row['source']['file_id'] != old_file_id:
                continue
            copied.append({'fields': row['fields'], 'issues': row['issues'], 'excluded': False,
                'source': {**row['source'], 'deferred_from': {'batch_id': str(batch_id),
                    'row_id': row['row_id'], 'file_id': old_file_id}}})
        http._append_parsed(cur, deferred_id, new_file, digest,
                            {**original['current_metadata'], 'origin': 'file', 'rows': copied})

    for row_id in sorted(pending_ids):
        cur.execute("""UPDATE source_items SET review_status='excluded',
            parsed_fields=jsonb_set(parsed_fields, '{excluded}', 'true'::jsonb), updated_at=now()
            WHERE batch_id=%s AND source_item_id=%s AND review_status IN ('pending','confirmed')""", (str(batch_id), row_id))
        if cur.rowcount != 1:
            raise ValueError('deferred source review changed')
    ready_ids = {str(row['row_id']) for row in preview['rows'] if row['status'] == 'ready'}
    for row_id in sorted(ready_ids):
        cur.execute("""UPDATE source_items SET review_status='confirmed',updated_at=now()
            WHERE batch_id=%s AND source_item_id=%s AND review_status='pending'""", (str(batch_id), row_id))
    updated = http.batch(cur, batch_id, lock=True)
    final = native.preview_native(cur, batch_id=batch_id, revision=updated['revision'], locked_accounts=accounts)
    if not final['preview']['can_publish']:
        raise ValueError('ready rows changed after deferral')
    result = native.publish_native(cur, batch_id=batch_id, revision=updated['revision'],
        source_sha256=final['source_sha256'], preview_token=final['preview']['preview_token'],
        prepared_at=datetime.now(UTC).isoformat(), published_token=preview_token, locked_accounts=accounts,
        partial_info={'request_revision': revision, 'request_token_hash': sha256(preview_token.encode()).hexdigest(), 'deferred_batch_id': str(deferred_id),
                      'deferred_count': len(pending)})
    result['records'] = result.pop('details')
    result['batch'] = http.batch(cur, batch_id)
    return result
