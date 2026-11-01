"""Isolated integration for current holding writers and unknown inputs."""
from __future__ import annotations

import os
from hashlib import sha256
import json

from psycopg2.extras import RealDictCursor

from domain.imports import isolation


def enabled(conn) -> bool:
    mode = isolation.source_mode()
    if mode != 'development' and os.environ.get('PAMS_HOLDING_EVIDENCE_ISOLATED', '').lower() != 'true':
        return False
    if conn.autocommit:
        raise ValueError('holding evidence requires a transaction')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if not isolation.source_enabled(cur):
            raise isolation.IsolationError('holding evidence requires isolated source mode')
    return True


def lock_account(conn, account_code):
    """Share the transaction lock used by account import publication."""
    if conn.autocommit:
        raise ValueError('holding writes require a transaction')
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                    ('account-import:' + account_code,))


def reject_unproven_retirements(conn, *, account_code, business_date, names, origin, cash_unknown=False):
    """Missing rows alone are not evidence of a complete position closure."""
    with conn.cursor() as cur:
        if origin == 'ibkr_flex':
            cur.execute('SELECT name FROM public.pams_holdings_snapshot WHERE account_code=%s AND as_of=%s FOR UPDATE',
                        (account_code, business_date))
        elif origin == 'tradingview':
            cur.execute('''SELECT name FROM public.pams_holdings_snapshot WHERE account_code=%s AND as_of=%s
                AND (major_category='A股' OR (NOT %s AND major_category='现金' AND symbol='$CASH')) FOR UPDATE''',
                        (account_code, business_date, cash_unknown))
        else:
            raise ValueError('unknown holding writer')
        existing = {row[0] for row in cur.fetchall()}
    if existing - set(names):
        raise ValueError('holding omission requires explicit complete-source retirement evidence')


def capture_existing_holdings(conn, *, account_code, business_date, names, source_by_name=None):
    """Persist old holding inputs before any in-place correction."""
    from domain.investments.holding_evidence_writer import prepare_holding_change

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for name in sorted(names):
            prepare_holding_change(cur, account_code=account_code, name=name, as_of=business_date,
                                   source_item_id=(source_by_name or {}).get(name))


def reject_identity_conflicts(conn, *, account_code, business_date, incoming):
    """A matching display name alone cannot identify a security or currency."""
    expected = {row['name']: row for row in incoming}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('account-import:'+account_code,))
        cur.execute('''SELECT id,name,symbol,currency FROM public.pams_holdings_snapshot
            WHERE account_code=%s AND as_of<=%s AND name=ANY(%s::text[])
            ORDER BY name,as_of DESC,id DESC''',
            (account_code, business_date, list(expected)))
        seen=set()
        for row in cur.fetchall():
            if row['name'] in seen: continue
            seen.add(row['name'])
            candidate=expected[row['name']]
            symbol,currency=candidate.get('symbol'),candidate.get('currency')
            if not symbol or not currency or not row['symbol'] or not row['currency'] or (row['symbol'], row['currency']) != (symbol, currency):
                raise ValueError('holding identity or currency requires explicit confirmation')
            # Cash has currency identity rather than a broker contract ID; every
            # other IBKR security must respect any prior reviewed contract identity.
            if account_code!='ibkr' or (symbol=='CASH_'+currency and candidate.get('major_category') in {'现金','借款'}):
                continue
            cur.execute('''SELECT i.batch_id,i.source_item_id FROM source_items i
                JOIN source_batches b ON b.batch_id=i.batch_id
                WHERE b.status='published' AND b.account_code=%s AND i.review_status='confirmed'
                  AND i.parsed_fields->'holding_identity_review'->>'holding_id'=%s''',
                (account_code,str(row['id'])))
            reviews=cur.fetchall(); verified=[]
            if reviews:
                cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=ANY(%s::uuid[]) ORDER BY batch_id FOR SHARE',
                            ([str(review['batch_id']) for review in reviews],))
                cur.fetchall()
            from domain.brokers.ibkr.ibkr_holding_identity import verified_holding_identity
            for review in reviews:
                try:
                    verified.append(verified_holding_identity(cur,batch_id=review['batch_id'],
                        item_id=review['source_item_id'],holding_id=row['id']))
                except ValueError:
                    continue
            if reviews and not verified:
                raise ValueError('holding contract identity review is stale or unavailable')
            if verified and (len({(proof['conid'],proof['source_account_id']) for proof in verified})!=1
                    or any(proof['conid']!=candidate.get('ibkr_conid') or
                           proof['source_account_id']!=candidate.get('ibkr_report_account') for proof in verified)):
                raise ValueError('holding report contract conflicts with reviewed identity')
            cur.execute('SELECT name,symbol,currency FROM pams_holdings_snapshot WHERE id=%s FOR UPDATE',(row['id'],))
            locked=cur.fetchone()
            if not locked or any(locked[k]!=row[k] for k in ('name','symbol','currency')):
                raise ValueError('holding identity changed during report review')


