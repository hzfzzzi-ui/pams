"""Build the same cash publication plan for preview and locked writes."""
from __future__ import annotations

from collections import Counter, defaultdict
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


def _manual_bank_amount(cur, day, amount, currency, account_currency, action, rates):
    if (not amount or (action == 'expense' and amount > 0)
            or (action == 'income' and amount < 0) or action not in {'expense', 'income', 'transfer'}):
        raise ValueError('手工流水的收支方向与金额不一致')
    if currency != account_currency:
        raise ValueError('流水币种与子账户币种不一致')
    if currency == 'CNY':
        rate = Decimal(1)
    else:
        key = (day, currency)
        if key not in rates:
            cur.execute("""SELECT rate FROM public.pams_fx_rates_daily
                WHERE as_of=%s AND base_currency='CNY' AND quote_currency=%s
                  AND rate>0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(btrim(source),'') IS NOT NULL""", key)
            row = cur.fetchone()
            rates[key] = _record(cur, row)['rate'] if row else None
        rate = rates[key]
        if rate is None:
            raise ValueError('缺少交易日人民币汇率')
    return {'business_date': day.isoformat(), 'currency': currency, 'net_cash': str(amount),
            'amount_cny': str((amount * rate).quantize(Decimal('0.01'))), 'fx_rate_to_cny': str(rate)}


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


def _separate_statement_rows(source, items):
    if source['acquisition_method'] != 'file' or source['content'] is None:
        return False
    positions = set()
    for item in items:
        evidence = item['raw_fields'].get('source') or {}
        position = (item['source_position'] or {}).get('row')
        if (item['parse_method'] != 'template' or evidence.get('statement') is not True
                or type(position) is not int or position < 1 or position in positions):
            return False
        positions.add(position)
    return True


def plan_cash(cur, *, source_id, expected_revision: int, for_write=False, verified_ibkr_ids=False) -> dict:
    """Prepare normalized facts and review outcomes, locking before a publish recheck."""
    from domain.imports.source_records_v2 import _verify_deferred_source
    from domain.imports.bank_source_confirmation import SOURCE_ONLY_ACCOUNTS

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
    manual_bank = source['acquisition_method'] == 'manual' and account in SOURCE_ONLY_ACCOUNTS
    if manual_bank:
        cur.execute('SELECT base_currency FROM public.pams_accounts WHERE account_code=%s', (account,))
        account_currency = _record(cur, cur.fetchone())['base_currency']
    if verified_ibkr_ids and (account != 'ibkr' or source['source_name'] != 'ibkr_historical_flex'):
        raise ValueError('official cash identity requires an IBKR historical source')
    if source['content'] is not None and sha256(bytes(source['content'])).hexdigest() != source['sha256']:
        raise ValueError('source content hash mismatch')

    cur.execute('''SELECT source_item_id,source_uid,source_position,parse_method,raw_fields,parsed_fields,
        record_hash,review_status,review_note FROM public.source_items
        WHERE source_id=%s ORDER BY source_item_id''' + lock, (ident,))
    items = [_record(cur, row) for row in cur.fetchall()]
    groups = defaultdict(list)
    candidates = {}
    statuses = {}
    issues = {}
    cash_preview = {}
    rates = {}
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
            if manual_bank:
                try:
                    cash_preview[item_id] = _manual_bank_amount(
                        cur, day, amount, currency, account_currency, item['parsed_fields'].get('action'), rates)
                except ValueError as exc:
                    issues[item_id] = [{'code': 'manual_bank_cash', 'message': str(exc)}]
                    raise
            signature = (day, amount, currency)
            if verified_ibkr_ids:
                signature = item['source_uid']
                if not signature or not signature.startswith('ibkr_cash:'):
                    raise ValueError('missing official cash identity')
        except (KeyError, TypeError, ValueError, InvalidOperation):
            statuses[item_id] = 'invalid'
            continue
        groups[signature].append(item)
        statuses[item_id] = 'candidate' if item['review_status'] == 'confirmed' else 'pending'
        candidates[item_id] = (signature, (item, item['parsed_fields'], day, posting, amount, currency))

    reviewable = {signature for signature, group in groups.items()
                  if not verified_ibkr_ids and len(group) > 1 and _separate_statement_rows(source, group)
                  and not _exists(cur, account, signature, official=False)}
    distinct = {signature for signature in reviewable if all(
        item['review_status'] == 'confirmed' and item['parsed_fields'].get('duplicate_decision') == 'distinct'
        for item in groups[signature])}
    ready = []
    for item_id, (signature, fact) in candidates.items():
        if signature in reviewable and signature not in distinct:
            issues[item_id] = [{'code': 'possible_duplicate',
                                'message': '同一账单有同日同额记录，请对照原件逐笔核对。'}]
        if statuses[item_id] != 'candidate':
            continue
        if ((len(groups[signature]) > 1 and signature not in distinct)
                or _exists(cur, account, signature, official=verified_ibkr_ids)):
            statuses[item_id] = 'duplicate'
        else:
            statuses[item_id] = 'ready'
            ready.append(fact)
    counts = Counter(statuses.values())
    unresolved = {item_id for item_id, status in statuses.items() if status in {'pending', 'duplicate', 'invalid'}}
    preview = {
        'source_id': ident, 'revision': expected_revision,
        'counts': {key: counts[key] for key in ('ready', 'pending', 'duplicate', 'invalid', 'excluded')},
        'rows': [{'source_item_id': item_id, 'status': status,
                  **({'cash_preview': cash_preview[item_id]} if item_id in cash_preview else {}),
                  **({'issues': issues[item_id]} if item_id in issues else {})} for item_id, status in statuses.items()],
        'can_publish': bool(ready) and not unresolved,
        'can_publish_ready': bool(ready) and bool(unresolved),
    }
    return {'source': source, 'items': items, 'ready': ready, 'unresolved': unresolved, 'preview': preview}


def preview_cash(cur, *, source_id, expected_revision: int) -> dict:
    return plan_cash(cur, source_id=source_id, expected_revision=expected_revision)['preview']
