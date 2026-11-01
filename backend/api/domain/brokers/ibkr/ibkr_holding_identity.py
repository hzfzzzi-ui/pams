"""Isolated explicit holding-to-contract review; authentication remains unknown."""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import re
from uuid import UUID

from domain.imports import isolation
from domain.imports.ibkr_holding_source import _extract, _read_source_report, _scope_is_well_formed
from domain.imports.unified_source import review_item


def _hash(raw):
    encoded=json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf8')
    return sha256(encoded).hexdigest()


def _position(cur, *, batch_id, item_id, holding_id, published):
    if cur.connection.autocommit or not isolation.source_enabled(cur):
        raise ValueError('identity review requires isolated transaction')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR SHARE',(str(batch_id),))
    batch=cur.fetchone()
    if (not batch or batch['account_code']!='ibkr' or batch['institution']!='ibkr'
            or batch['business_type']!='holding' or batch['date_from']!=batch['date_to']
            or batch['date_to'] is None or (published and batch['status']!='published')
            or (not published and batch['status'] not in ('draft','review'))):
        raise ValueError('identity source batch is invalid')
    cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope' FOR SHARE",(str(batch_id),))
    scope=cur.fetchone()
    cur.execute('SELECT * FROM source_items WHERE batch_id=%s AND source_item_id=%s FOR SHARE',(str(batch_id),str(item_id)))
    item=cur.fetchone()
    if (not scope or not item or scope.get('file_id')!=item.get('file_id')
            or scope.get('origin_file_id')!=item.get('origin_file_id')):
        raise ValueError('identity source item is missing')
    if published:
        from domain.imports.source_fact_evidence import _reviewed_snapshot
        if (scope['review_status']!='confirmed' or item['review_status']!='confirmed'
                or not str(scope['review_note'] or '').strip() or not str(item['review_note'] or '').strip()
                or not _reviewed_snapshot(cur,batch,str(batch_id),locking=True,snapshot_cache={})):
            raise ValueError('identity review is not published')
    scope_raw,item_raw=scope['raw_fields'],item['raw_fields']
    if _hash(scope_raw)!=scope['record_hash'].strip() or _hash(item_raw)!=item['record_hash'].strip():
        raise ValueError('identity source record hash mismatch')
    try:
        content=_read_source_report(cur,batch,scope)
        digest=sha256(content).hexdigest()
        account=scope_raw['source_account_id']
        _,statement,rows=_extract(content,source_account_id=account,business_date=batch['date_to'])
        if not _scope_is_well_formed(content,account,batch['date_to']):
            raise ValueError('identity report scope is invalid')
        ordinal=item_raw['ordinal']
        matches=[r for r in rows if r['ordinal']==ordinal]
    except (KeyError,ValueError,TypeError,AttributeError) as exc:
        raise ValueError('identity original report is invalid') from exc
    if (len(matches)!=1 or scope_raw.get('kind')!='scope' or scope_raw.get('source_sha256')!=digest
            or scope_raw.get('business_date')!=batch['date_to'].isoformat()
            or scope_raw.get('statement_attributes')!=statement
            or item['source_uid']!='position:'+str(ordinal)
            or item_raw!={'kind':'position','source_sha256':digest,'source_account_id':account,**matches[0]}):
        raise ValueError('identity original source mismatch')
    row=matches[0]; attrs=row['raw_attributes']; conid=str(attrs.get('conid') or '').strip()
    if (row['state']=='unknown' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',conid)
            or sum((r['symbol'],r['currency'])==(row['symbol'],row['currency']) for r in rows)!=1
            or sum((str(r['raw_attributes'].get('conid') or '').strip(),r['currency'])==(conid,row['currency']) for r in rows)!=1):
        raise ValueError('identity contract is missing or ambiguous')
    cur.execute('SELECT row_to_json(h)::text AS body FROM pams_holdings_snapshot h WHERE id=%s FOR UPDATE',(holding_id,))
    selected=cur.fetchone()
    if not selected:
        raise ValueError('identity holding is missing')
    raw_text=selected['body']; holding=json.loads(raw_text,parse_float=Decimal)
    numbers={'quantity':('position','quantity','qty'),'cost':('costBasisMoney','costBasis','cost'),
             'market_value':('marketValue','positionValue','value','markValue')}
    from domain.imports.ibkr_holding_source import _number
    if (holding.get('account_code')!='ibkr' or holding.get('as_of')!=batch['date_to'].isoformat()
            or holding.get('symbol')!=row['symbol'] or holding.get('currency')!=row['currency']
            or any(holding.get(k) is None or Decimal(str(holding[k]))!=_number(attrs,aliases) for k,aliases in numbers.items())):
        raise ValueError('identity source conflicts with holding')
    cur.execute('''SELECT count(*) AS n FROM pams_holdings_snapshot
        WHERE account_code='ibkr' AND as_of=%s AND symbol=%s AND currency=%s''',
        (batch['date_to'],row['symbol'],row['currency']))
    if cur.fetchone()['n']!=1:
        raise ValueError('identity holding is ambiguous')
    from domain.investments.valuation_input_evidence import _hash as holding_hash
    return {'holding_id':holding_id,'holding_hash':holding_hash(holding),
            'conid':conid,'source_account_id':account,'source_sha256':digest,'business_date':batch['date_to'].isoformat(),
            'identity_authentication':'unknown'},item


def review_holding_identity(cur, *, batch_id, item_id, holding_id, expected_revision, review_note):
    """Record a person's explicit review; no claim of reviewer authentication."""
    if not isinstance(review_note,str) or not review_note.strip():
        raise ValueError('explicit identity review note is required')
    evidence,item=_position(cur,batch_id=batch_id,item_id=item_id,holding_id=holding_id,published=False)
    parsed=dict(item['parsed_fields'] or {})
    parsed['holding_identity_review']=evidence
    return review_item(cur,batch_id=UUID(str(batch_id)),item_id=UUID(str(item_id)),
                       expected_revision=expected_revision,parsed_fields=parsed,
                       review_status='confirmed',review_note=review_note)


def verified_holding_identity(cur, *, batch_id, item_id, holding_id):
    """Verify the reviewed evidence against the current complete holding row."""
    evidence,item=_position(cur,batch_id=batch_id,item_id=item_id,holding_id=holding_id,published=True)
    if (item['parsed_fields'] or {}).get('holding_identity_review')!=evidence:
        raise ValueError('holding identity review is absent or stale')
    return evidence
