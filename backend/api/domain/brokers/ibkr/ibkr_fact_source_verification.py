"""Isolated read-only verification of linked IBKR facts against exact XML.

Consistency is not authentication, human approval or permission to publish.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from domain.brokers.ibkr.ibkr_cash_events import _business_date, normalize_cash_event
from domain.brokers.ibkr.ibkr_daily_close import _document
from domain.brokers.ibkr.ibkr_report_batch import NAMESPACE
from domain.imports.source_fact_evidence import _reviewed_snapshot
from domain.imports import isolation
from domain.imports.unified_source import _json_value
from domain.investments.investment_trade_fact import _is_fx_pair_symbol


class EvidenceUnavailable(ValueError):
    pass


class EvidenceCache:
    """Request-owned cache, scoped to one connection and one PostgreSQL xid.

    Call clear() at the end of a request to discard sensitive report material.
    Database source snapshots are cached only in read-only repeatable-read
    transactions. Writable transactions cache XML parsing but re-read sources
    every time, including after rollback to a savepoint. Recovery replicas
    never use the cache because they cannot allocate a transaction ID. This is
    internal request state, not an input from a client or a thread-safe cache.
    """
    def __init__(self):
        self.clear()

    def clear(self):
        self.connection = None
        self.identity = None
        self.sources = {}
        self.batches = {}
        self.associations = {}
        self.files = {}
        self.documents = {}
        self.document_bytes = {}
        self.node_indexes = {}
        self.statement_checks = {}

    def bind(self, cur, recovery):
        if recovery:
            self.clear()
            return False
        cur.execute('SELECT pg_backend_pid() AS backend, pg_current_xact_id()::text AS xid')
        state=_row(cur,cur.fetchone())
        identity=(state['backend'],state['xid'])
        if self.connection is not cur.connection or self.identity != identity:
            self.clear()
            self.connection=cur.connection
            self.identity=identity
        return True


def _require(condition, reason):
    if not condition:
        raise EvidenceUnavailable(reason)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False, default=_json_value).encode('utf-8')).hexdigest()


def _row(cur, value):
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    return dict(zip((column.name for column in cur.description), value))


def _batch_links(cur, batch_id, records):
    """Compare every stored fact association to the publication record."""
    _require(isinstance(records,list), 'missing_publication_records')
    expected=set()
    for record in records:
        _require(isinstance(record,dict) and set(record)=={'table','id','row_id'}, 'invalid_publication_record')
        _require(record['table'] in {'pams_cashflow_fact','pams_investment_trade_fact','pams_holdings_snapshot'},
                 'invalid_publication_record')
        expected.add((record['table'],str(record['id']),str(record['row_id'])))
    _require(len(expected)==len(records), 'duplicate_publication_record')
    actual=set()
    for table,column in (('pams_cashflow_fact','cashflow_id'),('pams_investment_trade_fact','trade_id'),
                         ('pams_holdings_snapshot','id')):
        cur.execute(f"SELECT fact.{column} AS fact_id,fact.source_item_id FROM public.{table} fact "
                    "JOIN public.source_items source ON source.source_item_id=fact.source_item_id "
                    "WHERE source.batch_id=%s",(str(batch_id),))
        for row in cur.fetchall():
            value=_row(cur,row)
            actual.add((table,str(value['fact_id']),str(value['source_item_id'])))
    _require(actual==expected,'publication_association_mismatch')
    return expected


def _verified_batch(cur, batch, *, lock, cache, report_file_id=None, report_origin=False):
    batch_id=str(batch['batch_id'])
    _require(batch['status']=='published' and batch['account_code']=='ibkr' and batch['institution']=='ibkr'
             and batch['acquisition_method']=='api','source_scope_mismatch')
    _require(_reviewed_snapshot(cur,batch,batch_id,locking=lock,
        snapshot_cache=cache.batches if cache is not None else {}),'source_snapshot_hash_mismatch')
    if cache is not None and batch_id in cache.associations:
        return cache.associations[batch_id]
    cur.execute('SELECT count(*) AS n FROM public.source_files WHERE batch_id=%s',(batch_id,))
    _require(_row(cur,cur.fetchone())['n']==(1 if report_file_id is not None and not report_origin else 0),'ambiguous_report_files')
    if report_file_id is not None:
        cur.execute("SELECT count(*) AS n FROM public.source_items WHERE batch_id=%s AND " +
            ("(origin_file_id IS DISTINCT FROM %s::uuid OR file_id IS NOT NULL)" if report_origin else
             "(file_id IS DISTINCT FROM %s::uuid OR origin_file_id IS NOT NULL)"),(batch_id,str(report_file_id)))
        _require(_row(cur,cur.fetchone())['n']==0,'report_item_file_mismatch')
    else:
        cur.execute('SELECT count(*) AS n FROM public.source_items WHERE batch_id=%s',(batch_id,))
        _require(_row(cur,cur.fetchone())['n']==1,'ambiguous_supplement_items')
    result=batch.get('published_result') or {}
    _require(isinstance(result,dict),'missing_publication_records')
    associations=_batch_links(cur,batch_id,result.get('records'))
    if cache is not None and len(cache.associations)<256:
        cache.associations[batch_id]=associations
    return associations


def _source(cur, fact, *, lock, cache):
    cache_key=(str(fact['source_item_id']),fact['source_table'],fact['source_key'])
    if cache is not None and cache_key in cache.sources:
        return cache.sources[cache_key]
    suffix=' FOR SHARE' if lock else ''
    cur.execute("SELECT * FROM public.source_items WHERE source_item_id=%s",(fact['source_item_id'],))
    item=_row(cur,cur.fetchone())
    _require(item is not None and item['review_status']=='confirmed','source_not_confirmed')
    cur.execute("SELECT * FROM public.source_batches WHERE batch_id=%s"+suffix,(item['batch_id'],))
    batch=_row(cur,cur.fetchone())
    cur.execute("SELECT * FROM public.source_items WHERE source_item_id=%s"+suffix,(fact['source_item_id'],))
    _require(_row(cur,cur.fetchone())==item,'source_changed_during_verification')
    _require(batch is not None and batch['workflow'] in {'ibkr_report','ibkr_cash_report','ibkr_cash_supplement'},'source_original_missing')
    table,key=fact['source_table'],fact['source_key']
    _require(table in {'ibkr_flex_trade','ibkr_flex_cash_transaction'} and isinstance(key,str) and key,
             'unsupported_report_fact')
    raw=item['raw_fields']; parsed=item['parsed_fields']
    _require(isinstance(raw,dict) and isinstance(parsed,dict) and raw.get('source_key')==key and
        isinstance(raw.get('official_attributes'),dict),'source_record_mismatch')
    account=raw.get('source_account_id'); digest=raw.get('report_sha256'); event_day=raw.get('business_date')
    _require(isinstance(account,str) and account and isinstance(digest,str) and
        re.fullmatch(r'[0-9a-f]{64}',digest) and isinstance(event_day,str),'invalid_report_metadata')
    event_date=date.fromisoformat(event_day)
    supplement=batch['workflow']=='ibkr_cash_supplement'
    ordinal=None
    if supplement:
        _require(table=='ibkr_flex_cash_transaction' and batch['business_type']=='cash' and raw.get('kind')=='cash_supplement'
            and item['file_id'] is None and item.get('origin_file_id') is not None, 'source_scope_mismatch')
        ordinal=raw.get('ordinal')
        _require(isinstance(ordinal,int) and not isinstance(ordinal,bool) and ordinal>0,'invalid_cash_ordinal')
        original_id=UUID(raw['original_batch_id']); file_id=UUID(raw['original_file_id'])
        _require(str(item['origin_file_id'])==str(file_id) and raw.get('original_item_id')==str(uuid5(original_id,'scope')),
                 'source_origin_identity_mismatch')
        _require(batch['date_from']==batch['date_to']==event_date and
            str(batch['batch_id'])==str(uuid5(original_id,'cash-supplement:'+key)), 'source_date_mismatch')
        expected_item=uuid5(UUID(str(batch['batch_id'])),'cash:'+key)
        _require(parsed.get('observation')=='cash_supplement','source_association_unproven')
        _require(isinstance(parsed.get('dated_fx_rate'),str) and bool(parsed['dated_fx_rate'].strip()),
                 'supplement_fx_missing')
        _require((batch.get('published_result') or {}).get('origin_file_id')==str(file_id),'source_origin_identity_mismatch')
        cur.execute("SELECT * FROM public.source_batches WHERE batch_id=%s"+suffix,(str(original_id),))
        original=_row(cur,cur.fetchone())
        _require(original is not None and original['workflow'] in {'ibkr_report','ibkr_cash_report'}
                 and original['business_type']==('cash' if original['workflow']=='ibkr_cash_report' else 'trade'),
                 'source_original_missing')
    else:
        _require(batch['business_type']==('cash' if batch['workflow']=='ibkr_cash_report' else 'trade')
                 and (batch['workflow']!='ibkr_cash_report' or table=='ibkr_flex_cash_transaction')
                 and bool(item['file_id'])!=bool(item.get('origin_file_id')),
                 'source_original_missing')
        expected_kind='trade' if table=='ibkr_flex_trade' else 'cash_event'
        _require(raw.get('kind')==expected_kind and raw.get('source_table')==table,'source_record_mismatch')
        original=batch; original_id=UUID(str(batch['batch_id'])); file_id=item['file_id'] or item['origin_file_id']
        expected_item=uuid5(original_id,('trade:' if table=='ibkr_flex_trade' else 'cash:')+key)
        _require(parsed.get('observation')=='created','source_association_unproven')
    _require(item['source_uid']==key and str(item['source_item_id'])==str(expected_item) and item['parse_method']=='api'
        and item['record_hash']==_digest(raw) and parsed.get('fact_id')==fact['trade_id'], 'source_identity_mismatch')
    if table=='ibkr_flex_trade':
        _require(item['external_id']==key, 'source_identity_mismatch')
    else:
        attributes={str(k).lower():str(v).strip() for k,v in raw['official_attributes'].items()}
        _require(len(attributes)==len(raw['official_attributes']) and
            item['external_id']==attributes.get('transactionid'), 'source_identity_mismatch')
    _require(original['date_to'] is not None and original['date_from'] is not None and
        event_date<=original['date_to'] and (supplement or original['date_from']<=event_date),
        'source_date_mismatch')
    report_day=original['date_to'].isoformat()
    _require(str(original_id)==str(uuid5(NAMESPACE,('cash-batch:' if original['workflow']=='ibkr_cash_report' else 'batch:')+account+':'+report_day+':'+digest)),
             'source_origin_identity_mismatch')
    if table=='ibkr_flex_trade':
        _require(event_day==report_day,'source_date_mismatch')
    cur.execute("SELECT * FROM public.source_items WHERE batch_id=%s AND source_uid='scope'"+suffix,(str(original_id),))
    scope=_row(cur,cur.fetchone())
    _require(scope is not None and bool(scope['file_id'])!=bool(scope.get('origin_file_id')) and
        str(scope['file_id'] or scope['origin_file_id'])==str(file_id),'source_origin_identity_mismatch')
    report_origin=bool(scope.get('origin_file_id'))
    own_associations=_verified_batch(cur,batch,lock=lock,cache=cache,report_file_id=None if supplement else file_id,
                                     report_origin=False if supplement else report_origin)
    _require(('pams_investment_trade_fact',str(fact['trade_id']),str(expected_item)) in own_associations,
             'source_association_unproven')
    if supplement:
        _verified_batch(cur,original,lock=lock,cache=cache,report_file_id=file_id,report_origin=report_origin)
    file_key=(str(original_id),str(file_id))
    if cache is not None and file_key in cache.files:
        content=cache.files[file_key]
    else:
        cur.execute("SELECT * FROM public.source_files WHERE file_id=%s"+suffix,(str(file_id),))
        file=_row(cur,cur.fetchone())
        _require(file is not None and file['file_format']=='xml' and file['content_type']=='application/xml',
                 'source_original_missing')
        owner_id=UUID(str(file['batch_id']))
        if report_origin:
            cur.execute("SELECT * FROM public.source_batches WHERE batch_id=%s"+suffix,(str(owner_id),))
            owner=_row(cur,cur.fetchone())
            cur.execute("SELECT * FROM public.source_items WHERE batch_id=%s AND source_uid='scope'"+suffix,(str(owner_id),))
            owner_scope=_row(cur,cur.fetchone())
            owner_raw=owner_scope['raw_fields'] if owner_scope else None
            _require(owner is not None and owner['workflow'] in {'ibkr_holding_review','ibkr_report','ibkr_cash_report'}
                and owner['business_type']=={'ibkr_holding_review':'holding','ibkr_report':'trade','ibkr_cash_report':'cash'}[owner['workflow']]
                and owner['date_to']==original['date_to'] and owner_id!=original_id
                and str(file_id)==str(uuid5(owner_id,'raw-xml')) and owner_scope is not None
                and owner_scope['review_status']=='confirmed' and owner_scope['parse_method']=='api'
                and str(owner_scope['file_id'])==str(file_id) and isinstance(owner_raw,dict)
                and owner_raw.get('kind')==('scope' if owner['workflow']=='ibkr_holding_review' else 'report')
                and owner_raw.get('source_account_id')==account
                and (owner_raw.get('source_sha256') or owner_raw.get('sha256'))==digest and owner_raw.get('business_date')==report_day
                and owner_scope['record_hash']==_digest(owner_raw), 'source_origin_identity_mismatch')
            _verified_batch(cur,owner,lock=lock,cache=cache,report_file_id=file_id)
        else:
            _require(owner_id==original_id and str(file_id)==str(uuid5(original_id,'raw-xml')),
                     'source_origin_identity_mismatch')
        content=bytes(file['content'])
        _require(len(content)<=32*1024*1024 and sha256(content).hexdigest()==digest and
            str(file['sha256']).strip()==digest,'source_file_hash_mismatch')
        if cache is not None and len(cache.files)<2:
            cache.files[file_key]=content
    scope_raw=scope['raw_fields'] if scope else None
    _require(isinstance(scope_raw,dict) and set(scope_raw)=={'kind','sha256','source_account_id','business_date',
             'reported_cash_count','recorded_cash_count','cash_pending_count'} and scope_raw['kind']=='report' and
        scope_raw['sha256']==digest and scope_raw['source_account_id']==account and scope_raw['business_date']==report_day and
        all(type(scope_raw[k]) is int and scope_raw[k]>=0 for k in ('reported_cash_count','recorded_cash_count','cash_pending_count')),
        'source_report_scope_mismatch')
    _require(str(scope['source_item_id'])==str(uuid5(original_id,'scope')) and
        str(scope['file_id'] or scope['origin_file_id'])==str(file_id) and
        scope['parse_method']=='api' and scope['review_status']=='confirmed' and scope['record_hash']==_digest(scope_raw),
        'source_report_scope_mismatch')
    normalized={'source_table':table,'source_key':key,'source_account_id':account,
        'official_attributes':raw['official_attributes'],'cash_ordinal':ordinal,
        'reported_cash_count':scope_raw['reported_cash_count'],
        'supplement_fx_rate': parsed.get('dated_fx_rate') if supplement else None,
        'fx_evidence':parsed.get('fx_evidence') if table=='ibkr_flex_cash_transaction' else None,
        'report_evidence':{'broker_source':'ibkr_flex','source_sha256':digest,'confirmed_day':report_day,'evidence_role':'source_report'}}
    out=(normalized,content)
    if cache is not None and len(cache.sources)<256:
        cache.sources[cache_key]=out
    return out

def _scope(evidence, node, account):
    while node is not None:
        attrs = {str(k).lower(): str(v).strip() for k,v in node.attrib.items()}
        _require(len(attrs)==len(node.attrib) and not attrs.get('model'), 'ambiguous_account_scope')
        _require(all(attrs.get(key) in (None,'',account) for key in ('accountid','acctid')),
                 'source_account_mismatch')
        node=evidence.parents.get(node)


def _report_node(raw, report_bytes, table, cache):
    _require(isinstance(report_bytes,bytes) and 0 < len(report_bytes) <= 32*1024*1024,'missing_report_bytes')
    info=raw['report_evidence']
    digest=info.get('source_sha256')
    _require(isinstance(digest,str) and re.fullmatch(r'[0-9a-f]{64}',digest), 'report_hash_mismatch')
    if cache is None or cache.document_bytes.get(digest) is not report_bytes:
        _require(sha256(report_bytes).hexdigest()==digest, 'report_hash_mismatch')
        if cache is not None and len(cache.document_bytes)<2:
            cache.document_bytes[digest]=report_bytes
    account=raw.get('source_account_id')
    _require(isinstance(account,str) and account and account==account.strip(), 'missing_source_account')
    try:
        day=date.fromisoformat(info['confirmed_day'])
    except (UnicodeError,KeyError,TypeError,ValueError) as exc:
        raise EvidenceUnavailable('invalid_report_metadata') from exc
    _require(info.get('broker_source') in {'ibkr_flex','ibkr_published_daily_close'}, 'unsupported_report_source')
    if table=='ibkr_flex_trade':
        _require(info.get('evidence_role')=='source_report', 'source_not_original_report')
    document_key=(digest,account)
    evidence=cache.documents.get(document_key) if cache is not None else None
    error=None
    if evidence is None:
        try:
            text=report_bytes.decode('utf-8')
        except UnicodeError as exc:
            raise EvidenceUnavailable('invalid_report_metadata') from exc
        evidence,_,error=_document(text,account,digest)
        if evidence is not None and cache is not None and len(cache.documents)<2:
            cache.documents[document_key]=evidence
    _require(evidence is not None and error is None, 'invalid_report_xml')
    tag='Trade' if table=='ibkr_flex_trade' else 'CashTransaction'
    section='Trades' if tag=='Trade' else 'CashTransactions'
    official=raw['official_attributes']
    lower={str(k).lower():str(v).strip() for k,v in official.items()}
    _require(len(lower)==len(official), 'ambiguous_row_attributes')
    ids=('tradeid','transactionid','ibexecid') if tag=='Trade' else ('transactionid',)
    selected=next(((key,lower[key]) for key in ids if lower.get(key)),None)
    _require(selected is not None and selected[1].lower() not in {'0','none','null','unknown'},'missing_source_identity')
    index_key=(document_key,tag)
    index=cache.node_indexes.get(index_key) if cache is not None else None
    if index is None:
        by_identity={}
        parent_statement={}
        statement_days={statement:statement_day for statement_day, statements_on_day in evidence.by_day.items()
                        for statement in statements_on_day}
        for statement in evidence.statements:
            for node in statement.iter():
                if node.tag.rsplit('}',1)[-1]!=tag:
                    continue
                attrs={str(k).lower():str(v).strip() for k,v in node.attrib.items()}
                _require(len(attrs)==len(node.attrib), 'ambiguous_row_attributes')
                for identity_key in ids:
                    if attrs.get(identity_key):
                        by_identity.setdefault((identity_key,attrs[identity_key]),[]).append(node)
                parent_statement.setdefault(node,[]).append((statement_days[statement],statement))
        root=evidence.statements[0]
        while root in evidence.parents:
            root=evidence.parents[root]
        report_statements=[n for n in root.iter() if n.tag.rsplit('}',1)[-1].lower()=='flexstatement']
        cash_nodes=[n for statement_item in report_statements for n in statement_item.iter()
                    if n.tag.rsplit('}',1)[-1].lower()=='cashtransaction']
        index=(by_identity,parent_statement,cash_nodes)
        if cache is not None and len(cache.node_indexes)<4:
            cache.node_indexes[index_key]=index
    by_identity,parent_statement,cash_nodes=index
    matches=by_identity.get(selected,[])
    _require(len(matches)==1 and dict(matches[0].attrib)==official, 'source_identity_not_unique')
    if tag=='Trade':
        statements=evidence.by_day.get(day,[])
    else:
        candidates=parent_statement.get(matches[0],[])
        _require(len(candidates)==1, 'ambiguous_statement')
        statement_day, candidate=candidates[0]
        _require(statement_day<=day and len(evidence.by_day[statement_day])==1, 'ambiguous_statement')
        statements=[candidate]
    _require(len(statements)==1, 'ambiguous_statement')
    statement=statements[0]
    statement_key=(index_key,statement)
    nodes=cache.statement_checks.get(statement_key) if cache is not None else None
    if nodes is None:
        _scope(evidence,statement,account)
        sections=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]==section]
        _require(len(sections)<=1, 'ambiguous_report_section')
        nodes={n for n in statement.iter() if n.tag.rsplit('}',1)[-1]==tag}
        if sections:
            _scope(evidence,sections[0],account)
            _require(sections[0].get('levelOfDetail','DETAIL').upper()=='DETAIL','report_section_not_detail')
            members=set(sections[0].iter())
            _require(all(n in members for n in nodes), 'report_row_outside_section')
        else:
            _require(all(evidence.parents.get(n) is statement for n in nodes), 'report_row_outside_statement')
        if cache is not None and len(cache.statement_checks)<64:
            cache.statement_checks[statement_key]=nodes
    _require(matches[0] in nodes, 'source_identity_not_unique')
    _scope(evidence,matches[0],account)
    if 'reported_cash_count' in raw:
        _require(raw['reported_cash_count']==len(cash_nodes),'report_cash_count_mismatch')
    ordinal=raw.get('cash_ordinal')
    if ordinal is not None:
        _require(tag=='CashTransaction' and ordinal<=len(cash_nodes) and cash_nodes[ordinal-1] is matches[0],
                 'cash_original_ordinal_mismatch')
    return lower, account, day, digest, selected


def _number(attrs, keys):
    values=[attrs[key] for key in keys if attrs.get(key) not in (None,'')]
    if not values:
        return None
    _require(all(re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',v) for v in values),'invalid_source_amount')
    try:
        amounts=[Decimal(v) for v in values]
        _require(all(v.is_finite() and abs(v)<Decimal('1e18') and v==v.quantize(Decimal('0.0000000001'))
                     for v in amounts) and len(set(amounts))==1,'ambiguous_source_amount')
    except InvalidOperation as exc:
        raise EvidenceUnavailable('invalid_source_amount') from exc
    return amounts[0]


def _equal(fact, field, expected, unknown):
    if expected is None or fact.get(field) is None:
        unknown.add(field)
        return
    actual=fact.get(field)
    _require(actual is not None and actual==expected, 'fact_field_mismatch:'+field)


def _trade(fact, attrs, account, report_day, selected, unknown):
    identity=json.dumps(['ibkr',account,selected[0],selected[1]],separators=(',',':'))
    key='ibkr_trade:' + sha256(identity.encode()).hexdigest()
    _require(fact.get('source_key')==key and fact.get('source_id')==key, 'trade_identity_mismatch')
    _require(attrs.get('tradedate'), 'missing_trade_date')
    trade_day=_business_date(attrs['tradedate'])
    _require(trade_day==fact['trade_date'] and trade_day==report_day,'trade_date_mismatch')
    symbol=attrs.get('symbol') or None
    currency=attrs.get('currency') or None
    side=(attrs.get('buysell') or '').lower() or None
    _require(currency is None or re.fullmatch(r'[A-Z]{3}',currency), 'invalid_source_currency')
    _require(side in {None,'buy','sell'}, 'invalid_source_side')
    is_fx = _is_fx_pair_symbol(symbol)
    if is_fx:
        _require(attrs.get('assetcategory') == 'CASH', 'ambiguous_currency_pair_asset')
        unknown.add('fx_conversion_legs')
    fact_side = 'fx_conversion' if is_fx else side
    for field,value in (('symbol',symbol),('currency',currency),('side',fact_side),('trade_type',fact_side)):
        _equal(fact,field,value,unknown)
    quantity=_number(attrs,('quantity',)); price=_number(attrs,('tradeprice',))
    gross=_number(attrs,('trademoney',)); fee=_number(attrs,('ibcommission',))
    tax=_number(attrs,('taxamount','tax')); net=_number(attrs,('netcash',))
    multiplier=_number(attrs,('multiplier',))
    if quantity is not None and side is not None:
        _require((quantity>0 and side=='buy') or (quantity<0 and side=='sell'),'quantity_direction_mismatch')
    if price is not None:
        _require(price>0,'invalid_source_price')
    if multiplier is not None:
        _require(multiplier>0,'invalid_contract_multiplier')
    for field,value in (('quantity',quantity),('price',price),('gross_amount',abs(gross) if gross is not None else None),
                        ('fee_amount',abs(fee) if fee is not None else None),
                        ('tax_amount',abs(tax) if tax is not None else None),('net_cash',net)):
        _equal(fact,field,value,unknown)
    if gross is not None and quantity is not None and price is not None and multiplier is not None:
        _require(abs(gross)==abs(quantity)*price*multiplier,'gross_amount_mismatch')
    else:
        unknown.add('gross_reconciliation')
    fee_currency=attrs.get('ibcommissioncurrency') or None
    if fee is not None:
        if fee_currency is not None:
            _require(re.fullmatch(r'[A-Z]{3}',fee_currency), 'invalid_fee_currency')
            _equal(fact,'fee_currency',fee_currency,unknown)
        elif fee != 0 or fact.get('fee_currency') is not None:
            unknown.add('fee_currency')
    same_fee_unit = fee is not None and (fee == 0 or (currency is not None and fee_currency==currency))
    if not is_fx and all(x is not None for x in (gross,fee,tax,net)) and side is not None and same_fee_unit:
        expected=-abs(gross)-abs(fee)-abs(tax) if side=='buy' else abs(gross)-abs(fee)-abs(tax)
        _require(net==expected,'net_cash_mismatch')
    else:
        unknown.add('net_reconciliation')
    if tax is not None and tax != 0:
        _require(not attrs.get('taxcurrency') or attrs['taxcurrency']==currency,'tax_currency_mismatch')
        if not attrs.get('taxcurrency'):
            unknown.add('tax_currency')
    realized=_number(attrs,('fifopnlrealized',))
    _equal(fact,'realized_pnl',realized,unknown)
    _equal(fact,'market',attrs.get('assetcategory') or None,unknown)
    _equal(fact,'name',attrs.get('description') or None,unknown)
    if attrs.get('settledate'):
        _equal(fact,'settle_date',_business_date(attrs['settledate']),unknown)
    else:
        unknown.add('settle_date')


def _cash(fact, raw, account, report_day, unknown):
    event=normalize_cash_event({'raw':raw,'statement_account_id':account},account_code='ibkr',
                               cutoff=date.min,statement_as_of=report_day)
    _require(event is not None,'missing_cash_event')
    for field in ('trade_date','account_code','symbol','name','currency','side','gross_amount','net_cash','source_key','source_id'):
        _equal(fact,field,event.get(field),unknown)
    lower={str(key).lower():str(value).strip() for key,value in raw.items()}
    if not lower.get('symbol'):
        unknown.add('symbol')
    if not lower.get('description'):
        unknown.add('name')
    _equal(fact,'trade_type',event['side'],unknown)
    if event['settle_date'] is not None:
        _equal(fact,'settle_date',event['settle_date'],unknown)
    else:
        unknown.add('settle_date')
    if event['side']=='fee':
        _equal(fact,'fee_amount',abs(event['net_cash']),unknown)
        _equal(fact,'fee_currency',event['currency'],unknown)
    else:
        unknown.add('fee_amount')
    if event['side']=='tax':
        _equal(fact,'tax_amount',abs(event['net_cash']),unknown)
    else:
        unknown.add('tax_amount')
    if event['side'] in {'dividend','interest','coupon','fee','tax'}:
        _equal(fact,'realized_pnl',event['net_cash'],unknown)
    elif fact.get('realized_pnl') is not None:
        unknown.add('realized_pnl')
    if fact.get('cashflow_id') is not None:
        unknown.add('cashflow_association')


def _cash_fx(cur,fact,source,unknown):
    """Verify only the explicit event-day CNY rate and derived fact fields."""
    currency=fact.get('currency')
    rate=None
    rate_source=None
    if currency=='CNY':
        rate=Decimal('1')
    elif currency:
        cur.execute("""SELECT rate,source FROM public.pams_fx_rates_daily WHERE base_currency='CNY'
            AND quote_currency=%s AND as_of=%s""",(currency,fact['trade_date']))
        rows=[_row(cur,row) for row in cur.fetchall()]
        if len(rows)==1 and rows[0]['rate'] is not None:
            rate=Decimal(str(rows[0]['rate']))
            rate_source=rows[0]['source']
            if not rate.is_finite() or rate<=0 or rate>=Decimal('1e10'):
                rate=None
    stated=source.get('supplement_fx_rate')
    if stated is not None:
        try:
            stated=Decimal(str(stated))
        except (InvalidOperation,ValueError) as exc:
            raise EvidenceUnavailable('invalid_supplement_fx') from exc
        _require(stated.is_finite() and stated>0 and fact.get('fx_rate_to_cny')==stated,
                 'supplement_fx_mismatch')
    if rate is None:
        unknown.update(('fx_rate_to_cny','net_cash_cny','realized_pnl_cny','fx_evidence'))
        return
    evidence=source.get('fx_evidence')
    if evidence is None:
        unknown.add('fx_evidence')
    else:
        _require(isinstance(evidence,dict) and set(evidence)=={'as_of','base_currency','quote_currency','rate','source','kind'},
                 'invalid_fx_evidence')
        try:
            stated_rate=Decimal(str(evidence['rate']))
        except (InvalidOperation,ValueError) as exc:
            raise EvidenceUnavailable('invalid_fx_evidence') from exc
        kind='identity' if currency=='CNY' else 'daily_reference_not_broker_settlement'
        _require(stated_rate==rate and evidence['as_of']==fact['trade_date'].isoformat() and
            evidence['base_currency']=='CNY' and evidence['quote_currency']==currency and
            evidence['kind']==kind and evidence['source']==rate_source,'fx_evidence_mismatch')
        if currency!='CNY' and (not isinstance(rate_source,str) or not rate_source.strip()):
            unknown.add('fx_source')
    _equal(fact,'fx_rate_to_cny',rate,unknown)
    if fact.get('net_cash') is not None:
        expected=(fact['net_cash']*rate).quantize(Decimal('0.0000000001'),rounding=ROUND_HALF_UP)
        _equal(fact,'net_cash_cny',expected,unknown)
    else:
        unknown.add('net_cash_cny')
    if fact.get('realized_pnl') is not None:
        expected=(fact['realized_pnl']*rate).quantize(Decimal('0.0000000001'),rounding=ROUND_HALF_UP)
        _equal(fact,'realized_pnl_cny',expected,unknown)
    elif fact.get('realized_pnl_cny') is not None:
        unknown.add('realized_pnl_cny')


def verify_ibkr_fact(cur, *, trade_id: int, report_bytes: bytes | None = None, cache: EvidenceCache | None = None) -> dict:
    """Check one linked fact; caller owns the isolated transaction.

    Unknown fields are explicit; consistent/partial is NOT source authentication,
    approval, a complete report claim or permission to write other facts. A caller
    in a writable transaction must call this before taking source/fact locks.
    A read-only caller must use REPEATABLE READ or SERIALIZABLE.
    """
    if cur.connection.autocommit:
        raise ValueError('IBKR evidence verification requires a transaction')
    if not isinstance(trade_id,int) or isinstance(trade_id,bool) or trade_id<=0:
        raise ValueError('invalid trade id')
    if cache is not None and not isinstance(cache,EvidenceCache):
        raise ValueError('invalid evidence cache')
    try:
        cur.execute("""SELECT current_setting('transaction_read_only')='on' AS readonly,
            current_setting('transaction_isolation') AS isolation_level,pg_is_in_recovery() AS recovery""")
        mode=_row(cur,cur.fetchone())
        readonly=mode['readonly']
        if readonly and mode['isolation_level'] not in {'repeatable read','serializable'}:
            if cache is not None:
                cache.clear()
            raise EvidenceUnavailable('read_only_requires_repeatable_read')
        if not readonly:
            if not isolation.source_enabled(cur):
                raise EvidenceUnavailable('writable_verification_requires_isolation')
            # Match the account -> source -> fact order used by holding writers.
            cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('account-import:ibkr',))
        active_cache=cache if cache is not None and cache.bind(cur,mode['recovery']) else None
        source_cache=active_cache if readonly else None
        # Discover the source without a fact lock. Publishers lock source before
        # fact; keeping the same order avoids a source/fact deadlock. Recheck
        # the complete fact after taking its lock so discovery cannot go stale.
        cur.execute('SELECT * FROM public.pams_investment_trade_fact WHERE trade_id=%s',(trade_id,))
        fact=_row(cur,cur.fetchone())
        _require(fact is not None and fact.get('source_item_id') is not None,'missing_fact_source')
        _require(fact['account_code']=='ibkr','fact_account_mismatch')
        raw,stored_bytes=_source(cur,fact,lock=not readonly,cache=source_cache)
        if report_bytes is not None:
            _require(isinstance(report_bytes,bytes) and report_bytes==stored_bytes,'report_bytes_mismatch')
        cur.execute('SELECT * FROM public.pams_investment_trade_fact WHERE trade_id=%s' +
                    (' FOR SHARE' if not readonly else ''),(trade_id,))
        current=_row(cur,cur.fetchone())
        _require(current is not None and current==fact,'fact_changed_during_verification')
        attrs,account,report_day,digest,selected=_report_node(raw,stored_bytes,fact['source_table'],active_cache)
        unknown=set()
        if fact['source_table']=='ibkr_flex_trade':
            _trade(fact,attrs,account,report_day,selected,unknown)
        else:
            _cash(fact,raw['official_attributes'],account,report_day,unknown)
            _cash_fx(cur,fact,raw,unknown)
        not_checked=['account_ownership','broker_authentication','cashflow_association','conid_asset_binding']
        if fact['source_table']=='ibkr_flex_trade':
            not_checked.extend(['fx_rate_to_cny','net_cash_cny','realized_pnl_cny'])
        return {'status':'partial' if unknown else 'consistent','unknown_fields':sorted(unknown),
                'report_sha256':digest,'source_item_id':str(fact['source_item_id']),
                'not_checked':not_checked,
                'source_authenticated':False,'authorizes_publication':False}
    except (EvidenceUnavailable, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        reason=str(exc) if isinstance(exc,EvidenceUnavailable) else 'invalid_evidence'
        return {'status':'unavailable','reason':reason,'unknown_fields':[],'source_authenticated':False,
                'authorizes_publication':False}