def _ibkr_retirement_replayed(cur, *, old, ref, business_date, report_sha256, read_only=False):
    """Accept a replay only when immutable before/after evidence matches both sources."""
    from domain.imports.ibkr_holding_source import retirement_status
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    from domain.investments.valuation_input_evidence import _bound_input, verify_recorded_version, versions_for_source_item
    if any(old.get(k) is None or old[k]!=0 for k in ('quantity','cost','market_value')):
        return False
    proof=retirement_status(cur,batch_id=ref['batch_id'],item_id=ref['item_id'],account_code='ibkr',business_date=business_date,read_only=read_only)
    if (proof.get('state')!='confirmed_zero' or proof.get('source_sha256')!=report_sha256
            or proof.get('symbol')!=old['symbol'] or proof.get('currency')!=old['currency']):
        return False
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s' + ('' if read_only else ' FOR SHARE'),(ref['identity_batch_id'],))
    batch=cur.fetchone()
    cur.execute('SELECT * FROM source_items WHERE batch_id=%s AND source_item_id=%s' + ('' if read_only else ' FOR SHARE'),
                (ref['identity_batch_id'],ref['identity_item_id']))
    item=cur.fetchone()
    identity=(item['parsed_fields'] or {}).get('holding_identity_review') if item else None
    if (not batch or batch['status']!='published' or batch['account_code']!='ibkr'
            or not item or item['review_status']!='confirmed' or not str(item['review_note'] or '').strip()
            or not isinstance(identity,dict) or identity.get('identity_authentication')!='unknown'
            or not identity.get('holding_id') or identity.get('conid')!=proof.get('conid')
            or identity.get('source_account_id')!=proof.get('source_account_id')
            or not _reviewed_snapshot(cur,batch,str(batch['batch_id']),locking=not read_only,snapshot_cache={})):
        return False
    current,_=_bound_input(cur,'holding',{'id':old['id']},locking=not read_only)
    before=False; after=False
    for version in versions_for_source_item(cur,ref['identity_item_id']):
        if (version['role']!='ibkr_reviewed_contract_identity' or version['account_code']!='ibkr'
                or version['business_date']>business_date or not verify_recorded_version(cur,version['version_key'])):
            continue
        cur.execute("""SELECT e.evidence FROM valuation_evidence v
            CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
            WHERE v.version_key=%s""",(version['version_key'],))
        entries=[row['evidence'] for row in cur.fetchall()]
        holdings=[e for e in entries if e.get('kind')=='holding' and isinstance(e.get('snapshot'),dict)]
        if len(holdings)!=1: continue
        snapshot=holdings[0]['snapshot']; h=snapshot.get('content_hash'); content=snapshot.get('content') or {}
        before |= ((holdings[0].get('source_ref') or {}).get('key')=={'id':identity['holding_id']}
                   and content.get('account_code')=='ibkr' and content.get('name')==old['name']
                   and version['business_date'].isoformat()==identity.get('business_date')
                   and h==identity.get('holding_hash'))
        zero_source=any(e.get('kind')=='source_item' and e.get('role')=='ibkr_zero_position'
                        and ((e.get('source_ref') or {}).get('key') or {}).get('source_item_id')==str(ref['item_id']) for e in entries)
        after |= ((holdings[0].get('source_ref') or {}).get('key')=={'id':old['id']}
                  and version['business_date']==business_date
                  and h==(current.get('snapshot') or {}).get('content_hash') and zero_source)
    return before and after


