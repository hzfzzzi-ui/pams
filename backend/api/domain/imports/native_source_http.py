"""Isolated source-first implementation of the existing import HTTP contract."""
from __future__ import annotations

import hmac
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from psycopg2.extras import Json

from domain.imports import account_import_store as store
from domain.imports import native_source as native
from domain.imports import isolation
from domain.imports.account_import_review import (
    DECISIONS,
    editable_fields,
    image_fingerprint,
    image_reviewed,
)
from domain.imports.unified_source import add_item, attach_file


def _token_key():
    mode = isolation.source_mode()
    key_name = ('PAMS_SOURCE_DEV_TOKEN_KEY' if mode == 'development' else
                'PAMS_SOURCE_TEST_TOKEN_KEY' if mode == 'test' else 'PAMS_IMPORT_RUNTIME_TOKEN_KEY')
    value = os.environ.get(key_name, '')
    try:
        key = bytes.fromhex(value)
    except ValueError as exc:
        raise store.ImportProblem('source_token_unconfigured', '来源预检密钥未配置', 503) from exc
    if (len(value) != 64 or len(key) != 32 or
        (mode != 'test' and value.lower() == os.environ.get('PAMS_SOURCE_TEST_TOKEN_KEY', '').lower())):
        raise store.ImportProblem('source_token_unconfigured', '来源预检密钥未配置或与测试密钥相同', 503)
    return key


def batch(cur, batch_id, *, lock=False):
    native._require(cur)
    cur.execute("SELECT * FROM source_batches WHERE batch_id=%s AND workflow='account_import'" + (' FOR UPDATE' if lock else ''), (str(batch_id),))
    raw = cur.fetchone()
    if not raw:
        raise store.ImportProblem('batch_missing', '找不到导入批次', 404)
    result = dict(raw)
    result.pop('published_preview_token', None)
    result['kind'], result['mode'] = result.pop('business_type'), result.pop('acquisition_method')
    cur.execute('SELECT * FROM source_files WHERE batch_id=%s ORDER BY created_at,file_id', (str(batch_id),))
    result['files'] = []
    for file in cur.fetchall():
        metadata = {**file['current_metadata']}
        if file['review_fingerprint']:
            metadata['review_fingerprint'] = file['review_fingerprint']
        result['files'].append({'file_id': str(file['file_id']), 'filename': file['filename'],
                                'content_type': file['content_type'], 'sha256': file['sha256'], 'metadata': metadata})
    cur.execute('SELECT * FROM source_items WHERE batch_id=%s ORDER BY source_order', (str(batch_id),))
    result['rows'] = []
    for item in cur.fetchall():
        values = item['parsed_fields']
        row = {'row_id': str(item['source_item_id']), **values,
               'source': item['raw_fields'].get('source', {'origin': 'manual'})}
        row['can_edit_fields'] = editable_fields(row)
        result['rows'].append(row)
    return store.norm(result)


def create(cur, account_code, mode, kind, completeness):
    native._require(cur)
    if account_code not in store.PROFILES and not (kind == 'cash' and store.statement.is_statement(account_code)):
        raise store.ImportProblem('unsupported_account', '该账户不在本轮投资导入范围')
    key = native.create_manual_batch(cur, account_code=account_code, kind=kind, completeness=completeness,
                                     institution=account_code, mode=mode)
    return batch(cur, key)


def list_batches(cur, limit):
    native._require(cur)
    cur.execute("""SELECT b.batch_id,b.account_code,b.acquisition_method AS mode,b.business_type AS kind,
        b.completeness,b.status,b.revision,b.created_at,b.updated_at,count(i.source_item_id) AS row_count
        FROM source_batches b LEFT JOIN source_items i ON i.batch_id=b.batch_id WHERE b.workflow='account_import'
        GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT %s""", (limit,))
    return {'rows': store.norm([dict(row) for row in cur.fetchall()])}


def get_file(cur, batch_id, file_id):
    native._require(cur)
    cur.execute('SELECT * FROM source_files WHERE batch_id=%s AND file_id=%s', (str(batch_id), str(file_id)))
    row = cur.fetchone()
    if not row:
        raise store.ImportProblem('file_missing', '找不到本批次原始资料', 404)
    return dict(row)


