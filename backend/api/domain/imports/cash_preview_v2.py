"""Build the same cash publication plan for preview and locked writes."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


def _record(cur, row):
    return dict(row) if isinstance(row, Mapping) else dict(zip((column[0] for column in cur.description), row))


def _fields(item):
    fields = item['parsed_fields']
    if fields.get('action') == 'balance' or (item['raw_fields'].get('source') or {}).get('balance_candidate') is True:
        raise ValueError('balance is not a cashflow')
    day = date.fromisoformat(fields['business_date'])
    posting = fields.get('posting_date')
    posting_day = date.fromisoformat(posting) if posting is not None else None
    amount = Decimal(str(fields['net_cash']))
    currency = fields['currency']
    if not amount.is_finite() or amount.as_tuple().exponent < -4 or not isinstance(currency, str) or not currency.strip():
        raise ValueError('invalid cashflow amount or currency')
    return day, posting_day, amount, currency


def _exists(cur, account, signature, *, official):
    if official:
        cur.execute('''SELECT 1 FROM public.pams_cashflow_fact f
            JOIN public.source_items i USING(source_item_id)
            WHERE f.account_code=%s AND i.source_uid=%s LIMIT 1''', (account, signature))
    else:
        cur.execute('''SELECT 1 FROM public.pams_cashflow_fact
            WHERE account_code=%s AND event_date=%s AND amount=%s AND currency=%s LIMIT 1''',
                    (account, *signature))
    return cur.fetchone() is not None


def plan_cash(cur, *, source_id, expected_revision: int, for_write=False, verified_ibkr_ids=False) -> dict:
    """Prepare normalized facts and review outcomes, locking before a publish recheck."""
    from domain.imports.source_records_v2 import _verify_deferred_source

    ident = str(source_id)
    locked_account = None
    if for_write:
        if cur.connection.autocommit:
            raise ValueError('source publication requires a transaction')
        cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s', (ident,))
        row = cur.fetchone()
        if not row:
            raise ValueError('source missing')
        locked_account = _record(cur, row)['account_code']
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + locked_account,))

    lock = ' FOR UPDATE' if for_write else ''
    cur.execute('''SELECT import_group_id,account_code,source_name,acquisition_method,document_type,
        filename,file_format,content_type,public.source_original_content(account_code,sha256,content) AS content,
        sha256,period_start,period_end,scope_note,status,revision
        FROM public.source_records WHERE source_id=%s''' + lock, (ident,))
    row = cur.fetchone()
    source = _record(cur, row) if row else None
    if (not source or source['status'] != 'draft' or source['document_type'] != 'cash'
            or source['revision'] != expected_revision or (for_write and source['account_code'] != locked_account)):
        raise ValueError('source changed or cannot publish cashflow')
    account = source['account_code']
    if verified_ibkr_ids and (account != 'ibkr' or source['source_name'] != 'ibkr_historical_flex'):
        raise ValueError('official cash identity requires an IBKR historical source')
    if source['content'] is not None and sha256(bytes(source['content'])).hexdigest() != source['sha256']:
        raise ValueError('source content hash mismatch')

    cur.execute('''SELECT source_item_id,source_uid,source_position,parse_method,raw_fields,parsed_fields,
        record_hash,review_status,review_note FROM public.source_items
        WHERE source_id=%s ORDER BY source_item_id''' + lock, (ident,))
    items = [_record(cur, row) for row in cur.fetchall()]
    signatures = Counter()
    candidates = {}
    statuses = {}
    for item in items:
        item_id = str(item['source_item_id'])
        raw = json.dumps(item['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
        if sha256(raw).hexdigest() != item['record_hash']:
            raise ValueError('source item hash mismatch')
        if item['review_status'] == 'excluded':
            statuses[item_id] = 'excluded'
            continue
        _verify_deferred_source(cur, source_id, item)
        try:
            day, posting, amount, currency = _fields(item)
            signature = (day, amount, currency)
            if verified_ibkr_ids:
                signature = item['source_uid']
                if not signature or not signature.startswith('ibkr_cash:'):
                    raise ValueError('missing official cash identity')
        except (KeyError, TypeError, ValueError, InvalidOperation):
            statuses[item_id] = 'invalid'
            continue
        signatures[signature] += 1
        statuses[item_id] = 'candidate' if item['review_status'] == 'confirmed' else 'pending'
        candidates[item_id] = (signature, (item, item['parsed_fields'], day, posting, amount, currency))

    ready = []
    for item_id, (signature, fact) in candidates.items():
        if statuses[item_id] != 'candidate':
            continue
        if signatures[signature] > 1 or _exists(cur, account, signature, official=verified_ibkr_ids):
            statuses[item_id] = 'duplicate'
        else:
            statuses[item_id] = 'ready'
            ready.append(fact)
    counts = Counter(statuses.values())
    unresolved = {item_id for item_id, status in statuses.items() if status in {'pending', 'duplicate', 'invalid'}}
    preview = {
        'source_id': ident, 'revision': expected_revision,
        'counts': {key: counts[key] for key in ('ready', 'pending', 'duplicate', 'invalid', 'excluded')},
        'rows': [{'source_item_id': item_id, 'status': status} for item_id, status in statuses.items()],
        'can_publish': bool(ready) and not unresolved,
        'can_publish_ready': bool(ready) and bool(unresolved),
    }
    return {'source': source, 'items': items, 'ready': ready, 'unresolved': unresolved, 'preview': preview}


def preview_cash(cur, *, source_id, expected_revision: int) -> dict:
    return plan_cash(cur, source_id=source_id, expected_revision=expected_revision)['preview']