def verify_ibkr_retirements(conn, *, account_code, business_date, incoming_names, sources,
                            expected_report_sha256=None):
    """Preserve verified existing zero rows; never infer identity for a new exit."""
    from domain.imports.ibkr_holding_source import retirement_status
    from domain.investments.valuation_input_evidence import current_holding_source

    if not isinstance(sources, dict):
        raise ValueError('retirement evidence must be a name-to-source mapping')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        batch_ids=[]
        for ref in sources.values():
            if (not isinstance(ref,dict) or not all(ref.get(k) for k in
                    ('batch_id','item_id','identity_batch_id','identity_item_id'))):
                raise ValueError('holding retirement requires verified existing conid identity')
            batch_ids.extend((str(ref['batch_id']),str(ref['identity_batch_id'])))
        if batch_ids:
            cur.execute('SELECT batch_id FROM source_batches WHERE batch_id=ANY(%s::uuid[]) ORDER BY batch_id FOR SHARE',(batch_ids,))
            cur.fetchall()
        cur.execute('''SELECT id,name,symbol,currency,major_category,quantity,cost,market_value,as_of
            FROM public.pams_holdings_snapshot WHERE account_code=%s AND as_of<=%s
            ORDER BY name,as_of DESC,id DESC''',(account_code,business_date))
        existing = {}
        for row in cur.fetchall():
            existing.setdefault(row['name'],row)
        missing = set(existing) - set(incoming_names)
        if not set(sources).issubset(missing):
            raise ValueError('holding omission requires explicit complete-source retirement evidence')
        retirements=[]; replayed=[]
        for name in sorted(sources):
            ref=sources[name]
            if (not isinstance(ref,dict) or not all(ref.get(k) for k in
                    ('batch_id','item_id','identity_batch_id','identity_item_id'))):
                raise ValueError('holding retirement requires verified existing conid identity')
            old=existing[name]
            if _ibkr_retirement_replayed(cur,old=old,ref=ref,business_date=business_date,
                                         report_sha256=expected_report_sha256):
                replayed.append(name)
                continue
            from domain.brokers.ibkr.ibkr_holding_identity import verified_holding_identity
            identity=verified_holding_identity(cur,batch_id=ref['identity_batch_id'],
                                               item_id=ref['identity_item_id'],holding_id=old['id'])
            proof=retirement_status(cur,batch_id=ref['batch_id'],item_id=ref['item_id'],
                                    account_code=account_code,business_date=business_date)
            if (old['major_category']!='美股' or proof.get('state')!='confirmed_zero'
                    or not expected_report_sha256 or proof.get('source_sha256')!=expected_report_sha256
                    or proof.get('symbol')!=old['symbol'] or proof.get('currency')!=old['currency']
                    or not proof.get('conid') or proof['conid']!=identity['conid']
                    or proof.get('source_account_id')!=identity['source_account_id']):
                raise ValueError('holding retirement requires verified existing conid identity')
            retirements.append({'id':old['id'],'name':name,'account_code':account_code,'business_date':business_date,
                                'previous_date':old['as_of'],
                                'item_id':str(ref['item_id']),'batch_id':str(ref['batch_id']),
                                'identity_item_id':str(ref['identity_item_id']),'identity_batch_id':str(ref['identity_batch_id']),
                                'source_sha256':expected_report_sha256,'conid':identity['conid'],
                                'source_account_id':identity['source_account_id']})
        preserved = list(replayed)
        for name in sorted(missing - set(sources)):
            old = existing[name]
            if (old['major_category'] != '美股' or
                any(old[key] is None or old[key] != 0 for key in ('quantity', 'cost', 'market_value')) or
                sum((row['symbol'], row['currency']) == (old['symbol'], old['currency']) for row in existing.values()) != 1):
                raise ValueError('holding omission requires explicit complete-source retirement evidence')
            current = current_holding_source(cur, old['id'])
            if current['status'] != 'recorded':
                raise ValueError('holding omission requires explicit complete-source retirement evidence')
            cur.execute('SELECT batch_id FROM public.source_items WHERE source_item_id=%s', (current['source_item_id'],))
            source = cur.fetchone()
            proof = retirement_status(cur, batch_id=source['batch_id'], item_id=current['source_item_id'],
                                      account_code=account_code, business_date=business_date) if source else {}
            if (proof.get('state') != 'confirmed_zero' or proof.get('symbol') != old['symbol'] or
                proof.get('currency') != old['currency']):
                raise ValueError('holding omission requires explicit complete-source retirement evidence')
            preserved.append(name)
        return retirements, preserved


