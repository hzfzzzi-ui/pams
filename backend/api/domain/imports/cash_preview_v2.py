"""Read-only preview for source-record cash documents."""
from __future__ import annotations
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from collections.abc import Mapping


def preview_cash(cur, *, source_id, expected_revision: int) -> dict:
    cur.execute('''SELECT account_code,document_type,status,revision,content,sha256
        FROM public.source_records WHERE source_id=%s''',(str(source_id),))
    row=cur.fetchone(); names=('account_code','document_type','status','revision','content','sha256')
    src=dict(row) if isinstance(row,Mapping) else dict(zip(names,row)) if row else None
    if not src or src['status']!='draft' or src['document_type']!='cash' or src['revision']!=expected_revision:
        raise ValueError('source changed or cannot preview')
    if src['content'] is not None and sha256(bytes(src['content'])).hexdigest()!=src['sha256']:
        raise ValueError('source content hash mismatch')
    cur.execute('''SELECT source_item_id,source_uid,source_position,parse_method,raw_fields,parsed_fields,record_hash,review_status
        FROM public.source_items WHERE source_id=%s ORDER BY source_item_id''',(str(source_id),))
    cols=('source_item_id','source_uid','source_position','parse_method','raw_fields','parsed_fields','record_hash','review_status')
    rows=[dict(r) if isinstance(r,Mapping) else dict(zip(cols,r)) for r in cur.fetchall()]
    signatures=Counter();candidates=[]
    for item in rows:
        digest=sha256(json.dumps(item['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')).hexdigest()
        if digest!=item['record_hash']: raise ValueError('source item hash mismatch')
        from domain.imports.source_records_v2 import _verify_deferred_source
        if item['review_status'] != 'excluded': _verify_deferred_source(cur,source_id,item)
        ident=str(item['source_item_id']); fields=item['parsed_fields']; status=item['review_status']
        if status=='excluded': candidates.append({'id':ident,'status':'excluded'});continue
        try:
            if fields.get('action') == 'balance' or (item['raw_fields'].get('source') or {}).get('balance_candidate') is True:
                raise ValueError('balance is not a cashflow')
            day=date.fromisoformat(fields['business_date']);amount=Decimal(str(fields['net_cash']));currency=fields['currency']
            if not amount.is_finite() or amount.as_tuple().exponent < -4 or not isinstance(currency,str) or not currency.strip(): raise ValueError('invalid fields')
            if fields.get('posting_date'): date.fromisoformat(fields['posting_date'])
        except (KeyError,TypeError,ValueError,InvalidOperation):
            candidates.append({'id':ident,'status':'invalid'});continue
        sig=(day,amount,currency);signatures[sig]+=1
        candidates.append({'id':ident,'status':'pending' if status!='confirmed' else 'candidate','signature':sig})
    for candidate in candidates:
        if candidate['status']!='candidate': continue
        sig=candidate['signature']
        cur.execute('''SELECT 1 FROM public.pams_cashflow_fact WHERE account_code=%s AND event_date=%s
            AND amount=%s AND currency=%s LIMIT 1''',(src['account_code'],*sig))
        candidate['status']='duplicate' if signatures[sig]>1 or cur.fetchone() else 'ready'
    counts=Counter(row['status'] for row in candidates)
    return {'source_id':str(source_id),'revision':expected_revision,
            'counts':{key:counts[key] for key in ('ready','pending','duplicate','invalid','excluded')},
            'rows':[{'source_item_id':item['id'],'status':item['status']} for item in candidates],
            'can_publish':counts['ready']>0 and not (counts['pending'] or counts['duplicate'] or counts['invalid']),
            'can_publish_ready':counts['ready']>0 and bool(counts['pending'] or counts['duplicate'] or counts['invalid'])}
