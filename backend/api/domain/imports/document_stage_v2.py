"""Stage account documents against the redesigned source model without posting facts."""
from __future__ import annotations

from hashlib import sha256
from pathlib import PurePath
from uuid import UUID, uuid4
from psycopg2.extras import Json

from domain.imports import account_import_store, document_kind, source_records_v2
from domain.imports.account_import_types import LICAITONG_CURRENT_PLUS
from domain.imports.template_assessment import assess_template


def _rows(parsed, kind, *, screenshot=False):
    if not screenshot and assess_template(parsed, kind) != 'matched':
        raise ValueError('document template is not confidently matched')
    rows = parsed.get('rows') or []
    if (not rows and not screenshot) or len(rows) > account_import_store.MAX_ROWS:
        raise ValueError('document has no rows or exceeds row limit')
    return rows


def _save_rows(cur, *, source_id, kind, parse_method, rows):
    excluded = 0
    for index, row in enumerate(rows, 1):
        fields = dict(row.get('fields') or {}); evidence = dict(row.get('source') or {})
        original = evidence.get('statement_row') or {}
        if kind == 'cash' and isinstance(original, dict):
            for field in ('posting_date', 'summary', 'description', 'counterparty'):
                if original.get(field) is not None:
                    fields[field] = original[field]
        position = {'row': evidence['row']} if evidence.get('row') is not None else {'row': index}
        if evidence.get('sheet') is not None:
            position['sheet'] = evidence['sheet']
        if evidence.get('page') is not None:
            position['page'] = evidence['page']
        item_id = uuid4()
        method = 'ocr' if evidence.get('parse_method') == 'ocr' else parse_method
        source_records_v2.add_item(cur, source_id=source_id, item_id=item_id, parse_method=method,
            raw_fields={'source': evidence, 'initial_fields': row.get('fields') or {}, 'initial_issues': row.get('issues') or []},
            parsed_fields=fields, source_position=position)
        if row.get('excluded'):
            cur.execute("UPDATE public.source_items SET review_status='excluded' WHERE source_item_id=%s", (str(item_id),))
            excluded += 1
    return excluded


def _saved_mapping(cur, account_code, kind):
    cur.execute('SELECT mapping FROM public.source_templates WHERE account_code=%s AND document_type=%s',
                (account_code, kind))
    saved = cur.fetchone()
    return saved['mapping'] if saved and hasattr(saved, 'keys') else saved[0] if saved else None


def stage_document(cur, *, account_code: str, source_name: str, filename: str, content: bytes,
                   import_group_id: UUID | None = None, document_type: str | None = None) -> dict:
    if cur.connection.autocommit:
        raise ValueError('document staging requires a transaction')
    if not filename or len(filename) > 180 or any(c in filename for c in ('/', '\\', '\x00', '\r', '\n')):
        raise ValueError('invalid filename')
    if not isinstance(content, bytes) or not 0 < len(content) <= 10 * 1024 * 1024:
        raise ValueError('empty or oversized document')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account_code,))
    is_statement = account_import_store.statement.is_statement(account_code)
    mapping = _saved_mapping(cur, account_code, 'cash') if is_statement else None
    suffix = PurePath(filename).suffix.lower().lstrip('.')
    screenshot = suffix in {'png', 'jpg', 'jpeg'}
    if document_type is not None:
        allowed = {'cash'} if is_statement else account_import_store.PROFILES.get(account_code, {}).get('kinds', ())
        if not screenshot or document_type not in allowed:
            raise ValueError('selected document type is not supported for this account or file')
        kind_result = {'kind': document_type}
    else:
        kind_result = document_kind.detect(content, filename, account_code, statement_mapping=mapping,
                                           include_statement=is_statement)
    kind = kind_result.get('kind')
    if kind not in {'cash','trade','holding'}:
        raise ValueError('document type could not be identified')
    if not is_statement:
        mapping = _saved_mapping(cur, account_code, kind)
    parsed = kind_result.get('parsed_statement')
    if parsed is None:
        parsed = account_import_store.parse(content, filename, account_code, kind, mapping=mapping)
    if parsed.get('document_kind') and parsed['document_kind'] != kind:
        raise ValueError('截图内容与所选资料类别不一致')
    rows = _rows(parsed, kind, screenshot=screenshot)
    if account_code == LICAITONG_CURRENT_PLUS and not rows:
        raise ValueError('未识别到活期+取出到账明细，请上传完整交易截图')
    if screenshot and kind == 'cash':
        for row in rows:
            fields = row['fields']
            if fields.get('action') == 'transfer' and fields.get('counterparty'):
                fields['counter_account_code'] = account_import_store.resolve_bank_destination(
                    cur, account_code, fields['counterparty'], fields.get('currency'))
    method = 'screenshot' if screenshot else 'file'
    parse_method = 'ocr' if method == 'screenshot' else 'template'
    cur.execute('''SELECT 1 FROM public.source_records WHERE account_code=%s AND document_type=%s
        AND sha256=%s AND status IN ('draft','published') LIMIT 1''', (account_code,kind,sha256(content).hexdigest()))
    if cur.fetchone():
        raise ValueError('same original document already exists for this account')
    source_id=uuid4()
    source_records_v2.create_source(cur,source_id=source_id,account_code=account_code,source_name=source_name,
        acquisition_method=method,document_type=kind,import_group_id=import_group_id,filename=filename,
        file_format=suffix,content=content)
    excluded = _save_rows(cur, source_id=source_id, kind=kind, parse_method=parse_method, rows=rows)
    return {'source_id':str(source_id),'document_type':kind,'template_status':'needs_review' if screenshot else 'matched',
            'row_count':len(rows),'excluded_count':excluded,'revision':len(rows)+1}