def _validate_fields(fields):
    if not isinstance(fields, dict) or set(fields) - store.FIELDS:
        raise store.ImportProblem('invalid_fields', '记录包含未知字段')
    if any(v is not None and (not isinstance(v, str) or len(v) > 500) for v in fields.values()):
        raise store.ImportProblem('invalid_value', '字段必须为文本或空值；金额不能经过浮点转换')


def _write_item(cur, batch_id, row, *, review_status):
    parsed = {key: row[key] for key in ('fields', 'issues', 'acknowledged', 'excluded')}
    cur.execute('UPDATE source_items SET parsed_fields=%s,review_status=%s,updated_at=now() WHERE batch_id=%s AND source_item_id=%s',
                (Json(parsed), review_status, str(batch_id), row['row_id']))
    if cur.rowcount != 1:
        raise ValueError('source record missing')


def save(cur, batch_id, revision, rows, completeness):
    native._require(cur)
    old = batch(cur, batch_id, lock=True)
    store.editable(old, revision)
    if len(rows) > store.MAX_ROWS or len(json.dumps(rows, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
        raise store.ImportProblem('draft_size', '候选记录超过容量', 413)
    previous = {r['row_id']: r for r in old['rows']}
    cleaned, seen = [], set()
    for row in rows:
        fields = row.get('fields')
        _validate_fields(fields)
        key = row.get('row_id')
        if key and key not in previous:
            raise store.ImportProblem('unknown_row', '候选记录不属于此批次')
        key = key or str(uuid4())
        if key in seen:
            raise store.ImportProblem('duplicate_row_id', '同一候选记录不能重复提交')
        seen.add(key)
        prior = previous.get(key)
        source = deepcopy(prior['source']) if prior else {'origin': 'manual'}
        if not prior and row.get('image_file_id'):
            image = get_file(cur, batch_id, str(UUID(str(row['image_file_id']))))
            if not image['content_type'].startswith('image/'):
                raise store.ImportProblem('not_image', '只能为本批截图补充核对记录')
            source = {'origin': 'ocr', 'file_id': str(image['file_id']), 'file_sha256': image['sha256'], 'human_added': True}
        # Statement direction is an explicit review decision; the immutable
        # uploaded source and its original parsed value remain unchanged.
        decisions = DECISIONS | ({'action'} if prior and old['kind'] == 'cash' and prior['source'].get('statement') is True else set())
        if prior and not editable_fields(prior) and any(
            fields.get(k) != prior['fields'].get(k) for k in (set(fields) | set(prior['fields'])) - decisions
        ):
            raise store.ImportProblem('file_row_readonly', '文件记录不能逐行修改；请调整列映射后重新解析')
        for flag in ('acknowledged', 'excluded'):
            if flag in row and not isinstance(row[flag], bool):
                raise store.ImportProblem('invalid_flag', '核对状态必须为布尔值')
        cleaned.append({'row_id': key, 'fields': fields, 'source': source,
                        'issues': prior['issues'] if prior else [], 'acknowledged': row.get('acknowledged', False),
                        'excluded': row.get('excluded', False)})
    if any(r['source'].get('file_id') and r['row_id'] not in seen for r in old['rows']):
        raise store.ImportProblem('file_row_readonly', '来源记录不能删除；请明确排除并保留原件')
    for key, prior in previous.items():
        if key not in seen:
            cur.execute('DELETE FROM source_items WHERE batch_id=%s AND source_item_id=%s', (str(batch_id), key))
    candidate = {**old, 'rows': cleaned}
    for row in cleaned:
        if row['row_id'] not in previous:
            raw = {'source': row['source'], 'initial_fields': row['fields'], 'initial_issues': row['issues']}
            digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            source = row['source']
            add_item(cur, item_id=UUID(row['row_id']), batch_id=UUID(str(batch_id)), source_uid=row['row_id'],
                     file_id=UUID(source['file_id']) if source.get('file_id') else None,
                     parse_method='ocr' if source.get('origin') == 'ocr' else 'manual',
                     record_hash=digest, raw_fields=raw, parsed_fields={})
        status = ('excluded' if row['excluded'] else
                  'confirmed' if row['source'].get('origin') != 'ocr' or
                  (row['acknowledged'] and image_reviewed(candidate, row['source'].get('file_id'))) else 'pending')
        _write_item(cur, batch_id, row, review_status=status)
    cur.execute('UPDATE source_batches SET completeness=%s,revision=revision+1,updated_at=now() WHERE batch_id=%s',
                (completeness, str(batch_id)))
    return batch(cur, batch_id)


def _append_parsed(cur, batch_id, file_id, digest, parsed):
    current = batch(cur, batch_id, lock=True)
    old = [r for r in current['rows'] if r['source'].get('file_id') == str(file_id)]
    if len(current['rows']) - len(old) + len(parsed['rows']) > store.MAX_ROWS:
        raise store.ImportProblem('too_many_rows', '每批最多2000条记录')
    for row in old:
        cur.execute('DELETE FROM source_items WHERE batch_id=%s AND source_item_id=%s', (str(batch_id), row['row_id']))
    for parsed_row in parsed['rows']:
        item_id = uuid4()
        source = {**parsed_row.get('source', {}), 'file_id': str(file_id), 'file_sha256': digest,
                  'origin': parsed.get('origin', 'file')}
        raw = {'source': source, 'initial_fields': parsed_row['fields'], 'initial_issues': parsed_row.get('issues', [])}
        record_hash = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        values = {'fields': parsed_row['fields'], 'issues': parsed_row.get('issues', []),
                  'acknowledged': False, 'excluded': bool(parsed_row.get('excluded', False))}
        method = 'ocr' if source['origin'] == 'ocr' else 'template'
        add_item(cur, item_id=item_id, batch_id=UUID(str(batch_id)), source_uid=str(item_id), file_id=UUID(str(file_id)),
                 parse_method=method, record_hash=record_hash, raw_fields=raw, parsed_fields=values)
        status = 'excluded' if values['excluded'] else 'pending'
        cur.execute('UPDATE source_items SET review_status=%s WHERE source_item_id=%s', (status, str(item_id)))
    metadata = {key: value for key, value in parsed.items() if key != 'rows'}
    cur.execute('UPDATE source_files SET current_metadata=%s,review_fingerprint=NULL WHERE batch_id=%s AND file_id=%s',
                (Json(metadata), str(batch_id), str(file_id)))


def upload(cur, batch_id, filename, content, content_type):
    native._require(cur)
    result = batch(cur, batch_id, lock=True)
    store.editable(result)
    if not filename or len(filename) > 180 or any(c in filename for c in ('/', '\\', '\x00', '\r', '\n')):
        raise store.ImportProblem('invalid_filename', '文件名无效')
    if not 0 < len(content) <= store.MAX_FILE_BYTES:
        raise store.ImportProblem('file_size', '文件为空或超过10MB', 413)
    digest = sha256(content).hexdigest()
    cur.execute('SELECT count(*) AS n,COALESCE(sum(octet_length(content)),0) AS size FROM source_files WHERE batch_id=%s', (str(batch_id),))
    limits = cur.fetchone()
    cur.execute('SELECT 1 FROM source_files WHERE batch_id=%s AND sha256=%s', (str(batch_id), digest))
    if cur.fetchone():
        return result
    if limits['n'] >= store.MAX_FILES or limits['size'] + len(content) > store.MAX_BATCH_BYTES:
        raise store.ImportProblem('batch_size', '每批最多10个文件、合计30MB', 413)
    cur.execute('SELECT mapping FROM source_templates WHERE account_code=%s AND business_type=%s',
                (result['account_code'], result['kind']))
    template = cur.fetchone()
    parsed = store.parse(content, filename, result['account_code'], result['kind'], template['mapping'] if template else None)
    suffix = filename.rsplit('.', 1)[-1].lower()
    safe = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'csv': 'text/plain',
            'tsv': 'text/plain', 'xls': 'text/plain', 'pdf': 'application/pdf',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}.get(suffix)
    if not safe:
        raise store.ImportProblem('file_type', '仅支持CSV、TSV、同花顺文本XLS、XLSX、PNG、JPEG及账单PDF')
    key = uuid4()
    attach_file(cur, file_id=key, batch_id=UUID(str(batch_id)), filename=filename,
                file_format='jpeg' if suffix == 'jpg' else suffix, content_type=safe, content=content)
    _append_parsed(cur, batch_id, key, digest, parsed)
    initialized = None
    if result['account_code'] == 'tonghuashun' and result['kind'] == 'trade':
        from domain.imports.ths_asset_candidates import initialize
        initialized = initialize(cur, parsed, batch_id=batch_id, file_id=key, file_sha256=digest)
    updated = batch(cur, batch_id)
    if initialized is not None:
        updated['asset_initialization'] = initialized
    return updated


def reparse(cur, batch_id, file_id, mapping, kind=None):
    native._require(cur)
    result = batch(cur, batch_id, lock=True)
    store.editable(result)
    if kind is not None and kind != result['kind']:
        raise store.ImportProblem('kind_mismatch', '重新解析不能改变批次资料类型')
    file = get_file(cur, batch_id, file_id)
    parsed = store.parse(bytes(file['content']), file['filename'], result['account_code'], result['kind'], mapping)
    _append_parsed(cur, batch_id, file_id, file['sha256'], parsed)
    cur.execute("""INSERT INTO source_templates(account_code,business_type,mapping) VALUES (%s,%s,%s)
        ON CONFLICT (account_code,business_type) DO UPDATE SET mapping=EXCLUDED.mapping,updated_at=now()""",
        (result['account_code'], result['kind'], Json(mapping)))
    return batch(cur, batch_id)


def review_image(cur, batch_id, file_id, revision):
    native._require(cur)
    result = batch(cur, batch_id, lock=True)
    store.editable(result, revision)
    file = get_file(cur, batch_id, file_id)
    if not file['content_type'].startswith('image/'):
        raise store.ImportProblem('not_image', '仅截图可以逐图核对')
    members = [r for r in result['rows'] if r['source'].get('file_id') == str(file_id)]
    if not members:
        raise store.ImportProblem('empty_image', '截图尚未识别出记录，不能标记为已核对')
    for row in members:
        row['acknowledged'] = True
        _write_item(cur, batch_id, row, review_status='excluded' if row['excluded'] else 'confirmed')
    fingerprint = image_fingerprint(result, str(file_id))
    cur.execute('UPDATE source_files SET review_fingerprint=%s WHERE batch_id=%s AND file_id=%s',
                (fingerprint, str(batch_id), str(file_id)))
    return batch(cur, batch_id)


def _token(preflight_token, source_sha, revision, bucket):
    value = f'{preflight_token}:{source_sha}:{revision}:{bucket}'.encode()
    return hmac.new(_token_key(), value, sha256).hexdigest()


def preview(cur, batch_id, revision):
    native._require(cur)
    accounts = native.lock_publication_accounts(cur, batch_id)
    store.editable(batch(cur, batch_id, lock=True), revision)
    result = native.preview_native(cur, batch_id=UUID(str(batch_id)), revision=revision, locked_accounts=accounts)
    value = result['preview']
    value['preview_token'] = _token(value['preview_token'], result['source_sha256'], revision, int(time.time() // 30))
    return value


def publish(cur, batch_id, revision, preview_token):
    native._require(cur)
    if not native.enabled():
        raise store.ImportProblem('publish_disabled', '导入发布未启用', 403)
    accounts = native.lock_publication_accounts(cur, batch_id)
    current = batch(cur, batch_id, lock=True)
    if current['status'] == 'published':
        cur.execute('SELECT published_preview_token FROM source_batches WHERE batch_id=%s', (str(batch_id),))
        saved_token = cur.fetchone()['published_preview_token']
        if current['revision'] != revision or not hmac.compare_digest(saved_token or '', preview_token):
            raise store.ImportProblem('preview_required', '请重新预检后发布', 409)
        try:
            result = native._replay(cur, UUID(str(batch_id)), revision, current['published_source_hash'])
        except ValueError as exc:
            raise store.ImportProblem('source_evidence_changed', '已发布来源或事实已变化，请人工核对', 409) from exc
    else:
        store.editable(current, revision)
        prepared = native.preview_native(cur, batch_id=UUID(str(batch_id)), revision=revision, locked_accounts=accounts)
        raw_token = prepared['preview']['preview_token']
        now = int(time.time() // 30)
        if not any(hmac.compare_digest(preview_token, _token(raw_token, prepared['source_sha256'], revision, now - delta))
                   for delta in range(31)):
            raise store.ImportProblem('source_changed', '资料已变化或预检过期，请重新预检', 409)
        try:
            result = native.publish_native(cur, batch_id=UUID(str(batch_id)), revision=revision,
                source_sha256=prepared['source_sha256'], preview_token=raw_token,
                prepared_at=datetime.now(UTC).isoformat(), published_token=preview_token, locked_accounts=accounts)
        except ValueError as exc:
            raise store.ImportProblem('source_preflight_blocked', '来源核对不完整或已变化，请重新核对和预检', 409) from exc
    result['records'] = result.pop('details')
    result['batch'] = batch(cur, batch_id)
    return result
