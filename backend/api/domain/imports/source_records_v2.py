"""Persistence primitives for redesigned source records; caller owns transaction."""
from __future__ import annotations

from hashlib import sha256
from collections.abc import Mapping
import json
from uuid import UUID, uuid4
from psycopg2.extras import Json

class SourceRevisionConflict(ValueError):
    """The source draft changed before a versioned update."""


_IMMUTABLE_REPORTED_FIELDS = frozenset({
    'business_date', 'trade_time', 'settle_date', 'posting_date', 'action', 'trade_type', 'side',
    'symbol', 'name', 'market', 'currency', 'quantity', 'price', 'gross_amount', 'net_cash',
    'fee_amount', 'fee_currency', 'fee_scope', 'tax_amount', 'counterparty', 'summary', 'description',
    'broker', 'execution_status', 'source_ref', 'kind',
})


def create_source(cur, *, source_id: UUID, account_code: str, source_name: str,
                  acquisition_method: str, document_type: str, import_group_id: UUID | None = None,
                  filename: str | None = None, file_format: str | None = None,
                  content_type: str | None = None, content: bytes | None = None) -> None:
    if cur.connection.autocommit:
        raise ValueError('source creation requires a transaction')
    max_bytes = (32 if file_format == 'xml' else 10) * 1024 * 1024
    if content is not None and (not isinstance(content, bytes) or not 0 < len(content) <= max_bytes):
        raise ValueError('invalid source content')
    digest = sha256(content).hexdigest() if content is not None else None
    cur.execute('''INSERT INTO public.source_records(source_id,import_group_id,account_code,source_name,
        acquisition_method,document_type,filename,file_format,content_type,content,sha256)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (str(source_id),str(import_group_id) if import_group_id else None,account_code,source_name,
         acquisition_method,document_type,filename,file_format,content_type,content,digest))


def add_item(cur, *, source_id: UUID, item_id: UUID, parse_method: str, raw_fields: dict,
             parsed_fields: dict, source_uid: str | None = None, source_position: dict | None = None) -> None:
    if cur.connection.autocommit:
        raise ValueError('source item creation requires a transaction')
    if not isinstance(raw_fields, dict) or not isinstance(parsed_fields, dict):
        raise ValueError('source fields must be objects')
    raw = json.dumps(raw_fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    cur.execute('SELECT status FROM public.source_records WHERE source_id=%s FOR UPDATE', (str(source_id),))
    row = cur.fetchone()
    if not row or (row['status'] if isinstance(row, Mapping) else row[0]) != 'draft':
        raise ValueError('source is missing or not editable')
    cur.execute('''INSERT INTO public.source_items(source_item_id,source_id,source_uid,source_position,
        parse_method,raw_fields,parsed_fields,record_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
        (str(item_id),str(source_id),source_uid,Json(source_position) if source_position is not None else None,
         parse_method,Json(raw_fields),Json(parsed_fields),sha256(raw).hexdigest()))
    cur.execute('UPDATE public.source_records SET revision=revision+1,updated_at=now() WHERE source_id=%s', (str(source_id),))


def add_manual_item(cur, *, source_id: UUID, item_id: UUID, expected_revision: int, parsed_fields: dict) -> int:
    """Add one manual row to an unchanged source draft."""
    if cur.connection.autocommit or not isinstance(parsed_fields, dict):
        raise ValueError('manual item requires a transaction and object fields')
    cur.execute("""SELECT revision FROM public.source_records WHERE source_id=%s AND status='draft'
        AND revision=%s FOR UPDATE""", (str(source_id), expected_revision))
    if not cur.fetchone():
        raise SourceRevisionConflict('source missing, stale or not a draft')
    cur.execute('SELECT COUNT(*) AS count FROM public.source_items WHERE source_id=%s', (str(source_id),))
    row = cur.fetchone()
    count = row['count'] if isinstance(row, Mapping) else row[0]
    if count >= 2000:
        raise ValueError('source item limit reached')
    add_item(cur, source_id=source_id, item_id=item_id, parse_method='manual',
             raw_fields={'entered_fields': parsed_fields}, parsed_fields=parsed_fields)
    return expected_revision + 1


def review_item(cur, *, source_id: UUID, item_id: UUID, expected_revision: int,
                parsed_fields: dict, review_status: str, note: str | None = None) -> int:
    if cur.connection.autocommit:
        raise ValueError('source review requires a transaction')
    if review_status not in {'pending','confirmed','excluded'} or not isinstance(parsed_fields, dict):
        raise ValueError('invalid source review')
    cur.execute('''SELECT revision FROM public.source_records WHERE source_id=%s AND status='draft'
        AND revision=%s FOR UPDATE''', (str(source_id), expected_revision))
    if not cur.fetchone():
        raise SourceRevisionConflict('source missing, stale or not editable')
    cur.execute('''SELECT parse_method,parsed_fields FROM public.source_items
        WHERE source_item_id=%s AND source_id=%s FOR UPDATE''', (str(item_id), str(source_id)))
    existing = cur.fetchone()
    if not existing:
        raise ValueError('source item missing')
    method = existing['parse_method'] if isinstance(existing, Mapping) else existing[0]
    fields = existing['parsed_fields'] if isinstance(existing, Mapping) else existing[1]
    if method in {'template', 'api'} and any(fields.get(key) != parsed_fields.get(key) for key in _IMMUTABLE_REPORTED_FIELDS):
        raise ValueError('original file or API business fields cannot be edited')
    cur.execute('''UPDATE public.source_items SET parsed_fields=%s,review_status=%s,review_note=%s,updated_at=now()
        WHERE source_item_id=%s AND source_id=%s''', (Json(parsed_fields),review_status,note,str(item_id),str(source_id)))
    if cur.rowcount != 1:
        raise ValueError('source item missing')
    cur.execute('UPDATE public.source_records SET revision=revision+1,updated_at=now() WHERE source_id=%s RETURNING revision', (str(source_id),))
    row=cur.fetchone()
    return int(row['revision'] if isinstance(row, Mapping) else row[0])


def review_items(cur, *, source_id: UUID, expected_revision: int, items: list[dict]) -> int:
    """Review multiple items in one transaction, rejecting duplicate identities."""
    if cur.connection.autocommit or not items or len(items) > 2000:
        raise ValueError('batch review requires a transaction and valid items')
    identifiers = [str(item['source_item_id']) for item in items if item.get('source_item_id') is not None]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('duplicate source item')
    revision = expected_revision
    for item in items:
        item_id = item.get('source_item_id')
        if item_id is None:
            item_id = uuid4()
            revision = add_manual_item(cur, source_id=source_id, item_id=item_id,
                                       expected_revision=revision, parsed_fields=item['parsed_fields'])
        revision = review_item(cur, source_id=source_id, item_id=item_id,
            expected_revision=revision, parsed_fields=item['parsed_fields'], review_status=item['review_status'],
            note=item.get('note'))
    return revision


def delete_draft(cur, *, source_id: UUID, expected_revision: int) -> None:
    """Delete only an unchanged draft and its unposted source items."""
    if cur.connection.autocommit:
        raise ValueError('draft deletion requires a transaction')
    cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s', (str(source_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('source missing')
    account = row['account_code'] if isinstance(row, Mapping) else row[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    cur.execute("SELECT 1 FROM public.source_records WHERE source_id=%s AND status='draft' AND revision=%s FOR UPDATE",
                (str(source_id), expected_revision))
    if not cur.fetchone():
        raise ValueError('source missing, stale or not a draft')
    cur.execute('DELETE FROM public.source_items WHERE source_id=%s', (str(source_id),))
    cur.execute("DELETE FROM public.source_records WHERE source_id=%s AND status='draft'", (str(source_id),))
    if cur.rowcount != 1:
        raise ValueError('draft changed')


def _verify_deferred_source(cur, source_id, item):
    """Verify a deferred item still traces to the excluded original and same bytes."""
    position=item.get('source_position') or {}
    origin=position.get('deferred_from') if isinstance(position,dict) else None
    if not origin:
        return
    try:
        parent_id=str(UUID(origin['source_id'])); original_id=str(UUID(origin['source_item_id']))
    except (KeyError,TypeError,ValueError,AttributeError) as exc:
        raise ValueError('invalid deferred source identity') from exc
    cur.execute("""SELECT i.raw_fields,i.record_hash,i.source_uid,i.parse_method,i.source_position,i.review_status,
        parent.status,parent.account_code AS parent_account,child.account_code AS child_account,
        parent.sha256 AS parent_hash,child.sha256 AS child_hash,parent.content AS parent_content,child.content AS child_content
        FROM public.source_items i JOIN public.source_records parent ON parent.source_id=i.source_id
        JOIN public.source_records child ON child.source_id=%s
        WHERE i.source_item_id=%s AND parent.source_id=%s FOR SHARE OF i,parent,child""",
        (str(source_id),original_id,parent_id))
    cols=('raw_fields','record_hash','source_uid','parse_method','source_position','review_status',
          'status','parent_account','child_account','parent_hash','child_hash','parent_content','child_content')
    row=cur.fetchone(); original=dict(row) if isinstance(row,Mapping) else dict(zip(cols,row)) if row else None
    if not original or parent_id==str(source_id) or original['status']!='published' or original['review_status']!='excluded':
        raise ValueError('deferred original source is unavailable')
    if (original['parent_account']!=original['child_account'] or original['parent_hash']!=original['child_hash'] or
        original['parent_content']!=original['child_content'] or original['raw_fields']!=item['raw_fields'] or
        original['record_hash']!=item['record_hash'] or original['source_uid']!=item['source_uid'] or
        original['parse_method']!=item['parse_method'] or position.get('original_position')!=original['source_position']):
        raise ValueError('deferred original evidence differs')


def publish_cash_source(cur, *, source_id: UUID, expected_revision: int) -> dict:
    """Atomically publish reviewed cash facts, without making classification decisions."""
    from datetime import date
    from decimal import Decimal, InvalidOperation

    if cur.connection.autocommit:
        raise ValueError('source publication requires a transaction')
    cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s', (str(source_id),))
    account_row = cur.fetchone()
    if not account_row:
        raise ValueError('source missing')
    account = account_row['account_code'] if isinstance(account_row, Mapping) else account_row[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    cur.execute('''SELECT account_code,document_type,status,revision,content,sha256
        FROM public.source_records WHERE source_id=%s FOR UPDATE''', (str(source_id),))
    columns=('account_code','document_type','status','revision','content','sha256')
    row=cur.fetchone()
    if not row:
        raise ValueError('source missing')
    source=dict(row) if isinstance(row, Mapping) else dict(zip(columns,row))
    if source['account_code'] != account or source['revision'] != expected_revision or source['status'] != 'draft' or source['document_type'] != 'cash':
        raise ValueError('source changed or cannot publish cashflow')
    content=source['content']
    if content is not None and sha256(bytes(content)).hexdigest() != source['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute('''SELECT source_item_id,source_uid,source_position,parse_method,raw_fields,parsed_fields,record_hash,review_status
        FROM public.source_items WHERE source_id=%s ORDER BY source_item_id FOR UPDATE''', (str(source_id),))
    columns=('source_item_id','source_uid','source_position','parse_method','raw_fields','parsed_fields','record_hash','review_status')
    items=[dict(row) if isinstance(row,Mapping) else dict(zip(columns,row)) for row in cur.fetchall()]
    if not items or any(item['review_status'] == 'pending' for item in items):
        raise ValueError('source contains unreviewed items')
    prepared=[]; seen=set()
    for item in items:
        raw=json.dumps(item['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')
        if sha256(raw).hexdigest() != item['record_hash']:
            raise ValueError('source item hash mismatch')
        if item['review_status']=='excluded':
            continue
        _verify_deferred_source(cur,source_id,item)
        fields=item['parsed_fields']
        try:
            if (fields.get('action') == 'balance' or
                    (item['raw_fields'].get('source') or {}).get('balance_candidate') is True):
                raise ValueError('balance is not a cashflow')
            day=date.fromisoformat(fields['business_date'])
            posting=date.fromisoformat(fields['posting_date']) if fields.get('posting_date') else None
            amount=Decimal(str(fields['net_cash']))
            currency=fields['currency']
            if not amount.is_finite() or amount.as_tuple().exponent < -4 or not isinstance(currency,str) or not currency.strip():
                raise ValueError('invalid cashflow amount or currency')
        except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
            raise ValueError('cashflow required fields invalid') from exc
        signature=(day,amount,currency)
        if signature in seen:
            raise ValueError('potential duplicate cashflow requires review')
        seen.add(signature)
        cur.execute('''SELECT 1 FROM public.pams_cashflow_fact WHERE account_code=%s AND event_date=%s
            AND amount=%s AND currency=%s LIMIT 1''', (source['account_code'],day,amount,currency))
        if cur.fetchone():
            raise ValueError('potential duplicate cashflow requires review')
        prepared.append((item,fields,day,posting,amount,currency))
    if not prepared:
        raise ValueError('source has no confirmed cashflows')
    for item,fields,day,posting,amount,currency in prepared:
        cur.execute('''INSERT INTO public.pams_cashflow_fact(account_code,event_date,posting_date,amount,currency,
            summary,description,counterparty,source_item_id,fact_role)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'statement_cashflow') RETURNING cashflow_id''',
            (source['account_code'],day,posting,amount,currency,fields.get('summary'),fields.get('description'),
             fields.get('counterparty'),str(item['source_item_id'])))
        row=cur.fetchone(); ident=row['cashflow_id'] if isinstance(row,Mapping) else row[0]
        cur.execute('INSERT INTO public.cashflow_classification(cashflow_id,status) VALUES (%s,\'pending\')',(ident,))
    cur.execute("UPDATE public.source_records SET status='published',revision=revision+1,updated_at=now() WHERE source_id=%s",(str(source_id),))
    return {'published':len(prepared),'classification':'pending'}


def publish_trade_source(cur, *, source_id: UUID, expected_revision: int, preview_only: bool = False) -> dict:
    """Publish reviewed original trade facts; preserve unreported amounts as NULL."""
    from datetime import date, datetime, time
    from decimal import Decimal, InvalidOperation

    if cur.connection.autocommit:
        raise ValueError('source publication requires a transaction')
    cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s', (str(source_id),))
    account_row=cur.fetchone()
    if not account_row:
        raise ValueError('source missing')
    account=account_row['account_code'] if isinstance(account_row, Mapping) else account_row[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    cur.execute('''SELECT account_code,document_type,status,revision,content,sha256 FROM public.source_records
        WHERE source_id=%s FOR UPDATE''',(str(source_id),))
    cols=('account_code','document_type','status','revision','content','sha256'); row=cur.fetchone()
    src=dict(row) if isinstance(row,Mapping) else dict(zip(cols,row)) if row else None
    if not src or src['account_code']!=account or src['document_type']!='trade' or src['status']!='draft' or src['revision']!=expected_revision:
        raise ValueError('source changed or cannot publish trade')
    if src['content'] is not None and sha256(bytes(src['content'])).hexdigest()!=src['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute('''SELECT source_item_id,source_uid,raw_fields,parsed_fields,record_hash,review_status
        FROM public.source_items WHERE source_id=%s ORDER BY source_item_id FOR UPDATE''',(str(source_id),))
    cols=('source_item_id','source_uid','raw_fields','parsed_fields','record_hash','review_status')
    items=[dict(row) if isinstance(row,Mapping) else dict(zip(cols,row)) for row in cur.fetchall()]
    if not items or any(item['review_status']=='pending' for item in items):
        raise ValueError('source contains unreviewed items')
    nums=('quantity','price','gross_amount','fee_amount','tax_amount','net_cash')
    allowed={'buy','sell','deposit','withdraw','fund_subscription','fund_redemption','dividend','interest','coupon','fee','tax','fx_conversion','corporate_action','split','transfer_in_kind','transfer_out_kind','position_adjustment','unknown'}
    prepared=[]; seen=set()
    for item in items:
        digest=sha256(json.dumps(item['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')).hexdigest()
        if digest!=item['record_hash']: raise ValueError('source item hash mismatch')
        if item['review_status']=='excluded': continue
        f=item['parsed_fields']; raw_type=f.get('trade_type') or f.get('action'); typ={'subscribe':'fund_subscription','redeem':'fund_redemption'}.get(raw_type,raw_type); currency=f.get('currency')
        try:
            day=date.fromisoformat(f['business_date']); settle=date.fromisoformat(f['settle_date']) if f.get('settle_date') else None
            stamp=datetime.combine(day,time.fromisoformat(f['trade_time'])) if f.get('trade_time') else None
            amounts={key:Decimal(str(f[key])) if f.get(key) not in (None,'') else None for key in nums}
            if typ not in allowed or not isinstance(currency,str) or not currency.strip(): raise ValueError('invalid trade type or currency')
            if typ in {'buy','sell'} and (not f.get('symbol') and not f.get('asset_id') or amounts['quantity'] is None or amounts['quantity'] <= 0):
                raise ValueError('trade asset or quantity missing')
            if typ in {'buy','deposit'} and amounts['net_cash'] is not None and (amounts['net_cash'] > 0 if typ == 'buy' else amounts['net_cash'] <= 0):
                raise ValueError('trade cash direction conflicts')
            if typ in {'sell','withdraw'} and amounts['net_cash'] is not None and (amounts['net_cash'] < 0 if typ == 'sell' else amounts['net_cash'] >= 0):
                raise ValueError('trade cash direction conflicts')
            if stamp is not None and stamp.tzinfo is not None: raise ValueError('trade time must be local')
            if any(not value.is_finite() or value.as_tuple().exponent < -10 for value in amounts.values() if value is not None): raise ValueError('invalid trade amount')
        except (KeyError,TypeError,ValueError,InvalidOperation) as exc: raise ValueError('trade required fields invalid') from exc
        uid=item['source_uid']
        if uid:
            if uid in seen: raise ValueError('duplicate source trade identifier')
            seen.add(uid)
            cur.execute('''SELECT 1 FROM public.pams_investment_trade_fact f JOIN public.source_items i ON i.source_item_id=f.source_item_id
                JOIN public.source_records r ON r.source_id=i.source_id WHERE f.account_code=%s AND i.source_uid=%s LIMIT 1''',(account,uid))
            if cur.fetchone(): raise ValueError('existing source trade identifier requires review')
        else:
            signature=(day,typ,f.get('symbol'),amounts['quantity'],amounts['net_cash'],currency)
            if signature in seen: raise ValueError('potential duplicate trade requires review')
            seen.add(signature)
            cur.execute('''SELECT 1 FROM public.pams_investment_trade_fact WHERE account_code=%s AND trade_date=%s AND trade_type=%s
                AND symbol IS NOT DISTINCT FROM %s AND quantity IS NOT DISTINCT FROM %s AND net_cash IS NOT DISTINCT FROM %s AND currency=%s LIMIT 1''',
                (account,day,typ,f.get('symbol'),amounts['quantity'],amounts['net_cash'],currency))
            if cur.fetchone(): raise ValueError('potential duplicate trade requires review')
        prepared.append((item,f,day,stamp,settle,typ,currency,amounts))
    if not prepared: raise ValueError('source has no confirmed trades')
    if preview_only:
        return {'source_id': str(source_id), 'revision': expected_revision,
                'items': [{'source_item_id': str(item['source_item_id']), 'trade_date': day.isoformat(),
                           'trade_time': stamp.isoformat() if stamp else None,
                           'settle_date': settle.isoformat() if settle else None,
                           'trade_type': typ, 'symbol': f.get('symbol'), 'name': f.get('name'),
                           'currency': currency, 'fee_currency': f.get('fee_currency'),
                           **{key: str(value) if value is not None else None for key, value in amounts.items()}}
                          for item, f, day, stamp, settle, typ, currency, amounts in prepared]}
    for item,f,day,stamp,settle,typ,currency,amounts in prepared:
        cur.execute('''INSERT INTO public.pams_investment_trade_fact(account_code,broker,portfolio_code,trade_date,trade_time,settle_date,
            asset_id,symbol,name,market,currency,trade_type,side,quantity,price,gross_amount,fee_amount,fee_currency,tax_amount,net_cash,
            execution_status,notes,source_item_id,fact_role) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'execution')''',
            (account,f.get('broker'),f.get('portfolio_code'),day,stamp,settle,f.get('asset_id'),f.get('symbol'),f.get('name'),f.get('market'),currency,typ,f.get('side') or (typ if typ in {'buy','sell','deposit','withdraw'} else None),
             amounts['quantity'],amounts['price'],amounts['gross_amount'],amounts['fee_amount'],f.get('fee_currency'),amounts['tax_amount'],amounts['net_cash'],
             f.get('execution_status'),f.get('notes'),str(item['source_item_id'])))
    cur.execute("UPDATE public.source_records SET status='published',revision=revision+1,updated_at=now() WHERE source_id=%s",(str(source_id),))
    return {'published':len(prepared)}


def publish_ready_cash_source(cur, *, source_id: UUID, expected_revision: int) -> dict:
    """Publish unambiguous reviewed cashflows and defer other evidence atomically."""
    from datetime import date
    from decimal import Decimal, InvalidOperation
    from uuid import uuid4

    if cur.connection.autocommit:
        raise ValueError('partial publication requires a transaction')
    cur.execute('SELECT account_code FROM public.source_records WHERE source_id=%s',(str(source_id),))
    row=cur.fetchone()
    if not row: raise ValueError('source missing')
    account=row['account_code'] if isinstance(row,Mapping) else row[0]
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:'+account,))
    cur.execute('''SELECT source_id,import_group_id,account_code,source_name,acquisition_method,document_type,
        filename,file_format,content_type,content,sha256,period_start,period_end,scope_note,status,revision
        FROM public.source_records WHERE source_id=%s FOR UPDATE''',(str(source_id),))
    cols=('source_id','import_group_id','account_code','source_name','acquisition_method','document_type',
          'filename','file_format','content_type','content','sha256','period_start','period_end','scope_note','status','revision')
    row=cur.fetchone();src=dict(row) if isinstance(row,Mapping) else dict(zip(cols,row)) if row else None
    if not src or src['account_code']!=account or src['document_type']!='cash' or src['status']!='draft' or src['revision']!=expected_revision:
        raise ValueError('source changed or cannot partially publish')
    if src['content'] is not None and sha256(bytes(src['content'])).hexdigest()!=src['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute('''SELECT source_item_id,source_uid,source_position,parse_method,raw_fields,parsed_fields,record_hash,review_status,review_note
        FROM public.source_items WHERE source_id=%s ORDER BY source_item_id FOR UPDATE''',(str(source_id),))
    cols=('source_item_id','source_uid','source_position','parse_method','raw_fields','parsed_fields','record_hash','review_status','review_note')
    items=[dict(row) if isinstance(row,Mapping) else dict(zip(cols,row)) for row in cur.fetchall()]
    signatures={}; unresolved=set(); checked=[]
    for item in items:
        raw=json.dumps(item['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')
        if sha256(raw).hexdigest()!=item['record_hash']: raise ValueError('source item hash mismatch')
        if item['review_status']=='excluded': continue
        _verify_deferred_source(cur,source_id,item)
        if item['review_status']!='confirmed': unresolved.add(str(item['source_item_id']))
        f=item['parsed_fields']
        try:
            if f.get('action') == 'balance' or (item['raw_fields'].get('source') or {}).get('balance_candidate') is True:
                raise ValueError('balance is not a cashflow')
            day=date.fromisoformat(f['business_date']); amount=Decimal(str(f['net_cash'])); currency=f['currency']
            if not amount.is_finite() or amount.as_tuple().exponent < -4 or not isinstance(currency,str) or not currency.strip(): raise ValueError('invalid fields')
            if f.get('posting_date'): date.fromisoformat(f['posting_date'])
        except (KeyError,TypeError,ValueError,InvalidOperation):
            unresolved.add(str(item['source_item_id']));continue
        sig=(day,amount,currency);signatures.setdefault(sig,[]).append(item)
        if item['review_status']=='confirmed': checked.append((item,sig))
    for item,sig in checked:
        if len(signatures[sig])>1:
            unresolved.update(str(x['source_item_id']) for x in signatures[sig]);continue
        cur.execute('''SELECT 1 FROM public.pams_cashflow_fact WHERE account_code=%s AND event_date=%s
            AND amount=%s AND currency=%s LIMIT 1''',(account,*sig))
        if cur.fetchone(): unresolved.add(str(item['source_item_id']))
    ready=[item for item,_ in checked if str(item['source_item_id']) not in unresolved]
    if not ready: raise ValueError('no unambiguous confirmed cashflows')
    if not unresolved: return publish_cash_source(cur,source_id=source_id,expected_revision=expected_revision)
    deferred_id=uuid4()
    cur.execute('''INSERT INTO public.source_records(source_id,import_group_id,account_code,source_name,acquisition_method,document_type,
        filename,file_format,content_type,content,sha256,period_start,period_end,scope_note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (str(deferred_id),str(src['import_group_id']) if src['import_group_id'] else None,account,src['source_name'],src['acquisition_method'],src['document_type'],
         src['filename'],src['file_format'],src['content_type'],bytes(src['content']) if src['content'] is not None else None,src['sha256'],src['period_start'],src['period_end'],src['scope_note']))
    for item in items:
        old_id=str(item['source_item_id'])
        if old_id not in unresolved: continue
        position={'original_position':item['source_position'],'deferred_from':{'source_id':str(source_id),'source_item_id':old_id}}
        cur.execute('''INSERT INTO public.source_items(source_item_id,source_id,source_uid,source_position,parse_method,
            raw_fields,parsed_fields,record_hash,review_status,review_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)''',
            (str(uuid4()),str(deferred_id),item['source_uid'],Json(position),item['parse_method'],Json(item['raw_fields']),
             Json(item['parsed_fields']),item['record_hash'],item['review_note']))
        cur.execute("UPDATE public.source_items SET review_status='excluded',updated_at=now() WHERE source_id=%s AND source_item_id=%s",
                    (str(source_id),old_id))
        if cur.rowcount!=1: raise ValueError('source item changed during deferral')
    result=publish_cash_source(cur,source_id=source_id,expected_revision=expected_revision)
    return {**result,'deferred_source_id':str(deferred_id),'deferred_count':len(unresolved)}
