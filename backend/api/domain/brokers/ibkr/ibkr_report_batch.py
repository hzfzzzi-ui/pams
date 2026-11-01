"""Isolated per-report IBKR source batch; caller owns one transaction.

The first verified report creates a fact and its primary source link.  A later
report can contain the same independently matched fact, but the item is marked
as an unlinked observation until a formal multi-source contribution relation is
available.  This module never backfills historical data or changes live paths.
"""
from __future__ import annotations

from hashlib import sha256
from datetime import date
import json
from uuid import UUID, uuid5

from psycopg2.extras import Json
from domain.imports import isolation
from domain.imports.unified_source import add_item, attach_file, create_batch, lock_source_review
from domain.imports.fact_associations import check_all_source_links
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts
from domain.brokers.ibkr.ibkr_cash_event_sync import _stored_equal
from domain.brokers.ibkr.ibkr_report_trade_source import _row

NAMESPACE=UUID('e2444fb8-94c2-43e1-a08e-27a0107be06b')
MAX_REPORT_ITEMS=2000
MAX_XML_BYTES=32*1024*1024


def _digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf8')).hexdigest()


def _report_original(cur,batch,scope):
    """Resolve a report's local or frozen same-account original file."""
    from domain.imports.ibkr_holding_source import _read_source_report
    raw=scope.get('raw_fields') or {}
    adapter={**scope,'raw_fields':{'source_account_id':raw.get('source_account_id'),
                                   'source_sha256':raw.get('sha256')}}
    return _read_source_report(cur,batch,adapter)


def _dated_fx_evidence(cur, event, fact):
    """Record the CNY conversion input without presenting it as settlement FX."""
    currency=event['currency']; day=event['trade_date']
    if currency=='CNY':
        rate='1'; source=None; kind='identity'
    else:
        cur.execute("SELECT rate,source FROM pams_fx_rates_daily WHERE as_of=%s AND base_currency='CNY' AND quote_currency=%s FOR SHARE",(day,currency))
        rows=cur.fetchall()
        if len(rows)!=1 or not _stored_equal(rows[0]['rate'],fact.get('fx_rate_to_cny')):
            raise ValueError('IBKR cash event dated FX changed')
        rate=str(rows[0]['rate']); source=rows[0]['source']; kind='daily_reference_not_broker_settlement'
    if not _stored_equal(rate,fact.get('fx_rate_to_cny')):
        raise ValueError('IBKR cash event FX fact mismatch')
    return {'as_of':day.isoformat(),'base_currency':'CNY','quote_currency':currency,
            'rate':rate,'source':source,'kind':kind}


def _existing_cash_proven(cur, fact, event):
    """Existing records need their own frozen original; this adds no lineage."""
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    proof=verify_ibkr_fact(cur,trade_id=fact['trade_id'])
    required={'trade_date','account_code','currency','side','gross_amount','net_cash',
              'source_key','source_id','fx_rate_to_cny','net_cash_cny','fx_evidence','fx_source'}
    if proof.get('status') not in {'consistent','partial'} or required.intersection(proof.get('unknown_fields',[])):
        raise ValueError('existing IBKR cash event lacks verified original evidence')
    cur.execute('SELECT raw_fields FROM source_items WHERE source_item_id=%s FOR SHARE',(fact.get('source_item_id'),))
    source=cur.fetchone()
    original=(source or {}).get('raw_fields') or {}
    if (original.get('source_account_id')!=event.get('source_account_id')
            or original.get('official_attributes')!=event.get('raw_json')):
        raise ValueError('existing IBKR cash original row differs')


