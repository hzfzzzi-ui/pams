"""Isolated manual review of IBKR holding evidence; no identity authentication."""
from hashlib import sha256
import json
from uuid import UUID, uuid5
from psycopg2.extras import Json
from domain.imports import isolation
from domain.imports.ibkr_holding_source import _extract, _read_source_report, _scope_is_well_formed
from domain.imports.unified_source import lock_source_review, review_item
from domain.imports.fact_associations import check_all_source_links
from domain.brokers.ibkr.ibkr_holding_identity import _position, review_holding_identity


def _hash(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()


def _load(cur,batch_id):
    if cur.connection.autocommit or not isolation.source_enabled(cur):
        raise ValueError('isolated transaction required')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR UPDATE',(str(batch_id),))
    batch=cur.fetchone()
    if (not batch or batch['account_code']!='ibkr' or batch['business_type']!='holding' or
        batch['institution']!='ibkr' or batch['workflow']!='ibkr_holding_review' or
        batch['status'] not in {'draft','review','published'} or batch['date_from']!=batch['date_to']):
        raise ValueError('holding review batch unavailable')
    if batch['status']=='published':
        from domain.imports.source_fact_evidence import _reviewed_snapshot
        if not _reviewed_snapshot(cur,batch,str(batch_id),locking=True,snapshot_cache={}):
            raise ValueError('published holding source snapshot mismatch')
        check_all_source_links(cur,batch_id,[])
    cur.execute('SELECT * FROM source_items WHERE batch_id=%s ORDER BY source_order FOR SHARE',(str(batch_id),))
    items=cur.fetchall()
    scopes=[i for i in items if i['source_uid']=='scope']
    if len(scopes)!=1:
        raise ValueError('holding scope is ambiguous')
    scope=scopes[0]; raw=scope['raw_fields']
    if (_hash(raw)!=str(scope['record_hash']).strip() or scope['parse_method']!='api' or
        raw.get('kind')!='scope' or 'raw_xml' in raw or
        raw.get('business_date')!=batch['date_to'].isoformat() or str(scope['source_item_id'])!=str(uuid5(UUID(str(batch_id)),'scope'))):
        raise ValueError('holding scope evidence mismatch')
    content=_read_source_report(cur,batch,scope)
    _,statement,positions=_extract(content,source_account_id=raw['source_account_id'],business_date=batch['date_to'])
    if (sha256(content).hexdigest()!=raw['source_sha256'] or raw.get('statement_attributes')!=statement):
        raise ValueError('holding report evidence mismatch')
    members={i['source_uid']:i for i in items if i['source_uid']!='scope'}
    if len(members)!=len(positions) or len(items)!=len(positions)+1:
        raise ValueError('holding position set mismatch')
    for row in positions:
        uid='position:'+str(row['ordinal']); item=members.get(uid)
        expected={'kind':'position','source_sha256':raw['source_sha256'],'source_account_id':raw['source_account_id'],**row}
        if (not item or item['parse_method']!='api' or item['raw_fields']!=expected or _hash(expected)!=str(item['record_hash']).strip() or
            str(item['source_item_id'])!=str(uuid5(UUID(str(batch_id)),uid)) or item['file_id']!=scope['file_id'] or
            item.get('origin_file_id')!=scope.get('origin_file_id')):
            raise ValueError('holding position evidence mismatch')
    return batch,scope,members,content,positions


def inspect(cur,batch_id):
    batch,scope,members,_,positions=_load(cur,batch_id)
    return {'batch_id':str(batch_id),'status':batch['status'],'revision':batch['revision'],
        'scope_item_id':str(scope['source_item_id']),'business_date':batch['date_to'].isoformat(),
        'source_account_id':scope['raw_fields']['source_account_id'],'source_sha256':scope['raw_fields']['source_sha256'],
        'scope_reviewed':scope['review_status']=='confirmed',
        'account_binding_reviewed':bool((scope['parsed_fields'] or {}).get('account_binding_review')),
        'positions':[{'item_id':str(members['position:'+str(row['ordinal'])]['source_item_id']),
             'review_status':members['position:'+str(row['ordinal'])]['review_status'],
             'identity_review_present':bool((members['position:'+str(row['ordinal'])]['parsed_fields'] or {}).get('holding_identity_review')),
             **row} for row in positions],
        'identity_authentication':'unknown','report_authentication':'unknown'}


def review(cur,*,batch_id,item_id,expected_revision,note,scope_complete=False):
    if not isinstance(note,str) or not note.strip() or len(note)>1000:
        raise ValueError('explicit review note required')
    batch,scope,members,content,_=_load(cur,batch_id)
    if batch['status'] not in {'draft','review'} or batch['revision']!=expected_revision:
        raise ValueError('stale or noneditable source')
    selected=next((i for i in [scope,*members.values()] if str(i['source_item_id'])==str(item_id)),None)
    if not selected:
        raise ValueError('source item not in batch')
    parsed=dict(selected['parsed_fields'] or {})
    if str(item_id)==str(scope['source_item_id']):
        if not scope_complete or not _scope_is_well_formed(content,scope['raw_fields']['source_account_id'],batch['date_to']):
            raise ValueError('explicit complete scope review required')
        parsed.update(coverage='all_positions',confirmed_complete=True,scope_review={'note':note.strip(),'authentication':'unknown'})
        cur.execute("UPDATE source_batches SET completeness='complete' WHERE batch_id=%s",(str(batch_id),))
    else:
        parsed['source_row_review']={'note':note.strip(),'authentication':'unknown'}
    revision=review_item(cur,batch_id=UUID(str(batch_id)),item_id=UUID(str(item_id)),expected_revision=expected_revision,
                         parsed_fields=parsed,review_status='confirmed',review_note=note.strip())
    return {'batch_id':str(batch_id),'revision':revision,'review_authentication':'unknown'}


def review_account_binding(cur, *, batch_id, source_account_id, expected_revision, note):
    """Record a separate human mapping decision; it does not authenticate the broker."""
    if not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError('explicit account mapping review note required')
    batch, scope, _, _, _ = _load(cur, batch_id)
    if (batch['status'] not in {'draft', 'review'} or batch['revision'] != expected_revision or
        scope['review_status'] != 'confirmed' or not (scope['parsed_fields'] or {}).get('scope_review') or
        not isinstance(source_account_id, str) or source_account_id != scope['raw_fields'].get('source_account_id')):
        raise ValueError('reviewed scope and matching source account required')
    parsed = dict(scope['parsed_fields'] or {})
    parsed['account_binding_review'] = {
        'account_code': batch['account_code'], 'source_account_id': source_account_id,
        'business_date': batch['date_to'].isoformat(), 'source_sha256': scope['raw_fields']['source_sha256'],
        'note': note.strip(), 'authentication': 'unknown',
    }
    revision = review_item(cur, batch_id=UUID(str(batch_id)), item_id=UUID(str(scope['source_item_id'])),
        expected_revision=expected_revision, parsed_fields=parsed, review_status='confirmed', review_note=note.strip())
    return {'batch_id': str(batch_id), 'revision': revision, 'account_authentication': 'unknown'}


def review_identity(cur,*,batch_id,item_id,holding_id,expected_revision,note):
    batch,scope,members,_,_=_load(cur,batch_id)
    item=next((i for i in members.values() if str(i['source_item_id'])==str(item_id)),None)
    if (batch['status'] not in {'draft','review'} or batch['revision']!=expected_revision or not item or
        item['review_status']!='confirmed' or not (item['parsed_fields'] or {}).get('source_row_review')):
        raise ValueError('source row must be reviewed separately first')
    revision=review_holding_identity(cur,batch_id=batch_id,item_id=item_id,holding_id=holding_id,
                                    expected_revision=expected_revision,review_note=note)
    return {'batch_id':str(batch_id),'revision':revision,'identity_authentication':'unknown'}


def review_account_scope(cur, *, batch_id, expected_revision, note, confirmed):
    """Confirm complete securities and currency balances using one exact report."""
    from domain.brokers.ibkr.ibkr_account_scope import build_attestation
    if confirmed is not True or not isinstance(note,str) or not note.strip() or len(note)>1000:
        raise ValueError('explicit complete account scope review required')
    batch,scope,items,content,_=_load(cur,batch_id)
    if batch['status'] not in {'draft','review'} or batch['revision']!=expected_revision:
        raise ValueError('stale or noneditable source')
    cur.execute('SELECT id FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s FOR UPDATE',('ibkr',batch['date_to']))
    att=build_attestation(cur,batch,scope,items,content,note,expected_revision+1)
    cur.execute("UPDATE source_batches SET scope_attestation=%s,revision=revision+1,updated_at=now() WHERE batch_id=%s AND revision=%s RETURNING revision",
                (Json(att),str(batch_id),expected_revision))
    updated=cur.fetchone()
    if not updated: raise ValueError('stale account scope')
    return {'batch_id':str(batch_id),'revision':updated['revision'],'authentication':'unknown',
            'cash_currencies':sorted(att['cash_amounts'])}


def publish(cur,*,batch_id,expected_revision):
    batch,scope,members,content,_=_load(cur,batch_id)
    if batch['status'] not in {'draft','review'} or batch['revision']!=expected_revision:
        raise ValueError('stale or noneditable source')
    if (scope['review_status']!='confirmed' or batch['completeness']!='complete' or
        not (scope['parsed_fields'] or {}).get('scope_review') or
        not _scope_is_well_formed(content,scope['raw_fields']['source_account_id'],batch['date_to'])):
        raise ValueError('scope has not been explicitly reviewed')
    for item in members.values():
        parsed=item['parsed_fields'] or {}
        if item['review_status']!='confirmed' or not parsed.get('source_row_review'):
            raise ValueError('each source row needs explicit review')
        if 'holding_identity_review' in parsed:
            identity=parsed['holding_identity_review']
            current,_=_position(cur,batch_id=batch_id,item_id=item['source_item_id'],holding_id=identity['holding_id'],published=False)
            if current!=identity:
                raise ValueError('holding identity review is stale')
    attestation=batch.get('scope_attestation')
    if attestation:
        from domain.brokers.ibkr.ibkr_account_scope import build_attestation
        if attestation.get('kind')!='ibkr_reviewed_complete_account' or build_attestation(
            cur,batch,scope,members,content,attestation.get('note',''),batch['revision'])!=attestation:
            raise ValueError('complete account scope review is stale')
    snapshot=lock_source_review(cur,batch_id=UUID(str(batch_id)),expected_revision=expected_revision)
    if snapshot['snapshot']['blockers']:
        raise ValueError('source review incomplete')
    check_all_source_links(cur,batch_id,[])
    result={'records':[],'scope':'source_review_only','review_authentication':'unknown'}
    cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_result=%s WHERE batch_id=%s",
                (snapshot['source_snapshot_sha256'],Json(result),str(batch_id)))
    scope_version=None
    if attestation:
        from domain.brokers.ibkr.ibkr_account_scope import record_complete
        scope_version=record_complete(cur,batch_id)
    return {'batch_id':str(batch_id),'status':'published','scope':'account_scope' if scope_version else 'source_review_only',
            'account_scope_version_id':scope_version,'review_authentication':'unknown','identity_authentication':'unknown'}