def apply_ibkr_retirements(conn, retirements):
    """Recheck every source and capture the old value before explicit zeroing."""
    from domain.brokers.ibkr.ibkr_holding_identity import verified_holding_identity
    from domain.imports.ibkr_holding_source import retirement_status
    from domain.investments.holding_evidence_writer import finish_holding_change
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for item in retirements:
            try:
                identity=verified_holding_identity(cur,batch_id=item['identity_batch_id'],
                    item_id=item['identity_item_id'],holding_id=item['id'])
                proof=retirement_status(cur,batch_id=item['batch_id'],item_id=item['item_id'],
                    account_code=item['account_code'],business_date=item['business_date'])
                cur.execute('SELECT name,symbol,currency,account_code,as_of FROM pams_holdings_snapshot WHERE id=%s FOR UPDATE',(item['id'],))
                old=cur.fetchone()
                valid=(old and old['name']==item['name'] and proof.get('state')=='confirmed_zero'
                    and old['account_code']==item['account_code']=='ibkr' and old['as_of']==item['previous_date']
                    and old['as_of']<=item['business_date']
                    and proof.get('source_sha256')==item['source_sha256'] and proof.get('conid')==identity['conid']==item['conid']
                    and proof.get('source_account_id')==identity['source_account_id']==item['source_account_id']
                    and proof.get('symbol')==old['symbol'] and proof.get('currency')==old['currency'])
            except (KeyError,TypeError,ValueError):
                valid=False
            if not valid:
                raise ValueError('holding retirement requires verified existing conid identity')
            finish_holding_change(cur,holding_id=item['id'],source_item_id=None,input_refs=[
                {'kind':'source_item','role':'ibkr_reviewed_contract_identity',
                 'key':{'source_item_id':item['identity_item_id']}}])
            if item['previous_date']==item['business_date']:
                cur.execute('''UPDATE pams_holdings_snapshot SET quantity=0,cost=0,market_value=0,
                    buy_price=NULL,current_price=NULL,updated_at=now() WHERE id=%s RETURNING id''',(item['id'],))
            else:
                cur.execute("SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='public.pams_holdings_snapshot'::regclass AND attname='asset_id' AND NOT attisdropped) AS present")
                asset_column=',asset_id' if cur.fetchone()['present'] else ''
                cur.execute(f'''INSERT INTO pams_holdings_snapshot(symbol,name,asset_liability,currency,account_code,
                    major_category,minor_category,market_value,cost,quantity,buy_price,current_price,as_of{asset_column})
                    SELECT symbol,name,asset_liability,currency,account_code,major_category,minor_category,
                           0,0,0,NULL,NULL,%s{asset_column} FROM pams_holdings_snapshot WHERE id=%s RETURNING id''',
                    (item['business_date'],item['id']))
            result=cur.fetchone()
            if not result: raise ValueError('holding retirement target is missing')
            item['result_id']=result['id']