def _cash_supplement(cur, *, original_batch, original_file, original_scope, report_sha256,
                     source_account_id, event, ordinal, fact, created):
    """Publish a new source decision referencing immutable original bytes."""
    if not isolation.source_enabled(cur) or cur.connection.autocommit:
        raise ValueError('IBKR cash supplement requires isolated transaction')
    original_batch=UUID(str(original_batch))
    original_file=UUID(str(original_file))
    original_scope=UUID(str(original_scope))
    cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('account-import:ibkr',0)) AS acquired")
    if not cur.fetchone()['acquired']:
        raise ValueError('IBKR account lock is busy')
    if UUID(str(original_scope))!=uuid5(UUID(str(original_batch)),'scope'):
        raise ValueError('IBKR cash supplement original identity mismatch')
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR SHARE',(str(original_batch),))
    original=cur.fetchone()
    cur.execute('SELECT * FROM source_items WHERE source_item_id=%s AND batch_id=%s FOR SHARE',
                (str(original_scope),str(original_batch)))
    scope=cur.fetchone()
    if (not original or original['status']!='published' or original['account_code']!='ibkr'
            or original['institution']!='ibkr' or original['workflow'] not in {'ibkr_report','ibkr_cash_report'}
            or original['date_to'] is None
            or UUID(str(original_batch))!=uuid5(NAMESPACE,('cash-batch:' if original['workflow']=='ibkr_cash_report' else 'batch:')+source_account_id+':'+original['date_to'].isoformat()+':'+report_sha256)
            or not scope or scope['review_status']!='confirmed'
            or str(scope.get('file_id') or scope.get('origin_file_id'))!=str(original_file)
            or (scope['raw_fields'] or {}).get('source_account_id')!=source_account_id
            or (scope['raw_fields'] or {}).get('sha256')!=report_sha256
            or (scope['raw_fields'] or {}).get('business_date')!=original['date_to'].isoformat()
            or event.get('source_account_id')!=source_account_id
            or event.get('source_evidence',{}).get('source_sha256')!=report_sha256):
        raise ValueError('IBKR cash supplement original evidence mismatch')
    content=_report_original(cur,original,scope)
    if sha256(content).hexdigest()!=report_sha256:
        raise ValueError('IBKR cash supplement original hash mismatch')
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    if not _reviewed_snapshot(cur,original,str(original_batch),locking=True,snapshot_cache={}):
        raise ValueError('IBKR cash supplement original snapshot mismatch')
    from domain.brokers.ibkr.ibkr_daily_close import _document
    from collectors.ibkr_flex import parse_flex_report
    text=content.decode('utf8')
    _,_,error=_document(text,source_account_id,report_sha256)
    if error: raise ValueError('IBKR cash supplement original report unsafe')
    original_events=parse_flex_report(text,account_code='ibkr',allow_empty_holdings=True).cash_events
    if (not isinstance(ordinal,int) or ordinal<1 or ordinal>len(original_events)
            or original_events[ordinal-1].get('raw')!=event.get('raw_json')
            or original_events[ordinal-1].get('scope_error')
            or sum(item.get('raw')==event.get('raw_json') for item in original_events)!=1):
        raise ValueError('IBKR cash supplement original row mismatch')
    from domain.brokers.ibkr.ibkr_cash_events import dated_fx_rate, normalize_cash_event
    normalized=normalize_cash_event(original_events[ordinal-1],account_code='ibkr',cutoff=date.min,
                                    statement_as_of=original['date_to'])
    fields=('source_key','source_account_id','transaction_id','account_code','trade_date','currency','side','net_cash','raw_json')
    if not normalized or any(normalized.get(key)!=event.get(key) for key in fields):
        raise ValueError('IBKR cash supplement normalized row mismatch')
    rate=dated_fx_rate(cur,normalized['currency'],normalized['trade_date'])
    if rate is None:
        raise ValueError('IBKR cash supplement dated FX missing')
    normalized['fx_rate_to_cny']=rate
    expected=build_investment_trade_fact(normalized)
    if normalized['side']=='fee':
        expected['fee_amount']=abs(normalized['net_cash'])
        expected['fee_currency']=normalized['currency']
    elif normalized['side']=='tax':
        expected['tax_amount']=abs(normalized['net_cash'])
    cur.execute('SELECT * FROM pams_investment_trade_fact WHERE trade_id=%s FOR UPDATE',(fact['trade_id'],))
    actual=cur.fetchone()
    protected=('account_code','trade_date','symbol','currency','trade_type','side','gross_amount','fee_amount',
               'fee_currency','tax_amount','net_cash','fx_rate_to_cny','net_cash_cny','source_table','source_key')
    if not actual or any(not _stored_equal(actual.get(key),expected.get(key)) for key in protected):
        raise ValueError('IBKR cash supplement fact mismatch')
    fact=actual
    fx_evidence=_dated_fx_evidence(cur,normalized,fact)
    batch_id=uuid5(original_batch,'cash-supplement:'+event['source_key'])
    item_id=uuid5(batch_id,'cash:'+event['source_key'])
    raw={'kind':'cash_supplement','original_batch_id':str(original_batch),'original_item_id':str(original_scope),
         'original_file_id':str(original_file),'report_sha256':report_sha256,'source_account_id':source_account_id,
         'business_date':event['trade_date'].isoformat(),'ordinal':ordinal,'source_key':event['source_key'],
         'official_attributes':event['raw_json']}
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR UPDATE',(str(batch_id),))
    previous=cur.fetchone()
    if previous:
        cur.execute('SELECT * FROM source_items WHERE batch_id=%s AND source_item_id=%s FOR SHARE',(str(batch_id),str(item_id)))
        item=cur.fetchone()
        if (previous['status']!='published' or previous['account_code']!='ibkr'
                or not item or item['review_status']!='confirmed' or item['raw_fields']!=raw
                or str(item.get('origin_file_id'))!=str(original_file)
                or item['parsed_fields'].get('fact_id')!=fact['trade_id']
                or item['parsed_fields'].get('fx_evidence')!=fx_evidence
                or not _reviewed_snapshot(cur,previous,str(batch_id),locking=True,snapshot_cache={})):
            raise ValueError('IBKR cash supplement replay mismatch')
        role=item['parsed_fields'].get('observation')
        if role=='cash_supplement':
            if str(fact.get('source_item_id'))!=str(item_id): raise ValueError('IBKR cash supplement source mismatch')
            records=[{'table':'pams_investment_trade_fact','id':fact['trade_id'],'row_id':str(item_id)}]
        elif role=='matched_existing_unlinked':
            _existing_cash_proven(cur,fact,event); records=[]
        else: raise ValueError('IBKR cash supplement observation mismatch')
        check_all_source_links(cur,batch_id,records)
        return str(batch_id)
    if created and fact.get('source_item_id') is not None:
        raise ValueError('new IBKR cash event already has a source')
    if not created:
        _existing_cash_proven(cur,fact,event)
    role='cash_supplement' if created else 'matched_existing_unlinked'
    create_batch(cur,batch_id=batch_id,account_code='ibkr',business_type='cash',institution='ibkr',acquisition_method='api')
    cur.execute("UPDATE source_batches SET workflow='ibkr_cash_supplement',completeness='partial',date_from=%s,date_to=%s WHERE batch_id=%s",
                (event['trade_date'],event['trade_date'],str(batch_id)))
    add_item(cur,item_id=item_id,batch_id=batch_id,origin_file_id=original_file,source_uid=event['source_key'],
             parse_method='api',external_id=event['transaction_id'],record_hash=_digest(raw),raw_fields=raw,
             parsed_fields={'observation':role,'fact_id':fact['trade_id'],
                            'dated_fx_rate':str(fact['fx_rate_to_cny']),'fx_evidence':fx_evidence})
    cur.execute("UPDATE source_items SET review_status='confirmed',review_note='automated original-row and dated FX check; no human authentication' WHERE source_item_id=%s",(str(item_id),))
    if created:
        cur.execute('UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE trade_id=%s AND source_item_id IS NULL RETURNING trade_id',
                    (str(item_id),fact['trade_id']))
        if len(cur.fetchall())!=1: raise ValueError('IBKR cash supplement association conflict')
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(str(batch_id),))
    snapshot=lock_source_review(cur,batch_id=batch_id,expected_revision=cur.fetchone()['revision'])
    if snapshot['snapshot']['blockers']: raise ValueError('IBKR cash supplement is incomplete')
    records=[{'table':'pams_investment_trade_fact','id':fact['trade_id'],'row_id':str(item_id)}] if created else []
    check_all_source_links(cur,batch_id,records)
    cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_result=%s WHERE batch_id=%s",
                (snapshot['source_snapshot_sha256'],Json({'records':records,'origin_file_id':str(original_file),
                 'observations':[] if created else [{'item_id':str(item_id),'fact_id':fact['trade_id'],'role':role}]}),str(batch_id)))
    return str(batch_id)


