"""Stage TradingView CSV evidence for review in disposable databases only.

Source staging does not calculate positions or publish facts. Matching the
legacy six-field hash is insufficient to prove that two rows are duplicates.
"""
from __future__ import annotations

import csv
from hashlib import sha256
from io import StringIO
import json
from uuid import UUID, uuid5

from collectors.tradingview_portfolio import SUPPORTED_SIDES, _parse_time, _row_hash, _to_decimal
from domain.imports import isolation
from domain.imports.unified_source import add_item, attach_file, create_batch

REQUIRED = {'Symbol', 'Side', 'Qty', 'Fill Price', 'Commission', 'Closing Time'}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 30 * 1024 * 1024
MAX_ROWS = 2000


def _prepare(files):
    """Validate all rows before writing, preserving physical CSV locations."""
    if not files or len(files) > 10:
        raise ValueError('one to ten TradingView CSV files required')
    output, hashes, seen_rows = [], set(), {}
    total_bytes = total_rows = 0
    for filename, content in files:
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 180 or
            any(char in filename for char in ('/', '\\', '\x00', '\r', '\n')) or
            not filename.lower().endswith('.csv')):
            raise ValueError('invalid TradingView CSV filename')
        if not isinstance(content, bytes) or not content or len(content) > MAX_FILE_BYTES:
            raise ValueError('invalid TradingView CSV content size')
        total_bytes += len(content)
        if total_bytes > MAX_BATCH_BYTES:
            raise ValueError('TradingView batch exceeds size limit')
        file_hash = sha256(content).hexdigest()
        if file_hash in hashes:
            raise ValueError('repeated file content needs explicit confirmation')
        hashes.add(file_hash)
        try:
            reader = csv.DictReader(StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)) or not REQUIRED.issubset(headers):
                raise ValueError('invalid or ambiguous TradingView CSV headers')
            rows = []
            previous_line = reader.line_num
            for raw in reader:
                first, last = previous_line + 1, reader.line_num
                previous_line = last
                if None in raw or any(value is None for value in raw.values()):
                    raise ValueError('TradingView CSV row has missing or extra cells')
                if str(raw.get('Side', '')).strip() not in SUPPORTED_SIDES:
                    raise ValueError('unsupported TradingView side')
                _parse_time(raw.get('Closing Time', ''))
                for key in ('Qty', 'Fill Price', 'Commission'):
                    value = _to_decimal(raw.get(key), default=None)
                    if value is not None and not value.is_finite():
                        raise ValueError('non-finite TradingView amount')
                row_hash = _row_hash(raw)
                if row_hash in seen_rows:
                    raise ValueError('ambiguous TradingView rows require explicit confirmation')
                seen_rows[row_hash] = (file_hash, first, last)
                rows.append((first, last, row_hash, raw))
                total_rows += 1
                if total_rows > MAX_ROWS:
                    raise ValueError('TradingView batch exceeds row limit')
            output.append((filename, content, file_hash, rows))
        except (csv.Error, UnicodeError) as exc:
            raise ValueError('invalid TradingView CSV encoding or structure') from exc
    return output


def stage_csvs(cur, *, batch_id, account_code: str, files) -> dict:
    """Write raw files and pending source items in the caller's transaction."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('TradingView source requires isolated mode')
    if cur.connection.autocommit:
        raise ValueError('TradingView staging requires a transaction')
    batch_id = UUID(str(batch_id))
    prepared = _prepare(files)
    row_hashes = [row_hash for _, _, _, rows in prepared for _, _, row_hash, _ in rows]
    from domain.investments.holding_evidence_writer import lock_holding_accounts
    lock_holding_accounts(cur, [account_code])
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('tradingview-source:' + account_code,))
    cur.execute('''SELECT 1 FROM public.source_items i JOIN public.source_batches b USING (batch_id)
                   WHERE b.account_code=%s AND b.institution='tradingview'
                     AND i.raw_fields->>'legacy_six_field_hash' = ANY(%s::text[]) LIMIT 1''',
                (account_code, row_hashes))
    if cur.fetchone():
        raise ValueError('possible repeated TradingView source needs explicit confirmation')
    create_batch(cur, batch_id=batch_id, account_code=account_code, business_type='trade',
                 institution='tradingview', acquisition_method='upload')
    count = 0
    for filename, content, file_hash, rows in prepared:
        file_id = uuid5(batch_id, 'file:' + file_hash)
        attach_file(cur, file_id=file_id, batch_id=batch_id, filename=filename,
                    file_format='csv', content_type='text/csv', content=content)
        for first, last, row_hash, raw in rows:
            uid = f'{file_hash}:{first}:{last}'
            item_id = uuid5(batch_id, 'item:' + uid)
            evidence = {'file_sha256': file_hash, 'line_start': first, 'line_end': last,
                        'legacy_six_field_hash': row_hash, 'raw_fields': raw}
            encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            add_item(cur, item_id=item_id, batch_id=batch_id, source_uid=uid, file_id=file_id,
                     location=f'{first}-{last}', parse_method='template', record_hash=sha256(encoded).hexdigest(),
                     raw_fields=evidence, parsed_fields={})
            count += 1
    return {'batch_id': str(batch_id), 'files': len(prepared), 'items': count, 'review_required': True}