def finish_ibkr_retirements(conn, retirements):
    from domain.investments.holding_evidence_writer import finish_holding_change
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for item in retirements:
            if not all(item.get(k) for k in ('id','item_id','identity_item_id')):
                raise ValueError('holding retirement requires verified existing conid identity')
            cur.execute('SELECT quantity,cost,market_value FROM pams_holdings_snapshot WHERE id=%s FOR SHARE',(item.get('result_id'),))
            holding=cur.fetchone()
            if not holding or any(holding[k]!=0 for k in ('quantity','cost','market_value')):
                raise ValueError('holding retirement value conflicts with source')
            finish_holding_change(cur,holding_id=item['result_id'],source_item_id=None,input_refs=[
                {'kind':'source_item','role':'ibkr_zero_position','key':{'source_item_id':item['item_id']}},
                {'kind':'source_item','role':'ibkr_reviewed_contract_identity','key':{'source_item_id':item['identity_item_id']}}])


def _proven_tradingview_trade(cur, account_code, tx, file_cache):
    """Bind only a published source item and fact matching the actual input row."""
    cur.execute("SELECT to_regclass('public.source_items') AS items, to_regclass('public.source_batches') AS batches, to_regclass('public.source_files') AS files")
    available = cur.fetchone()
    if not all(available.values()):
        return None
    cur.execute("""SELECT EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid=to_regclass('public.pams_investment_trade_fact') AND attname='source_item_id' AND NOT attisdropped) AS present""")
    if not cur.fetchone()['present']:
        return None
    cur.execute("""SELECT f.*, i.raw_fields AS evidence,i.record_hash AS evidence_hash,
                          i.location AS evidence_location,i.source_uid AS evidence_uid,i.parse_method AS evidence_method,
                          sf.filename AS evidence_filename,
                          CASE WHEN %s THEN NULL ELSE sf.content END AS file_content
        FROM public.pams_investment_trade_fact f
        JOIN public.source_items i ON i.source_item_id=f.source_item_id AND i.review_status='confirmed'
        JOIN public.source_batches b ON b.batch_id=i.batch_id AND b.status='published'
          AND b.account_code=f.account_code AND b.institution='tradingview'
        JOIN public.source_files sf ON sf.file_id=i.file_id AND sf.batch_id=i.batch_id
        WHERE f.source_table='tradingview_csv' AND f.account_code=%s
          AND i.raw_fields->>'legacy_six_field_hash'=%s FOR SHARE OF f,i,b,sf""",
        (tx.source_hash in file_cache, account_code, tx.row_hash))
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    source = row['evidence']
    raw = (row['raw_json'] or {}).get('source_row', {}).get('raw_json')
    if (raw != tx.raw or source.get('raw_fields') != tx.raw or
        source.get('file_sha256') != tx.source_hash or source.get('legacy_six_field_hash') != tx.row_hash or
        row['trade_time'] != tx.closing_time or row['trade_date'] != tx.closing_time.date() or
        row['side'] != tx.side.lower() or row['quantity'] != tx.quantity or row['price'] != tx.fill_price or
        row['fee_amount'] != abs(tx.commission) or row['currency'] != 'CNY'):
        return None
    from domain.imports.tradingview_source import _prepare
    from collectors.tradingview_portfolio import TradingViewImportError
    if tx.source_hash not in file_cache:
        content = bytes(row['file_content'])
        if sha256(content).hexdigest() != tx.source_hash:
            return None
        try:
            parsed = _prepare([(row['evidence_filename'], content)])
            file_cache[tx.source_hash] = {f'{first}-{last}': (first, last, key, data)
                for _, _, _, items in parsed for first, last, key, data in items}
        except (ValueError, UnicodeError, TradingViewImportError):
            return None
    match = file_cache[tx.source_hash].get(row['evidence_location'])
    matches = [match] if match else []
    if len(matches) != 1:
        return None
    first, last, key, data = matches[0]
    exact = {'file_sha256': tx.source_hash, 'line_start': first, 'line_end': last,
             'legacy_six_field_hash': key, 'raw_fields': data}
    digest = sha256(json.dumps(exact, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    if (source != exact or row['evidence_hash'] != digest or row['evidence_uid'] != f'{tx.source_hash}:{first}:{last}'
        or row['evidence_method'] != 'template'):
        return None
    from domain.imports.tradingview_source_publish import source_row_from_item
    from domain.investments.investment_trade_fact import _STRICT_FACT_COLUMNS, build_investment_trade_fact
    item = {'source_item_id': row['source_item_id'], 'source_uid': row['evidence_uid']}
    expected = build_investment_trade_fact(source_row_from_item({'account_code': account_code}, item,
                                                              {'filename': row['evidence_filename']}, tx.raw))
    if any(row[key] != expected[key] for key in _STRICT_FACT_COLUMNS):
        return None
    return {'kind': 'trade', 'key': {'trade_id': row['trade_id']}, 'role': 'tradingview_transaction_input'}


def reject_unproven_tradingview_collisions(conn, *, account_code, transactions):
    """Do not let the six-field legacy hash overwrite unmatched evidence."""
    hashes = [tx.row_hash for tx in transactions]
    if len(hashes) != len(set(hashes)):
        raise ValueError('TradingView transaction identity is ambiguous')
    file_cache = {}
    proven = set()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for tx in transactions:
            ref = _proven_tradingview_trade(cur, account_code, tx, file_cache)
            if not ref:
                raise ValueError('TradingView hash requires matching verified source and fact evidence')
            proven.add(ref['key']['trade_id'])
    return proven


def capture_written_holdings(conn, *, account_code, business_date, names, origin, transactions=None):
    """Record each actual result; unavailable contribution references stay unknown."""
    from domain.investments.holding_evidence_writer import finish_holding_change

    if not names or len(set(names)) != len(names):
        raise ValueError('holding result set is empty or has ambiguous names')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''SELECT id,name,symbol FROM public.pams_holdings_snapshot
            WHERE account_code=%s AND as_of=%s AND name=ANY(%s::text[]) ORDER BY id FOR SHARE''',
            (account_code, business_date, names))
        rows = cur.fetchall()
        if len(rows) != len(names) or {row['name'] for row in rows} != set(names):
            raise ValueError('holding results differ from the writer output')
        file_cache = {}
        for row in rows:
            if origin == 'ibkr_flex':
                refs = [{'kind': 'source_item', 'role': 'ibkr_flex_statement'}]
            elif origin == 'tradingview':
                relevant = [tx for tx in transactions or [] if row['symbol'] == '$CASH' or
                            (tx.normalized_symbol == row['symbol'] and tx.side in {'Buy', 'Sell'})]
                refs = []
                missing = not relevant
                for tx in relevant:
                    ref = _proven_tradingview_trade(cur, account_code, tx, file_cache)
                    if ref:
                        refs.append(ref)
                    else:
                        missing = True
                if missing:
                    refs.append({'kind': 'trade', 'role': 'tradingview_transaction_inputs'})
                if row['symbol'] != '$CASH':
                    refs.append({'kind': 'quote', 'role': 'tradingview_mark_input'})
            else:
                raise ValueError('unknown holding writer')
            finish_holding_change(cur, holding_id=row['id'], source_item_id=None, input_refs=refs)
