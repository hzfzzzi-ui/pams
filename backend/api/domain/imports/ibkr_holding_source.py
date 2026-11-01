"""Isolated, reviewable IBKR position evidence; never infer an absent exit."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from domain.imports import isolation
from domain.imports.unified_source import add_item, attach_file, create_batch
from domain.brokers.ibkr.ibkr_daily_close import _document

MAX_REPORT_BYTES = 32 * 1024 * 1024


def _day(value):
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except (TypeError, ValueError):
            pass
    return None


def _number(attrs, keys):
    values = [str(attrs[key]).strip() for key in keys if key in attrs and str(attrs[key]).strip()]
    if not values:
        return None
    try:
        numbers = [Decimal(value) for value in values]
    except InvalidOperation:
        return None
    if any(not value.is_finite() for value in numbers) or len(set(numbers)) != 1:
        return None
    return numbers[0]


def _extract(content: bytes, *, source_account_id: str, business_date: date, max_bytes: int = MAX_REPORT_BYTES):
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise ValueError('invalid IBKR report size')
    try:
        text = content.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('invalid IBKR report') from exc
    evidence, _, error = _document(text, source_account_id, sha256(content).hexdigest())
    if error:
        raise ValueError('invalid IBKR report: ' + error)
    statements = [node for node in evidence.statements if _day(node.get('toDate')) == business_date]
    if len(statements) != 1:
        raise ValueError('IBKR account and business date must identify one statement')
    statement = statements[0]
    parents = {child: parent for parent in statement.iter() for child in parent}
    rows = []
    for ordinal, node in enumerate((n for n in statement.iter() if n.tag.rsplit('}', 1)[-1] == 'OpenPosition'), 1):
        attrs = dict(node.attrib)
        conflict = False
        ancestor = node
        while ancestor is not None:
            values = {key.lower(): str(value).strip() for key, value in ancestor.attrib.items()}
            if (len(values) != len(ancestor.attrib) or values.get('model') or
                any(values.get(key) not in (None, '', source_account_id) for key in ('accountid', 'acctid'))):
                conflict = True
            ancestor = parents.get(ancestor)
        symbol = str(attrs.get('symbol') or '').strip()
        currency = str(attrs.get('currency') or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{3}', currency):
            currency = None
        position_day = _day(attrs.get('reportDate'))
        quantity = _number(attrs, ('position', 'quantity', 'qty'))
        cost = _number(attrs, ('costBasisMoney', 'costBasis', 'cost'))
        value = _number(attrs, ('marketValue', 'positionValue', 'value', 'markValue'))
        missing = [name for name, item in (('account', None if conflict else source_account_id),
                    ('symbol', symbol or None), ('currency', currency), ('business_date', position_day), ('quantity', quantity),
                    ('cost', cost), ('market_value', value)) if item is None]
        if position_day is not None and position_day != business_date:
            missing.append('business_date_conflict')
        state = 'unknown' if missing else ('explicit_zero' if quantity == cost == value == 0 else 'nonzero')
        rows.append({'ordinal': ordinal, 'raw_attributes': attrs, 'symbol': symbol or None, 'currency': currency,
                     'business_date': position_day.isoformat() if position_day else None,
                     'state': state, 'unknown_fields': missing})
    return text, dict(statement.attrib), rows


def _scope_is_well_formed(content: bytes, source_account_id: str, business_date: date) -> bool:
    try:
        evidence, _, error = _document(content.decode('utf-8'), source_account_id, sha256(content).hexdigest())
    except UnicodeError:
        return False
    if error:
        return False
    statements = [n for n in evidence.statements if _day(n.get('toDate')) == business_date]
    if len(statements) != 1:
        return False
    statement = statements[0]
    sections = [n for n in statement.iter() if n.tag.rsplit('}', 1)[-1] == 'OpenPositions']
    if len(sections) != 1:
        return False
    section = sections[0]
    attrs = {key.lower(): str(value).strip() for key, value in section.attrib.items()}
    if (len(attrs) != len(section.attrib) or attrs.get('model') or
        any(attrs.get(key) not in (None, '', source_account_id) for key in ('accountid', 'acctid')) or
        attrs.get('levelofdetail', 'DETAIL').upper() != 'DETAIL'):
        return False
    members = set(section.iter())
    return all(node in members for node in statement.iter() if node.tag.rsplit('}', 1)[-1] == 'OpenPosition')


def _read_source_report(cur, batch, scope, *, locking=True):
    """Read one frozen original without trusting an item-provided XML copy."""
    raw=scope['raw_fields']
    local=scope.get('file_id'); origin=scope.get('origin_file_id')
    if bool(local)==bool(origin):
        raise ValueError('source must reference exactly one original file')
    file_id=local or origin
    cur.execute("""SELECT f.*,b.status AS original_status,b.account_code AS original_account,
        b.workflow AS original_workflow,b.institution AS original_institution,b.date_to AS original_day
        FROM source_files f JOIN source_batches b ON b.batch_id=f.batch_id WHERE f.file_id=%s""" +
        (' FOR SHARE OF b,f' if locking else ''), (str(file_id),))
    file=cur.fetchone()
    if not file or file['original_account']!=batch['account_code'] or file['original_institution']!='ibkr':
        raise ValueError('original source account mismatch')
    if origin:
        if (file['original_status']!='published' or file['original_workflow'] not in {'ibkr_report','ibkr_cash_report','ibkr_holding_review'}
                or file['original_day']!=batch['date_to']):
            raise ValueError('original report is not a published same-account report')
        cur.execute('SELECT * FROM source_batches WHERE batch_id=%s' + (' FOR SHARE' if locking else ''),(file['batch_id'],))
        original=cur.fetchone()
        cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'" + (' FOR SHARE' if locking else ''),(file['batch_id'],))
        source_scope=cur.fetchone()
        from domain.imports.source_fact_evidence import _reviewed_snapshot
        original_raw=(source_scope.get('raw_fields') or {}) if source_scope else {}
        original_kind='report' if file['original_workflow'] in {'ibkr_report','ibkr_cash_report'} else 'scope'
        digest_key='sha256' if original_kind=='report' else 'source_sha256'
        if (not original or not source_scope or source_scope['review_status']!='confirmed'
            or str(source_scope.get('file_id'))!=str(file_id)
            or original_raw.get('kind')!=original_kind
            or original_raw.get('source_account_id')!=raw.get('source_account_id')
            or original_raw.get(digest_key)!=raw.get('source_sha256')
            or original_raw.get('business_date')!=batch['date_to'].isoformat()
            or not _reviewed_snapshot(cur,original,str(file['batch_id']),locking=locking,snapshot_cache={})):
            raise ValueError('original report evidence is unverified')
    elif str(file['batch_id'])!=str(batch['batch_id']):
        raise ValueError('local file is not in source batch')
    if file['file_format']!='xml' or file['content_type']!='application/xml':
        raise ValueError('original report format mismatch')
    content=bytes(file['content'])
    if sha256(content).hexdigest()!=raw.get('source_sha256') or str(file['sha256']).strip()!=raw.get('source_sha256'):
        raise ValueError('original report hash mismatch')
    return content


def stage_report(cur, *, batch_id, account_code: str, source_account_id: str,
                 business_date: date, content: bytes):
    """Stage evidence only; neither scope nor identity is approved."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('IBKR holding source requires isolated mode')
    if (cur.connection.autocommit or account_code!='ibkr' or not source_account_id or
        not isinstance(business_date,date) or isinstance(business_date,datetime)):
        raise ValueError('transaction, IBKR account and business date required')
    batch_id=UUID(str(batch_id))
    _,statement,rows=_extract(content,source_account_id=source_account_id,business_date=business_date)
    digest=sha256(content).hexdigest()
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    # A frozen report file is shared only when account, date, original bytes and
    # its source snapshot all verify. Reuse the earliest matching original.
    cur.execute("""SELECT f.file_id FROM source_files f JOIN source_batches b ON b.batch_id=f.batch_id
        JOIN source_items s ON s.batch_id=b.batch_id AND s.source_uid='scope'
        WHERE b.account_code=%s AND b.institution='ibkr' AND b.workflow IN ('ibkr_report','ibkr_cash_report','ibkr_holding_review') AND b.status='published'
          AND b.date_to=%s AND f.sha256=%s AND s.review_status='confirmed'
          AND s.raw_fields->>'source_account_id'=%s AND COALESCE(s.raw_fields->>'sha256',s.raw_fields->>'source_sha256')=%s
        ORDER BY f.created_at,f.file_id FOR SHARE OF b,f,s""",
        (account_code,business_date,digest,source_account_id,digest))
    candidates=cur.fetchall()
    origin=UUID(str(candidates[0]['file_id'])) if candidates else None
    create_batch(cur,batch_id=batch_id,account_code=account_code,business_type='holding',institution='ibkr',acquisition_method='api')
    cur.execute("UPDATE public.source_batches SET workflow='ibkr_holding_review',date_from=%s,date_to=%s WHERE batch_id=%s",
                (business_date,business_date,str(batch_id)))
    local=None
    if origin is None:
        local=uuid5(batch_id,'raw-xml')
        attach_file(cur,file_id=local,batch_id=batch_id,filename='ibkr-flex.xml',file_format='xml',content_type='application/xml',content=content)
    scope={'kind':'scope','source_sha256':digest,'statement_attributes':statement,
           'source_account_id':source_account_id,'business_date':business_date.isoformat()}
    def add(uid,raw):
        encoded=json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')
        key=uuid5(batch_id,uid)
        add_item(cur,item_id=key,batch_id=batch_id,source_uid=uid,parse_method='api',file_id=local,origin_file_id=origin,
                 record_hash=sha256(encoded).hexdigest(),raw_fields=raw,parsed_fields={})
        return str(key)
    scope_id=add('scope',scope)
    item_ids=[]
    for row in rows:
        raw={'kind':'position','source_sha256':digest,'source_account_id':source_account_id,**row}
        item_ids.append(add('position:'+str(row['ordinal']),raw))
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(str(batch_id),))
    batch=cur.fetchone()
    cur.execute('SELECT * FROM source_items WHERE source_item_id=%s',(scope_id,))
    item=cur.fetchone()
    if _read_source_report(cur,batch,item)!=content:
        raise ValueError('original report bytes mismatch')
    return {'batch_id':str(batch_id),'scope_item_id':scope_id,'position_item_ids':item_ids,
            'positions':rows,'scope_confirmed':False,'origin_reused':bool(origin)}

