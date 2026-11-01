"""Revalidate source-first manual/template/OCR facts against reviewed evidence.

The result proves consistency with saved evidence, not real-world identity or
source authenticity. Unsupported providers or incomplete evidence stay unknown.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any

from domain.assets.alias_policy import is_identity_alias
from domain.imports.unified_source import _json_value
from domain.ledger.cashflow_fact import make_source_hash


def _rows(cur):
    names = [col.name if hasattr(col, 'name') else col[0] for col in cur.description]
    return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in cur.fetchall()]


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             default=_json_value, allow_nan=False).encode('utf-8')).hexdigest()


def _number(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _asset(cur, fields, fact):
    symbol, identifier = fields.get('symbol'), fields.get('asset_id')
    if not symbol and not identifier:
        return False
    if identifier:
        cur.execute("SELECT asset_id,primary_symbol FROM pams_asset_master WHERE asset_id=%s AND status='active' AND currency='CNY'", (identifier,))
        candidates = _rows(cur)
    else:
        cur.execute("""SELECT DISTINCT m.asset_id,m.primary_symbol FROM pams_asset_master m
            LEFT JOIN pams_asset_alias a ON a.asset_id=m.asset_id
            WHERE m.status='active' AND m.currency='CNY' AND (m.primary_symbol=%s OR a.alias_value=%s)""", (symbol,symbol))
        candidates = _rows(cur)
        cur.execute("SELECT asset_id,alias_type,alias_value FROM pams_asset_alias WHERE alias_value=%s", (symbol,))
        allowed = {r['asset_id'] for r in _rows(cur) if is_identity_alias(r['asset_id'],r['alias_type'],r['alias_value'])}
        candidates = [r for r in candidates if r['primary_symbol'] == symbol or r['asset_id'] in allowed]
    return (len(candidates)==1 and candidates[0]['asset_id']==fact.get('asset_id')
            and candidates[0]['primary_symbol']==fact.get('symbol'))


def _reviewed_snapshot(cur, batch, batch_id, *, locking, snapshot_cache):
    """Rebuild pre-publication snapshot, including immutable source file hashes."""
    cache_key=(batch_id,str(batch['published_source_hash']).strip())
    if cache_key in snapshot_cache:
        return snapshot_cache[cache_key]
    share = ' FOR SHARE' if locking else ''
    cur.execute("""SELECT file_id,filename,file_format,content_type,sha256,content,current_metadata,review_fingerprint
        FROM source_files WHERE batch_id=%s ORDER BY file_id""" + share, (batch_id,))
    files = _rows(cur)
    for file in files:
        if sha256(bytes(file.pop('content'))).hexdigest() != str(file['sha256']).strip():
            return False
    cur.execute("""SELECT source_item_id,source_order,file_id,origin_file_id,source_uid,external_id,location,
        parse_method,raw_fields,parsed_fields,record_hash,review_status,review_note
        FROM source_items WHERE batch_id=%s ORDER BY source_order""" + share, (batch_id,))
    items = _rows(cur)
    for item in items:
        if _digest(item['raw_fields']) != str(item['record_hash']).strip():
            return False
    blockers = []
    if not any(item['review_status'] == 'confirmed' for item in items):
        blockers.append('no_confirmed_items')
    if any(item['review_status'] == 'pending' for item in items):
        blockers.append('pending_items')
    if blockers:
        return False
    base = {k: batch[k] for k in ('batch_id','account_code','business_type','workflow','institution',
            'acquisition_method','date_from','date_to','completeness','scope_attestation','status','revision')}
    for original_status in ('draft','review'):
        base['status'] = original_status
        if _digest({'batch':base,'files':files,'items':items,'blockers':blockers}) == str(batch['published_source_hash']).strip():
            snapshot_cache[cache_key]=True
            return True
    snapshot_cache[cache_key]=False
    return False


def _publication_values_match(table, fact, record):
    after=record.get('after')
    if not isinstance(after,dict):
        return False
    fields: tuple[str, ...]
    if table == 'pams_investment_trade_fact':
        fields = ('account_code','trade_date','currency','trade_type','side','symbol','asset_id',
                  'quantity','price','gross_amount','fee_amount','tax_amount','net_cash')
        numbers = {'quantity','price','gross_amount','fee_amount','tax_amount','net_cash'}
    else:
        fields = ('account_code','counter_account_code','event_date','currency','amount','cashflow_type')
        numbers = {'amount'}
    for key in fields:
        if key not in after:
            return False
        left, right = after[key], fact.get(key)
        if left is None or right is None:
            if left is not None or right is not None:
                return False
        elif key in numbers:
            if _number(left) is None or _number(left) != _number(right):
                return False
        elif str(left) != str(right):
            return False
    return True


class _LockedNativeReplay:
    """A source proof held only during a single, lock-protected replay call."""

    def __init__(self, cur: Any, batch: dict[str, Any]):
        if cur.connection.autocommit:
            raise ValueError('replay source proof requires a transaction')
        self.cur = cur
        self.batch = batch
        self.batch_id = str(batch['batch_id'])
        self.cache: dict = {}
        # Hold the parent lock even when called independently; child locks are
        # acquired while rebuilding the published snapshot below.
        cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=%s FOR UPDATE', (self.batch_id,))
        if not cur.fetchone() or not _reviewed_snapshot(cur, batch, self.batch_id, locking=True, snapshot_cache=self.cache):
            raise ValueError('published source evidence is unavailable')
        self.active = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.active = False
        self.cache.clear()


def verify_native_fact(cur: Any, *, table: str, fact_id: int, _snapshot_cache: dict | None = None,
                       _replay_context: _LockedNativeReplay | None = None) -> dict[str, Any]:
    """Read-only consistency proof for one native fact; caller owns transaction.

    `verified` means the reviewed row, immutable source snapshot, publisher
    result and business fields agree. It does not authenticate any human, bank,
    broker or external file. Other workflows return `unknown`.
    """
    if table not in {'pams_investment_trade_fact','pams_cashflow_fact'} or not isinstance(fact_id,int) or isinstance(fact_id,bool) or fact_id <= 0:
        raise ValueError('invalid fact identity')
    if cur.connection.autocommit:
        return {'status':'unknown','reason':'transaction_required'}
    cur.execute("""SELECT current_setting('transaction_read_only') AS readonly,
        current_setting('transaction_isolation') AS isolation,pg_is_in_recovery() AS recovering""")
    mode = cur.fetchone()
    readonly = (mode['readonly'] if isinstance(mode,dict) else mode[0]) == 'on'
    isolation = mode['isolation'] if isinstance(mode,dict) else mode[1]
    recovery = mode['recovering'] if isinstance(mode,dict) else mode[2]
    if readonly and isolation not in {'repeatable read','serializable'}:
        return {'status':'unknown','reason':'consistent_snapshot_required'}
    locking=not readonly
    share=' FOR SHARE' if locking else ''
    replay = _replay_context is not None
    if replay and (not isinstance(_replay_context, _LockedNativeReplay) or not _replay_context.active
                   or _replay_context.cur is not cur or not locking or isolation != 'serializable'):
        return {'status':'unknown','reason':'invalid_replay_context'}
    if _replay_context is not None:
        snapshot_cache = _replay_context.cache
    else:
        snapshot_cache = _snapshot_cache if _snapshot_cache is not None else {}
    if locking and not replay:
        # Writable callers may roll back a savepoint within the same XID.
        # Only one _replay call may reuse evidence while holding all source locks.
        snapshot_cache.clear()
    if recovery:
        # Hot standby cannot allocate an XID; verify each batch anew rather
        # than accidentally carry a cache across transactions.
        snapshot_cache.clear()
    else:
        # PostgreSQL assigns an XID even for an explicitly read-only transaction;
        # unlike xact_id_if_assigned(), this identifies each commit/rollback epoch.
        cur.execute('SELECT pg_backend_pid() AS backend, pg_current_xact_id()::text AS xid')
        tx=cur.fetchone()
        tx_key=(tx['backend'],tx['xid']) if isinstance(tx,dict) else (tx[0],tx[1])
        if snapshot_cache.get('_transaction') != tx_key:
            if replay and snapshot_cache.get('_transaction') is not None:
                return {'status':'unknown','reason':'replay_transaction_changed'}
            if not replay:
                snapshot_cache.clear()
            snapshot_cache['_transaction']=tx_key
    pk = 'trade_id' if table == 'pams_investment_trade_fact' else 'cashflow_id'
    if locking:
        cur.execute(f'SELECT * FROM public.{table} WHERE {pk}=%s', (fact_id,))
    else:
        # In an immutable MVCC snapshot, fetch the fact and its source item in
        # one round trip. Keep the fact's native NUMERIC values, rather than
        # converting them through JSON and losing Decimal precision.
        cur.execute(f'''SELECT f.*,i.source_item_id AS _item_id,i.batch_id AS _item_batch_id,
            i.file_id AS _item_file_id,i.source_uid AS _item_source_uid,
            i.parse_method AS _item_parse_method,i.raw_fields AS _item_raw_fields,
            i.parsed_fields AS _item_parsed_fields,i.review_status AS _item_review_status
            FROM public.{table} f LEFT JOIN source_items i ON i.source_item_id=f.source_item_id
            WHERE f.{pk}=%s''', (fact_id,))
    rows = _rows(cur)
    if not rows or not rows[0].get('source_item_id'):
        return {'status':'unknown','reason':'source_link_missing'}
    initial=rows[0]
    snapshot_item = None
    if not locking:
        item_keys = {'source_item_id':'_item_id','batch_id':'_item_batch_id','file_id':'_item_file_id',
                     'source_uid':'_item_source_uid','parse_method':'_item_parse_method',
                     'raw_fields':'_item_raw_fields','parsed_fields':'_item_parsed_fields',
                     'review_status':'_item_review_status'}
        snapshot_item = {name:initial.pop(alias) for name,alias in item_keys.items()}
    accounts = {initial.get('account_code')}
    if table == 'pams_cashflow_fact' and initial.get('counter_account_code'):
        accounts.add(initial['counter_account_code'])
    if not all(isinstance(account,str) and account for account in accounts):
        return {'status':'unknown','reason':'fact_account_missing'}
    if locking:
        for account in sorted(accounts):
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired", ('account-import:' + account,))
            lock_result=cur.fetchone()
            acquired=lock_result.get('acquired') if isinstance(lock_result,dict) else lock_result[0]
            if not acquired:
                return {'status':'unknown','reason':'account_lock_busy'}
    if locking:
        cur.execute('SELECT batch_id FROM source_items WHERE source_item_id=%s', (str(initial['source_item_id']),))
        links=_rows(cur)
        if not links:
            return {'status':'unknown','reason':'source_item_missing'}
        batch_id = str(links[0]['batch_id'])
        item = None
    else:
        item = snapshot_item
        if not item or not item.get('source_item_id'):
            return {'status':'unknown','reason':'source_item_missing'}
        batch_id = str(item['batch_id'])
    if _replay_context is not None and batch_id != _replay_context.batch_id:
        return {'status':'unknown','reason':'replay_batch_changed'}
    batch_cache_key = ('_batch', batch_id)
    batch: Any
    if _replay_context is not None:
        batch = _replay_context.batch
    else:
        batch = snapshot_cache.get(batch_cache_key) if not locking and not recovery else None
    if batch is None:
        cur.execute('SELECT * FROM source_batches WHERE batch_id=%s' + share, (batch_id,))
        batches=_rows(cur)
        if not batches:
            return {'status':'unknown','reason':'source_batch_missing'}
        batch=batches[0]
        if not locking and not recovery:
            snapshot_cache[batch_cache_key] = batch
    if locking:
        cur.execute('SELECT * FROM source_items WHERE source_item_id=%s' + share, (str(initial['source_item_id']),))
        items=_rows(cur)
        if not items:
            return {'status':'unknown','reason':'source_item_changed'}
        item=items[0]
    if item is None:
        return {'status':'unknown','reason':'source_item_missing'}
    if item.get('batch_id') != batch['batch_id']:
        return {'status':'unknown','reason':'source_item_changed'}
    if locking:
        cur.execute(f'SELECT * FROM public.{table} WHERE {pk}=%s' + share, (fact_id,))
        rows=_rows(cur)
        if (not rows or rows[0].get('source_item_id')!=initial['source_item_id']
                or rows[0].get('account_code')!=initial['account_code']
                or (table=='pams_cashflow_fact' and rows[0].get('counter_account_code')!=initial.get('counter_account_code'))):
            return {'status':'unknown','reason':'fact_changed_during_lock'}
        fact=rows[0]
    else:
        fact=initial
    if str(fact.get('review_status') or '').lower() not in {'auto','confirmed','reviewed'}:
        return {'status':'unknown','reason':'fact_not_active'}
    if batch.get('workflow') != 'account_import' or item.get('parse_method') not in {'manual','template','ocr'}:
        return {'status':'unknown','reason':'unsupported_source_workflow'}
    if batch.get('status')!='published' or item.get('review_status')!='confirmed':
        return {'status':'unknown','reason':'source_not_published_or_reviewed'}
    parsed=item.get('parsed_fields') or {}
    fields=parsed.get('fields')
    if not isinstance(fields,dict) or parsed.get('excluded') is not False or (item['parse_method']=='ocr' and parsed.get('acknowledged') is not True):
        return {'status':'unknown','reason':'reviewed_fields_incomplete'}
    if str(item.get('source_uid')) != str(item['source_item_id']):
        return {'status':'inconsistent','reason':'source_identity_mismatch'}
    if item['parse_method'] in {'template','ocr'}:
        source=(item.get('raw_fields') or {}).get('source') or {}
        if not item.get('file_id'):
            return {'status':'unknown','reason':'source_file_missing'}
        file_key = ('_source_file',str(item.get('file_id')))
        source_files = snapshot_cache.get(file_key) if (replay or not locking) and not recovery else None
        if source_files is None:
            cur.execute('SELECT batch_id,sha256 FROM source_files WHERE file_id=%s' + share, (str(item.get('file_id')),))
            source_files=_rows(cur)
            if (replay or not locking) and not recovery:
                snapshot_cache[file_key] = source_files
        if (len(source_files)!=1 or str(source_files[0]['batch_id'])!=str(batch['batch_id'])
                or str(source.get('file_id'))!=str(item.get('file_id'))
                or source.get('file_sha256')!=str(source_files[0]['sha256']).strip()
                or source.get('origin')!=('ocr' if item['parse_method']=='ocr' else 'file')):
            return {'status':'inconsistent','reason':'source_file_link_mismatch'}
    if not _reviewed_snapshot(cur,batch,str(batch['batch_id']),locking=locking,snapshot_cache=snapshot_cache):
        return {'status':'inconsistent','reason':'source_snapshot_mismatch'}
    index_key = ('_publication_index', batch_id)
    publication_index = snapshot_cache.get(index_key) if (replay or not locking) and not recovery else None
    if publication_index is None:
        result=batch.get('published_result') or {}
        records=result.get('records')
        if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
            return {'status':'unknown','reason':'publication_result_missing'}
        publication_index = {}
        for record in records:
            if isinstance(record.get('table'), str):
                publication_index.setdefault((record['table'],str(record.get('id'))), []).append(record)
        if (replay or not locking) and not recovery:
            snapshot_cache[index_key] = publication_index
    expected=publication_index.get((table,str(fact_id)), [])
    if (len(expected)!=1 or str(expected[0].get('row_id'))!=str(item['source_item_id'])
            or fact.get('source_table')!='source_items'):
        return {'status':'inconsistent','reason':'publication_link_mismatch'}
    if table=='pams_cashflow_fact' and not _publication_values_match(table,fact,expected[0]):
        return {'status':'inconsistent','reason':'publication_fact_mismatch'}
    source_id=str(fact.get('source_id') or '')
    key={'source_id':source_id}
    if table=='pams_investment_trade_fact': key['source_key']=fact.get('source_key')
    if fact.get('source_hash')!=make_source_hash('source_items',key):
        return {'status':'inconsistent','reason':'fact_identity_mismatch'}
    currency = fields.get('currency')
    if not currency or currency != fact.get('currency'):
        return {'status':'inconsistent','reason':'currency_mismatch'}
    if currency != 'CNY' and table != 'pams_cashflow_fact':
        return {'status':'unknown','reason':'unsupported_currency'}
    action=fields.get('action')
    if not isinstance(action, str) or not action.strip():
        return {'status':'unknown','reason':'source_action_missing'}
    confirmed=fields.get('confirmed_date') if action in {'subscribe','redeem'} else fields.get('business_date')
    amount=_number(fields.get('net_cash'))
    if not confirmed or amount is None:
        return {'status':'unknown','reason':'source_amount_or_date_missing'}
    derived=expected[0].get('role')=='derived_bank_leg'
    if table=='pams_cashflow_fact':
        rate = Decimal(1)
        if currency != 'CNY':
            if derived:
                return {'status':'unknown','reason':'unsupported_currency'}
            evidence = (fact.get('raw_json') or {}).get('fx_evidence') or {}
            rate = _number(evidence.get('rate'))
            if (evidence.get('basis') != 'daily_market_projection' or evidence.get('base_currency') != 'CNY'
                    or evidence.get('quote_currency') != currency or evidence.get('as_of') != confirmed
                    or not isinstance(evidence.get('source'),str) or not evidence['source'].strip()
                    or rate is None or rate <= 0):
                return {'status':'unknown','reason':'fx_evidence_missing'}
            cur.execute("SELECT to_regclass('public.pams_fx_rates_daily') AS relation")
            if not _rows(cur)[0]['relation']:
                return {'status':'unknown','reason':'fx_evidence_missing'}
            cur.execute("SELECT rate,source FROM pams_fx_rates_daily WHERE as_of=%s AND base_currency='CNY' AND quote_currency=%s" + share, (confirmed,currency))
            fx = _rows(cur)
            if len(fx) != 1 or _number(fx[0].get('rate')) != rate or fx[0].get('source') != evidence['source']:
                return {'status':'unknown','reason':'fx_evidence_changed'}
        expected_account=fields.get('counter_account_code') if derived else batch['account_code']
        valid=(fact.get('account_code')==expected_account and str(fact.get('event_date'))==confirmed
               and _number(fact.get('amount'))==(-amount if derived else amount)
               and _number(fact.get('fx_rate_to_cny'))==rate
               and _number(fact.get('amount_cny'))==((-amount if derived else amount) * rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        if derived:
            valid &= (fact.get('counter_account_code')==batch['account_code'] and action in {'deposit','withdraw'}
                      and fact.get('cashflow_type')==('investment_funding' if action=='deposit' else 'investment_withdrawal'))
            cur.execute("""SELECT trade_id FROM pams_investment_trade_fact WHERE cashflow_id=%s AND source_item_id=%s
                AND account_code=%s AND trade_date=%s AND currency='CNY' AND net_cash=%s""",
                (fact_id,str(item['source_item_id']),batch['account_code'],confirmed,amount))
            linked=_rows(cur)
            valid &= len(linked)==1 and verify_native_fact(cur,table='pams_investment_trade_fact',
                fact_id=linked[0]['trade_id'],_snapshot_cache=snapshot_cache,
                _replay_context=_replay_context)['status']=='verified'
        else:
            valid &= fields.get('kind')=='cash' and action in {'income','expense','transfer'}
            valid &= (action!='income' or amount>=0) and (action!='expense' or amount<=0)
            if action=='transfer': valid &= fact.get('counter_account_code')==fields.get('counter_account_code')
    else:
        direction={'subscribe':'fund_subscription','redeem':'fund_redemption','transfer':'deposit' if amount>0 else 'withdraw'}.get(action,action)
        side={'transfer':'deposit' if amount>0 else 'withdraw'}.get(action,action)
        valid=(fact.get('account_code')==batch['account_code'] and str(fact.get('trade_date'))==confirmed
               and fact.get('trade_type')==direction and fact.get('side')==side
               and _number(fact.get('net_cash'))==amount and _number(fact.get('fx_rate_to_cny'))==Decimal(1)
               and _number(fact.get('net_cash_cny'))==amount)
        from domain.investments.investment_trade_fact import _trade_flags
        valid &= all(fact.get(key)==value for key,value in _trade_flags(direction,side).items())
        if fields.get('kind')=='trade':
            valid &= fields.get('status')=='confirmed'
            if action in {'buy','sell','subscribe','redeem'}:
                after=expected[0].get('after') or {}
                valid &= (after.get('asset_id')==fact.get('asset_id') and after.get('symbol')==fact.get('symbol')
                          and bool(fact.get('asset_id')) and bool(fact.get('symbol')))
                if fields.get('asset_id') and fields['asset_id'] != fact.get('asset_id'):
                    valid=False
                if fields.get('symbol') != fact.get('symbol'):
                    if isolation not in {'repeatable read','serializable'}:
                        return {'status':'unknown','reason':'asset_mapping_snapshot_required'}
                    valid &= _asset(cur,fields,fact)
                valid &= _number(fields.get('quantity')) is not None and _number(fields.get('quantity'))==_number(fact.get('quantity'))
                valid &= _number(fields.get('price')) is not None and _number(fields.get('price'))==_number(fact.get('price'))
            if action!='fee':
                valid &= _number(fields.get('gross_amount')) is not None and _number(fields.get('gross_amount'))==_number(fact.get('gross_amount'))
            fee=_number(fields.get('fee_amount'))
            valid &= fee is not None and abs(fee)==_number(fact.get('fee_amount'))
            tax=_number(fields.get('tax_amount'))
            if fields.get('fee_scope')=='separate': valid &= tax is not None and abs(tax)==_number(fact.get('tax_amount'))
            elif fields.get('fee_scope')=='total_charges': valid &= fact.get('tax_amount') is None
            else: valid=False
            gross=_number(fact.get('gross_amount'))
            charges=(abs(fee) + (abs(tax) if tax is not None and fields.get('fee_scope')=='separate' else Decimal(0))) if fee is not None else None
            if gross is not None and charges is not None:
                expected_cash = (-gross-charges if direction in {'buy','fund_subscription'} else
                                 gross-charges if direction in {'sell','fund_redemption','dividend','interest','coupon'} else
                                 -gross if direction in {'fee','tax'} else amount)
                valid &= expected_cash == amount
            else:
                valid=False
            realized_expected = amount if direction in {'dividend','interest','coupon','fee','tax'} else None
            valid &= _number(fact.get('realized_pnl')) == realized_expected
            valid &= _number(fact.get('realized_pnl_cny')) == realized_expected
        elif fields.get('kind')=='cash':
            valid &= direction in {'deposit','withdraw'} and _number(fact.get('gross_amount'))==abs(amount)
        else: valid=False
    return {'status':'verified' if valid else 'inconsistent',
            'reason':'reviewed_source_matches' if valid else 'fact_content_mismatch',
            'source_role':'derived_bank_leg' if derived else 'direct',
            'external_authority_verified':False}


class NativeFactVerifier:
    """Verify facts with caches scoped to one immutable read-only transaction."""
    def __init__(self, cur):
        self.cur=cur
        self._snapshot_cache={}
        self._proof_cache={}
        self._proof_transaction=None

    def _readonly_key(self):
        if self.cur.connection.autocommit:
            return None
        self.cur.execute("""SELECT current_setting('transaction_read_only') AS readonly,
            current_setting('transaction_isolation') AS isolation,pg_is_in_recovery() AS recovering,
            pg_backend_pid() AS backend,
            CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_xact_id()::text END AS xid""")
        state=self.cur.fetchone()
        if isinstance(state,dict):
            readonly,isolation,recovering,backend,xid=(state['readonly'],state['isolation'],state['recovering'],state['backend'],state['xid'])
        else:
            readonly,isolation,recovering,backend,xid=state
        if readonly=='on' and isolation in {'repeatable read','serializable'} and not recovering and xid:
            return (backend,xid)
        return None

    def verify_many(self, *, table: str, fact_ids: list[int]) -> dict[int, dict[str, Any]]:
        # Check the transaction before and after the whole batch; a connection
        # reused across commit/rollback cannot return stale proof. Writable or
        # standby transactions always use the ordinary uncached verifier.
        valid = table in {'pams_investment_trade_fact','pams_cashflow_fact'} and all(
            isinstance(i,int) and not isinstance(i,bool) and i > 0 for i in fact_ids)
        tx = self._readonly_key() if valid else None
        if tx is None:
            self._proof_cache.clear()
            self._proof_transaction=None
            return {i:verify_native_fact(self.cur,table=table,fact_id=i,_snapshot_cache=self._snapshot_cache) for i in fact_ids}
        if tx != self._proof_transaction:
            self._proof_cache.clear()
            self._proof_transaction=tx
        for fact_id in fact_ids:
            key=(table,fact_id)
            if key not in self._proof_cache:
                self._proof_cache[key]=verify_native_fact(self.cur,table=table,fact_id=fact_id,_snapshot_cache=self._snapshot_cache)
        if self._readonly_key() != tx:
            self._proof_cache.clear()
            self._proof_transaction=None
            return {i:{'status':'unknown','reason':'transaction_changed_during_verification'} for i in fact_ids}
        return {i:dict(self._proof_cache[(table,i)]) for i in fact_ids}

    def verify(self, *, table: str, fact_id: int) -> dict[str, Any]:
        return self.verify_many(table=table,fact_ids=[fact_id])[fact_id]