def _same_report_fact(existing, expected, fields):
    """Late FX enriches projections, never the original immutable trade fact."""
    for key in fields:
        if key in {'fx_rate_to_cny','net_cash_cny','realized_pnl_cny'} and existing.get('fx_rate_to_cny') is None:
            if existing.get(key) is not None:
                return False
            continue
        if not _stored_equal(existing.get(key), expected.get(key)):
            return False
    return True


def publish_report_batch(cur, *, report_bytes:bytes, source_account_id:str, day, report_sha256:str,
                         replay=False, cash_entries=None, cash_pending_count=0, cash_only=False, trade_cutoff=None):
    if not isolation.source_enabled(cur) or cur.connection.autocommit:
        raise ValueError('IBKR report batch requires isolated transaction')
    if not isinstance(report_bytes,bytes) or not report_bytes or len(report_bytes)>MAX_XML_BYTES:
        raise ValueError('IBKR report byte limit exceeded')
    if sha256(report_bytes).hexdigest()!=report_sha256:
        raise ValueError('IBKR report hash mismatch')
    try:
        xml=report_bytes.decode('utf8')
    except UnicodeError as exc:
        raise ValueError('IBKR report is not UTF-8') from exc
    from domain.brokers.ibkr.ibkr_daily_close import _document
    document,_,error=_document(xml,source_account_id,report_sha256)
    if error:
        raise ValueError('unsafe IBKR report: '+error)
    count=sum(1 for statement in document.statements for node in statement.iter()
              if node.tag.rsplit('}',1)[-1] in {'Trade','CashTransaction'})
    # Reserve one source item for the report-level integrity record.
    if count+1>MAX_REPORT_ITEMS:
        raise ValueError('IBKR report exceeds source item limit')
    from collectors.ibkr_flex import parse_flex_report, _validated_isolated_report_trades
    parsed=parse_flex_report(xml,account_code='ibkr',allow_empty_holdings=cash_only)
    if len(parsed.trades)>MAX_REPORT_ITEMS:
        raise ValueError('IBKR report exceeds source item limit')
    if cash_only:
        statements=[statement for statement in document.statements if statement.get('toDate') in (day.strftime('%Y%m%d'),day.isoformat())]
        if len(statements)!=1:
            raise ValueError('IBKR cash report statement is ambiguous')
        trades=[]
        stats={'trades_not_written_by_cash_import':len(parsed.trades)}
    else:
        account,trades,stats=_validated_isolated_report_trades(parsed,day,'ibkr',xml,report_sha256)
        if account!=source_account_id:
            raise ValueError('IBKR report account mismatch')
    cash_entries=list(cash_entries or [])
    cash_keys=set()
    for entry in cash_entries:
        event=entry.get('event') or {}; raw=event.get('raw_json') or {}
        matches=[(idx,item) for idx,item in enumerate(parsed.cash_events,1) if item.get('raw')==raw]
        if (event.get('source_account_id')!=source_account_id or event.get('account_code')!='ibkr'
                or event.get('source_key') in cash_keys or len(matches)!=1 or matches[0][1].get('scope_error')
                or event.get('source_evidence',{}).get('source_sha256')!=report_sha256):
            raise ValueError('IBKR cash event report identity is ambiguous')
        entry['_original_ordinal']=matches[0][0]
        cash_keys.add(event['source_key'])
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    batch_id=uuid5(NAMESPACE,('cash-batch:' if cash_only else 'batch:')+source_account_id+':'+day.isoformat()+':'+report_sha256)
    file_id=uuid5(batch_id,'raw-xml')
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR UPDATE',(str(batch_id),))
    prior=cur.fetchone()
    original_trade_count=len(trades)
    if prior and not cash_only:
        recorded_count=(prior.get('published_result') or {}).get('trade_count')
        if recorded_count == 0:
            trades=[]
        elif recorded_count != original_trade_count:
            raise ValueError('IBKR report published trade selection is inconsistent')
    elif trade_cutoff is not None and not cash_only:
        trades=[trade for trade in trades if trade['trade_date'] > trade_cutoff]
    stats['trades_skipped_by_cutoff']=original_trade_count-len(trades)
    prepared=[]
    for trade in trades:
        row=_row(cur,trade,source_account_id,day,report_sha256)
        prepared.append((row['source_key'],row))
    if len({key for key,_ in prepared})!=len(prepared):
        raise ValueError('IBKR report trade identity ambiguous')
    if prior:
        if (prior['status']!='published' or prior['account_code']!='ibkr' or prior['institution']!='ibkr'
                or prior['workflow']!=('ibkr_cash_report' if cash_only else 'ibkr_report') or prior['date_to']!=day):
            raise ValueError('IBKR report batch is incomplete')
        cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope' FOR SHARE",(str(batch_id),))
        existing_scope=cur.fetchone()
        if not existing_scope:
            raise ValueError('IBKR report batch scope missing')
        file_id=UUID(str(existing_scope.get('file_id') or existing_scope.get('origin_file_id')))
        if _report_original(cur,prior,existing_scope)!=report_bytes:
            raise ValueError('IBKR report batch source mismatch')
        from domain.imports.source_fact_evidence import _reviewed_snapshot
        if not _reviewed_snapshot(cur,prior,str(batch_id),locking=True,snapshot_cache={}):
            raise ValueError('IBKR report snapshot mismatch')
        saved=prior['published_result'] or {}
        records=saved.get('records')
        if not isinstance(records,list):
            raise ValueError('IBKR report publication records missing')
        check_all_source_links(cur,batch_id,records)
        cur.execute("SELECT source_uid,raw_fields,parsed_fields FROM source_items WHERE batch_id=%s AND review_status='confirmed'",(str(batch_id),))
        items={item['source_uid']:item for item in cur.fetchall()}
        if (saved.get('trade_count')!=len(trades)
                or len(items)!=len(trades)+saved.get('cash_count',0)+1 or 'scope' not in items):
            raise ValueError('IBKR report batch item set mismatch')
        scope=items['scope']['raw_fields']
        if any(scope.get(k)!=v for k,v in {'kind':'report','sha256':report_sha256,
                'source_account_id':source_account_id,'business_date':day.isoformat()}.items()):
            raise ValueError('IBKR report batch scope mismatch')
        for key,row in prepared:
            item=items.get(key)
            if not item or item['raw_fields'].get('official_attributes')!=row['raw_json']:
                raise ValueError('IBKR report replay item mismatch')
            fact=build_investment_trade_fact(row); fact['fee_currency']=row['explicit_fee_currency']
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade' AND source_key=%s FOR SHARE",(key,))
            facts=cur.fetchall()
            protected=('account_code','trade_date','symbol','currency','trade_type','side','quantity','price',
                       'gross_amount','fee_amount','fee_currency','tax_amount','net_cash','realized_pnl','realized_pnl_cny','fx_rate_to_cny','net_cash_cny')
            if len(facts)!=1 or not _same_report_fact(facts[0],fact,protected):
                raise ValueError('IBKR report replay fact mismatch')
            if item['parsed_fields'].get('fact_id')!=facts[0]['trade_id']:
                raise ValueError('IBKR report replay identity mismatch')
            role=item['parsed_fields'].get('observation')
            if role=='created' and str(facts[0].get('source_item_id'))!=str(uuid5(batch_id,'trade:'+key)):
                raise ValueError('IBKR report replay source association mismatch')
            if role not in {'created','matched_existing_unlinked'}:
                raise ValueError('IBKR report replay observation invalid')
        for entry in cash_entries:
            event=entry['event']; item=items.get(event['source_key'])
            cur.execute('SELECT * FROM pams_investment_trade_fact WHERE trade_id=%s FOR SHARE',(entry['fact_id'],))
            fact=cur.fetchone()
            if (not fact
                    or fact.get('account_code')!='ibkr' or fact.get('source_key')!=event['source_key']
                    or fact.get('currency')!=event['currency'] or fact.get('trade_date')!=event['trade_date']
                    or fact.get('side')!=event['side'] or fact.get('net_cash')!=event['net_cash']):
                raise ValueError('IBKR report replay cash fact mismatch')
            if item:
                if (item['raw_fields'].get('official_attributes')!=event['raw_json']
                        or item['parsed_fields'].get('fact_id')!=fact['trade_id']
                        or item['parsed_fields'].get('fx_evidence')!=_dated_fx_evidence(cur,event,fact)):
                    raise ValueError('IBKR report replay cash item mismatch')
                role=item['parsed_fields'].get('observation')
                if role=='created' and str(fact.get('source_item_id'))!=str(uuid5(batch_id,'cash:'+event['source_key'])):
                    raise ValueError('IBKR report replay cash source mismatch')
                if role=='matched_existing_unlinked': _existing_cash_proven(cur,fact,event)
                elif role!='created': raise ValueError('IBKR report replay cash observation mismatch')
            else:
                _cash_supplement(cur,original_batch=batch_id,original_file=file_id,original_scope=uuid5(batch_id,'scope'),
                    report_sha256=report_sha256,source_account_id=source_account_id,event=event,
                    ordinal=entry['_original_ordinal'],fact=fact,created=entry['created'])
        return {'written':0,'existing_verified':len(trades),'batch_id':str(batch_id),
                'source_contributions':'not_recorded','replayed':True,**stats}
    if replay:
        raise ValueError('IBKR report batch missing on replay')
    create_batch(cur,batch_id=batch_id,account_code='ibkr',business_type='cash' if cash_only else 'trade',institution='ibkr',acquisition_method='api')
    earliest=min([day]+[entry['event']['trade_date'] for entry in cash_entries])
    cur.execute("UPDATE source_batches SET workflow=%s,completeness='partial',date_from=%s,date_to=%s WHERE batch_id=%s",('ibkr_cash_report' if cash_only else 'ibkr_report',earliest,day,str(batch_id)))
    cur.execute("""SELECT f.file_id FROM source_files f JOIN source_batches b ON b.batch_id=f.batch_id
        JOIN source_items s ON s.batch_id=b.batch_id AND s.source_uid='scope' AND s.file_id=f.file_id
        WHERE b.account_code='ibkr' AND b.institution='ibkr' AND b.workflow IN ('ibkr_holding_review',%s)
          AND b.status='published' AND b.date_to=%s AND f.sha256=%s AND s.review_status='confirmed'
          AND s.raw_fields->>'source_account_id'=%s AND COALESCE(s.raw_fields->>'source_sha256',s.raw_fields->>'sha256')=%s
        ORDER BY f.created_at,f.file_id FOR SHARE OF b,f,s""",('ibkr_report' if cash_only else 'ibkr_cash_report',day,report_sha256,source_account_id,report_sha256))
    originals=cur.fetchall()
    origin_file_id=UUID(str(originals[0]['file_id'])) if originals else None
    if origin_file_id is None:
        attach_file(cur,file_id=file_id,batch_id=batch_id,filename='ibkr-flex.xml',file_format='xml',
                    content_type='application/xml',content=report_bytes)
    else:
        file_id=origin_file_id
    scope={'kind':'report','sha256':report_sha256,'source_account_id':source_account_id,'business_date':day.isoformat(),
           'reported_cash_count':len(parsed.cash_events),'recorded_cash_count':len(cash_entries),
           'cash_pending_count':cash_pending_count}
    add_item(cur,item_id=uuid5(batch_id,'scope'),batch_id=batch_id,file_id=None if origin_file_id else file_id,
             origin_file_id=origin_file_id,source_uid='scope',parse_method='api',
             record_hash=_digest(scope),raw_fields=scope,parsed_fields={'scope':'integrity_only'})
    if origin_file_id:
        cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(str(batch_id),)); staged_batch=cur.fetchone()
        cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'",(str(batch_id),)); staged_scope=cur.fetchone()
        if _report_original(cur,staged_batch,staged_scope)!=report_bytes:
            raise ValueError('IBKR reused original differs from report')
    expected=[]; observations=[]; written=0; existing_verified=0
    verified_originals={}
    for key,row in sorted(prepared,key=lambda x:x[0]):
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('ibkr_flex_trade:'+key,))
        fact=build_investment_trade_fact(row); fact['fee_currency']=row['explicit_fee_currency']
        cur.execute('''SELECT f.*,i.raw_fields AS source_raw,i.review_status AS source_review,
            COALESCE(i.file_id,i.origin_file_id) AS source_file_id,b.batch_id AS source_batch_id,b.status AS source_status,b.account_code AS source_account,
            sf.sha256 AS original_sha
            FROM pams_investment_trade_fact f LEFT JOIN source_items i ON i.source_item_id=f.source_item_id
            LEFT JOIN source_batches b ON b.batch_id=i.batch_id
            LEFT JOIN source_files sf ON sf.file_id=COALESCE(i.file_id,i.origin_file_id)
            WHERE f.source_table='ibkr_flex_trade' AND f.source_key=%s FOR UPDATE OF f''',(key,))
        found=cur.fetchall()
        protected=('account_code','trade_date','symbol','currency','trade_type','side','quantity','price',
                   'gross_amount','fee_amount','fee_currency','tax_amount','net_cash','realized_pnl','realized_pnl_cny','fx_rate_to_cny','net_cash_cny')
        if found:
            existing=found[0]; old=existing.get('source_raw') or {}
            if (len(found)!=1 or existing.get('source_status')!='published' or existing.get('source_review')!='confirmed'
                or existing.get('source_account')!='ibkr' or not existing.get('source_file_id')
                or old.get('official_attributes')!=row['raw_json'] or old.get('source_key')!=key
                or old.get('source_account_id')!=source_account_id
                or old.get('report_sha256')!=str(existing.get('original_sha') or '').strip()
                or not _same_report_fact(existing,fact,protected)):
                raise ValueError('existing IBKR fact lacks matching original source')
            old_batch_id=str(existing['source_batch_id'])
            if old_batch_id not in verified_originals:
                cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR SHARE',(existing['source_batch_id'],))
                original_batch=cur.fetchone()
                cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope' FOR SHARE",(existing['source_batch_id'],))
                old_scope=cur.fetchone()
                if not original_batch or not old_scope: raise ValueError('existing IBKR report source file missing')
                old_bytes=_report_original(cur,original_batch,old_scope); old_digest=sha256(old_bytes).hexdigest()
                if old_digest!=str(existing.get('original_sha') or '').strip():
                    raise ValueError('existing IBKR report hash mismatch')
                try:
                    old_text=old_bytes.decode('utf8')
                except UnicodeError as exc:
                    raise ValueError('existing IBKR report is malformed') from exc
                _,_,old_error=_document(old_text,source_account_id,old_digest)
                if old_error: raise ValueError('existing IBKR report is unsafe')
                old_parsed=parse_flex_report(old_text,account_code='ibkr')
                _,old_trades,_=_validated_isolated_report_trades(old_parsed,day,'ibkr',old_text,old_digest)
                from domain.imports.source_fact_evidence import _reviewed_snapshot
                if not original_batch or not _reviewed_snapshot(cur,original_batch,old_batch_id,locking=True,snapshot_cache={}):
                    raise ValueError('existing IBKR report source snapshot mismatch')
                verified_originals[old_batch_id]=[t.get('raw') for t in old_trades]
            if sum(raw==old.get('official_attributes') for raw in verified_originals[old_batch_id])!=1:
                raise ValueError('existing IBKR report row is missing or ambiguous')
            fact_id=existing['trade_id']; role='matched_existing_unlinked'; existing_verified+=1
        else:
            if insert_investment_trade_facts(cur,[fact],strict_source=True)!=1:
                raise ValueError('IBKR report fact conflict')
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE source_table=%s AND source_key=%s',('ibkr_flex_trade',key))
            fact_id=cur.fetchone()['trade_id']; role='created'; written+=1
        item_id=uuid5(batch_id,'trade:'+key)
        raw={'kind':'trade','source_table':'ibkr_flex_trade','source_key':key,'source_account_id':source_account_id,
             'report_sha256':report_sha256,'business_date':day.isoformat(),'official_attributes':row['raw_json']}
        add_item(cur,item_id=item_id,batch_id=batch_id,file_id=None if origin_file_id else file_id,
                 origin_file_id=origin_file_id,source_uid=key,parse_method='api',
                 external_id=key,record_hash=_digest(raw),raw_fields=raw,
                 parsed_fields={'observation':role,'fact_id':fact_id})
        if role=='created':
            cur.execute('UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE trade_id=%s AND source_item_id IS NULL RETURNING trade_id',
                        (str(item_id),fact_id))
            if len(cur.fetchall())!=1: raise ValueError('IBKR report fact association conflict')
            expected.append({'table':'pams_investment_trade_fact','id':fact_id,'row_id':str(item_id)})
        else:
            observations.append({'item_id':str(item_id),'fact_id':fact_id,'role':role})
    for entry in sorted(cash_entries,key=lambda item:item['event']['source_key']):
        event=entry['event']; key=event['source_key']; fact_id=entry['fact_id']
        cur.execute('SELECT * FROM pams_investment_trade_fact WHERE trade_id=%s FOR UPDATE',(fact_id,))
        fact=cur.fetchone()
        if (not fact or fact['source_table']!='ibkr_flex_cash_transaction' or fact['source_key']!=key
                or fact['account_code']!='ibkr' or fact['currency']!=event['currency']
                or fact['trade_date']!=event['trade_date'] or fact['side']!=event['side']
                or fact['net_cash']!=event['net_cash']):
            raise ValueError('IBKR cash event conflicts with report fact')
        created=bool(entry.get('created'))
        if not created:
            _existing_cash_proven(cur,fact,event)
        if created and fact.get('source_item_id') is not None:
            raise ValueError('new IBKR cash event already has a source')
        item_id=uuid5(batch_id,'cash:'+key)
        raw={'kind':'cash_event','source_table':'ibkr_flex_cash_transaction','source_key':key,
             'source_account_id':source_account_id,'report_sha256':report_sha256,
             'business_date':event['trade_date'].isoformat(),'official_attributes':event['raw_json']}
        add_item(cur,item_id=item_id,batch_id=batch_id,file_id=None if origin_file_id else file_id,
                 origin_file_id=origin_file_id,source_uid=key,parse_method='api',
                 external_id=event['transaction_id'],record_hash=_digest(raw),raw_fields=raw,
                 parsed_fields={'observation':'created' if created else 'matched_existing_unlinked',
                                'fact_id':fact_id,'fx_evidence':_dated_fx_evidence(cur,event,fact)})
        if created:
            cur.execute('UPDATE pams_investment_trade_fact SET source_item_id=%s WHERE trade_id=%s AND source_item_id IS NULL RETURNING trade_id',
                        (str(item_id),fact_id))
            if len(cur.fetchall())!=1: raise ValueError('IBKR cash event association conflict')
            expected.append({'table':'pams_investment_trade_fact','id':fact_id,'row_id':str(item_id)})
        else:
            observations.append({'item_id':str(item_id),'fact_id':fact_id,'role':'matched_existing_unlinked'})
    cur.execute("UPDATE source_items SET review_status='confirmed',review_note='automated report integrity check; no human authentication' WHERE batch_id=%s",(str(batch_id),))
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(str(batch_id),))
    rev=cur.fetchone()['revision']; snapshot=lock_source_review(cur,batch_id=batch_id,expected_revision=rev)
    if snapshot['snapshot']['blockers']: raise ValueError('IBKR report source is incomplete')
    check_all_source_links(cur,batch_id,expected)
    result={'trade_count':len(trades),'cash_count':len(cash_entries),'records':expected,'observations':observations,
            'association_status':'cross_report_contributions_not_recorded'}
    cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s,published_result=%s WHERE batch_id=%s",
                (snapshot['source_snapshot_sha256'],Json(result),str(batch_id)))
    return {'written':written,'existing_verified':existing_verified,'batch_id':str(batch_id),
            'source_contributions':'not_recorded','replayed':False,**stats}
