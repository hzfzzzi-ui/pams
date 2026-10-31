"""Isolated, reviewable IBKR position evidence; never infer an absent exit."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from domain.imports import isolation
from domain.imports.unified_source import add_item, create_batch
from domain.brokers.ibkr.ibkr_daily_close import _document

MAX_REPORT_BYTES = 10 * 1024 * 1024


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


def _extract(content: bytes, *, source_account_id: str, business_date: date):
    if not isinstance(content, bytes) or not content or len(content) > MAX_REPORT_BYTES:
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


def stage_report(cur, *, batch_id, account_code: str, source_account_id: str,
                 business_date: date, content: bytes):
    """Keep the report and every raw position; staged candidates are not approvals."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('IBKR holding source candidate requires isolated mode')
    if (cur.connection.autocommit or not account_code or not source_account_id or
        not isinstance(business_date, date) or isinstance(business_date, datetime)):
        raise ValueError('transaction, account and business date required')
    batch_id = UUID(str(batch_id))
    report, statement, rows = _extract(content, source_account_id=source_account_id, business_date=business_date)
    digest = sha256(content).hexdigest()
    create_batch(cur, batch_id=batch_id, account_code=account_code, business_type='holding',
                 institution='ibkr', acquisition_method='api')
    cur.execute('UPDATE public.source_batches SET date_from=%s,date_to=%s WHERE batch_id=%s',
                (business_date, business_date, str(batch_id)))
    scope = {'kind': 'scope', 'source_sha256': digest, 'raw_xml': report,
             'statement_attributes': statement, 'source_account_id': source_account_id,
             'business_date': business_date.isoformat()}
    def add(uid, raw):
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        key = uuid5(batch_id, uid)
        add_item(cur, item_id=key, batch_id=batch_id, source_uid=uid, parse_method='api',
                 record_hash=sha256(encoded).hexdigest(), raw_fields=raw, parsed_fields={})
        return str(key)
    scope_id = add('scope', scope)
    item_ids = []
    for row in rows:
        raw = {'kind': 'position', 'source_sha256': digest, 'source_account_id': source_account_id, **row}
        item_ids.append(add('position:' + str(row['ordinal']), raw))
    return {'batch_id': str(batch_id), 'scope_item_id': scope_id, 'position_item_ids': item_ids,
            'positions': rows, 'scope_confirmed': False}


def retirement_status(cur, *, batch_id, item_id, account_code: str, business_date: date):
    """Return confirmed zero only with independently reviewed complete scope."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('IBKR holding source candidate requires isolated mode')
    cur.execute('SELECT * FROM public.source_batches WHERE batch_id=%s FOR SHARE', (str(batch_id),))
    batch = cur.fetchone()
    if (not batch or batch['account_code'] != account_code or batch['business_type'] != 'holding' or
        batch['institution'] != 'ibkr' or batch['status'] != 'published' or batch['completeness'] != 'complete' or
        batch['date_from'] != business_date or batch['date_to'] != business_date):
        return {'state': 'unknown', 'reason': 'unverified_scope'}
    cur.execute("SELECT * FROM public.source_items WHERE batch_id=%s AND source_uid='scope' FOR SHARE", (str(batch_id),))
    scope = cur.fetchone()
    cur.execute('SELECT * FROM public.source_items WHERE batch_id=%s AND source_item_id=%s FOR SHARE', (str(batch_id), str(item_id)))
    item = cur.fetchone()
    if (not scope or not item or scope['review_status'] != 'confirmed' or item['review_status'] != 'confirmed' or
        not str(scope['review_note'] or '').strip() or
        scope['parsed_fields'].get('coverage') != 'all_positions' or scope['parsed_fields'].get('confirmed_complete') is not True):
        return {'state': 'unknown', 'reason': 'scope_or_item_not_reviewed'}
    raw_scope, raw_item = scope['raw_fields'], item['raw_fields']
    try:
        for entry in (scope, item):
            encoded = json.dumps(entry['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if entry['record_hash'] != sha256(encoded).hexdigest():
                return {'state': 'unknown', 'reason': 'source_evidence_mismatch'}
        content = raw_scope['raw_xml'].encode('utf-8')
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
    return {'state': 'confirmed_zero', 'symbol': selected['symbol'], 'currency': selected['currency'],
            'source_sha256': digest, 'source_item_id': str(item['source_item_id'])}