def mapping_metadata(cur, *, source_id: UUID) -> dict:
    """Read available file headers without modifying source evidence or reviews."""
    cur.execute("""SELECT account_code,document_type,filename,public.source_original_content(account_code,sha256,content) AS content,sha256,acquisition_method,status,revision
        FROM public.source_records WHERE source_id=%s""", (str(source_id),))
    source = cur.fetchone()
    if not source:
        raise ValueError('source missing')
    if source['acquisition_method'] != 'file' or source['content'] is None:
        raise ValueError('mapping is only available for file sources')
    content = bytes(source['content'])
    if sha256(content).hexdigest() != source['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute('SELECT mapping FROM public.source_templates WHERE account_code=%s AND document_type=%s',
                (source['account_code'], source['document_type']))
    row = cur.fetchone()
    mapping = row['mapping'] if row else {}
    parsed = account_import_store.parse(content, source['filename'], source['account_code'],
                                        source['document_type'], mapping=mapping)
    cur.execute("SELECT EXISTS (SELECT 1 FROM public.source_items WHERE source_id=%s AND review_status <> 'pending') AS reviewed",
                (str(source_id),))
    reviewed = cur.fetchone()['reviewed']
    return {'source_id': str(source_id), 'revision': source['revision'],
            'headers': parsed.get('headers') or [], 'suggested_mapping': parsed.get('suggested_mapping') or {},
            'mapping': mapping, 'warnings': parsed.get('warnings') or [],
            'editable': source['status'] == 'draft' and not reviewed}


def update_mapping(cur, *, source_id: UUID, revision: int, mapping: dict) -> dict:
    if cur.connection.autocommit:
        raise ValueError('mapping requires a transaction')
    if (not isinstance(mapping, dict) or len(mapping) > 40 or
        any(key not in account_import_store.FIELDS or not isinstance(value, str) or not value.strip() or len(value) > 200
            for key, value in mapping.items())):
        raise ValueError('invalid field mapping')
    cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s', (str(source_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('source missing')
    account = row['account_code'] if hasattr(row, 'keys') else row[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    cur.execute('''SELECT account_code,document_type,filename,public.source_original_content(account_code,sha256,content) AS content,sha256,acquisition_method FROM public.source_records
        WHERE source_id=%s AND status='draft' AND revision=%s FOR UPDATE''', (str(source_id), revision))
    source = cur.fetchone()
    if not source or source['account_code'] != account or source['acquisition_method'] != 'file':
        raise ValueError('source changed or mapping is unavailable')
    content = bytes(source['content'])
    if sha256(content).hexdigest() != source['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute("SELECT 1 FROM public.source_items WHERE source_id=%s AND review_status <> 'pending' LIMIT 1", (str(source_id),))
    if cur.fetchone():
        raise ValueError('reviewed items cannot be replaced by a mapping')
    rows = _rows(account_import_store.parse(content, source['filename'], account, source['document_type'], mapping=mapping),
                 source['document_type'])
    cur.execute('DELETE FROM public.source_items WHERE source_id=%s', (str(source_id),))
    excluded = _save_rows(cur, source_id=source_id, kind=source['document_type'], parse_method='template', rows=rows)
    cur.execute('''INSERT INTO public.source_templates(account_code,document_type,mapping) VALUES (%s,%s,%s)
        ON CONFLICT (account_code,document_type) DO UPDATE SET mapping=EXCLUDED.mapping,updated_at=now()''',
        (account, source['document_type'], Json(mapping)))
    cur.execute('SELECT revision FROM public.source_records WHERE source_id=%s', (str(source_id),))
    return {'source_id': str(source_id), 'revision': cur.fetchone()['revision'], 'row_count': len(rows), 'excluded_count': excluded}