def retirement_status(cur, *, batch_id, item_id, account_code: str, business_date: date, read_only=False):
    """Return confirmed zero only with independently reviewed complete scope."""
    if read_only:
        cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
        mode=cur.fetchone()
        if not mode['readonly'] or mode['level'] not in {'repeatable read','serializable'}:
            raise ValueError('holding source reads require a read-only repeatable-read transaction')
    elif not isolation.source_enabled(cur):
        raise isolation.IsolationError('IBKR holding source requires isolated mode')
    suffix = '' if read_only else ' FOR SHARE'
    cur.execute('SELECT * FROM public.source_batches WHERE batch_id=%s' + suffix, (str(batch_id),))
    batch = cur.fetchone()
    if (not batch or batch['account_code'] != account_code or batch['business_type'] != 'holding' or
        batch['institution'] != 'ibkr' or batch['status'] != 'published' or batch['completeness'] != 'complete' or
        batch['date_from'] != business_date or batch['date_to'] != business_date):
        return {'state': 'unknown', 'reason': 'unverified_scope'}
    cur.execute("SELECT * FROM public.source_items WHERE batch_id=%s AND source_uid='scope'" + suffix, (str(batch_id),))
    scope = cur.fetchone()
    cur.execute('SELECT * FROM public.source_items WHERE batch_id=%s AND source_item_id=%s' + suffix, (str(batch_id), str(item_id)))
    item = cur.fetchone()
    if (not scope or not item or scope['review_status'] != 'confirmed' or item['review_status'] != 'confirmed' or
        scope.get('file_id') != item.get('file_id') or scope.get('origin_file_id') != item.get('origin_file_id') or
        not str(scope['review_note'] or '').strip() or not str(item['review_note'] or '').strip() or
        scope['parsed_fields'].get('coverage') != 'all_positions' or scope['parsed_fields'].get('confirmed_complete') is not True):
        return {'state': 'unknown', 'reason': 'scope_or_item_not_reviewed'}
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    if not _reviewed_snapshot(cur,batch,str(batch_id),locking=not read_only,snapshot_cache={}):
        return {'state': 'unknown', 'reason': 'published_snapshot_mismatch'}
    raw_scope, raw_item = scope['raw_fields'], item['raw_fields']
    try:
        for entry in (scope, item):
            encoded = json.dumps(entry['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if entry['record_hash'] != sha256(encoded).hexdigest():
                return {'state': 'unknown', 'reason': 'source_evidence_mismatch'}
        content = _read_source_report(cur,batch,scope,locking=not read_only)
        digest = sha256(content).hexdigest()
        _, attributes, positions = _extract(content, source_account_id=raw_scope['source_account_id'], business_date=business_date)
        if not _scope_is_well_formed(content, raw_scope['source_account_id'], business_date):
            return {'state': 'unknown', 'reason': 'invalid_position_scope'}
        ordinal = raw_item['ordinal']
        candidates = [row for row in positions if row['ordinal'] == ordinal]
    except (KeyError, TypeError, ValueError):
        return {'state': 'unknown', 'reason': 'invalid_original_evidence'}
    if (raw_scope.get('source_sha256') != digest or raw_item.get('source_sha256') != digest or
        raw_scope.get('statement_attributes') != attributes or raw_scope.get('business_date') != business_date.isoformat() or
        raw_item.get('source_account_id') != raw_scope.get('source_account_id') or len(candidates) != 1 or
        item['source_uid'] != 'position:' + str(ordinal) or
        raw_item != {'kind': 'position', 'source_sha256': digest, 'source_account_id': raw_scope['source_account_id'], **candidates[0]}):
        return {'state': 'unknown', 'reason': 'source_evidence_mismatch'}
    selected = candidates[0]
    if selected['state'] != 'explicit_zero':
        return {'state': 'unknown', 'reason': 'position_not_explicit_zero'}
    if sum((row['symbol'], row['currency']) == (selected['symbol'], selected['currency']) for row in positions) != 1:
        return {'state': 'unknown', 'reason': 'ambiguous_symbol'}
    conid = str(selected['raw_attributes'].get('conid') or '').strip()
    if conid and sum((str(row['raw_attributes'].get('conid') or '').strip(),row['currency']) ==
                     (conid,selected['currency']) for row in positions) != 1:
        return {'state': 'unknown', 'reason': 'ambiguous_contract'}
    return {'state': 'confirmed_zero', 'symbol': selected['symbol'], 'currency': selected['currency'],
            'conid': conid,
            'source_account_id': raw_scope['source_account_id'],
            'source_sha256': digest, 'source_item_id': str(item['source_item_id'])}
