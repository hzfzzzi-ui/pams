"""Published source-backed coverage for account external cash flows.

A coverage report is an immutable unified source batch, not a new ledger or
runtime state table. Review explicitly binds every row to a verified fact; it
does not authenticate the real-world identity of the uploaded report.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
from pathlib import PurePosixPath
from uuid import UUID, uuid5

from psycopg2.extras import Json
from domain.imports import isolation
from domain.imports.account_import_store import norm
from domain.imports.source_fact_evidence import NativeFactVerifier, _digest, _reviewed_snapshot
from domain.imports.unified_source import add_item, attach_file, create_batch, lock_source_review
from domain.investments.holding_evidence_writer import lock_holding_accounts
from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.accounts.official_flow_coverage import _view_date


NAMESPACE = UUID('c58a1418-d2ab-4918-9bf0-79b50564a8e6')
FIELDS = ('account_code','period_start','period_end','event_date','side','currency','net_amount','external_id')
MAX_BYTES = 10485760
MAX_ROWS = 2000
KNOWN_CASH_TYPES = {'buy','sell','fund_subscription','fund_redemption','dividend','interest','coupon',
                    'fee','tax','fx_conversion','corporate_action','split','transfer_in_kind',
                    'transfer_out_kind','position_adjustment','deposit','withdraw'}


def _require(cur):
    if cur.connection.autocommit or not isolation.source_enabled(cur):
        raise ValueError('flow coverage requires an isolated transaction')


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('coverage date must be ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('coverage date must be ISO date') from exc
    if parsed.isoformat() != value:
        raise ValueError('coverage date must be ISO date')
    return parsed


def parse_report(content: bytes, *, account_code: str):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
        raise ValueError('coverage original is missing or too large')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('coverage report must be UTF-8') from exc
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError('coverage template columns differ')
    records = []
    period = None
    seen = set()
    try:
        for row in reader:
            if len(records) >= MAX_ROWS or None in row or any(not isinstance(v, str) or len(v) > 500 for v in row.values()):
                raise ValueError('coverage report has invalid rows')
            row = {k: v.strip() for k,v in row.items()}
            first, last = _day(row['period_start']), _day(row['period_end'])
            if row['account_code'] != account_code or first > last or last > _view_date():
                raise ValueError('coverage account or period is invalid')
            if period is None:
                period = (first,last)
            if period != (first,last):
                raise ValueError('coverage rows disagree on period')
            if row['side'] == 'none':
                if any(row[k] for k in ('event_date','currency','net_amount','external_id')) or records:
                    raise ValueError('empty coverage marker must be the only row')
                records.append({'side':'none'})
                continue
            if any(record['side'] == 'none' for record in records) or row['side'] not in {'deposit','withdraw'}:
                raise ValueError('coverage side is invalid')
            day = _day(row['event_date'])
            if not first <= day <= last or len(row['currency']) != 3 or not row['currency'].isascii() or not row['currency'].isupper() or not row['currency'].isalpha():
                raise ValueError('coverage event date or currency is invalid')
            try:
                amount = Decimal(row['net_amount'])
                if (not amount.is_finite() or amount == 0 or abs(amount) >= Decimal('1e18') or
                    amount != amount.quantize(Decimal('0.0000000001')) or
                    (row['side'] == 'deposit') != (amount > 0)):
                    raise ValueError('coverage amount or direction is invalid')
            except (InvalidOperation, ValueError) as exc:
                raise ValueError('coverage amount or direction is invalid') from exc
            external_id = row['external_id']
            if not external_id or external_id in seen:
                raise ValueError('coverage source identity is absent or repeated')
            seen.add(external_id)
            records.append({'event_date':day.isoformat(),'side':row['side'],'currency':row['currency'],
                            'net_amount':str(amount),'external_id':external_id})
    except csv.Error as exc:
        raise ValueError('coverage CSV is invalid') from exc
    if not records or period is None:
        raise ValueError('coverage report is empty')
    if records == [{'side':'none'}]:
        records = []
    return {'account_code':account_code,'period_start':period[0].isoformat(),
            'period_end':period[1].isoformat(),'events':records}


def stage(cur, *, account_code, filename, content):
    _require(cur)
    if not isinstance(account_code,str) or not account_code or account_code.strip()!=account_code:
        raise ValueError('coverage account is invalid')
    name = str(filename or '')
    if (PurePosixPath(name.replace('\\','/')).name != name or not name.lower().endswith('.csv')
        or len(name)>180):
        raise ValueError('coverage filename must be a CSV basename')
    report = parse_report(content, account_code=account_code)
    lock_holding_accounts(cur,[account_code])
    cur.execute('SELECT 1 FROM pams_accounts WHERE account_code=%s AND active', (account_code,))
    if not cur.fetchone():
        raise ValueError('coverage account missing or inactive')
    digest=sha256(content).hexdigest()
    batch_id=uuid5(NAMESPACE,account_code+':'+digest)
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR UPDATE',(str(batch_id),))
    existing=cur.fetchone()
    if existing:
        if (existing['account_code']!=account_code or existing['workflow']!='external_flow_coverage'
            or existing['date_from'].isoformat()!=report['period_start'] or existing['date_to'].isoformat()!=report['period_end']):
            raise ValueError('coverage batch identity conflicts')
        return inspect(cur,batch_id)
    create_batch(cur,batch_id=batch_id,account_code=account_code,business_type='cash',
                 institution=account_code,acquisition_method='upload')
    cur.execute("""UPDATE source_batches SET workflow='external_flow_coverage',completeness='complete',
        date_from=%s,date_to=%s WHERE batch_id=%s""",(report['period_start'],report['period_end'],str(batch_id)))
    file_id=uuid5(batch_id,'file')
    attach_file(cur,file_id=file_id,batch_id=batch_id,filename=name,file_format='csv',
                content_type='text/csv',content=content)
    raw={'report':report,'source_sha256':digest,'authentication':'unknown'}
    add_item(cur,item_id=uuid5(batch_id,'scope'),batch_id=batch_id,file_id=file_id,source_uid='scope',
             parse_method='template',record_hash=_digest(raw),raw_fields=raw,parsed_fields={})
    return inspect(cur,batch_id)


def inspect(cur,batch_id):
    _require(cur)
    cur.execute("SELECT * FROM source_batches WHERE batch_id=%s AND workflow='external_flow_coverage'",(str(batch_id),))
    batch=cur.fetchone()
    if not batch:
        raise ValueError('coverage batch missing')
    cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'",(str(batch_id),))
    item=cur.fetchone()
    cur.execute("SELECT file_id,filename,sha256 FROM source_files WHERE batch_id=%s",(str(batch_id),))
    files=cur.fetchall()
    if not item or len(files)!=1:
        raise ValueError('coverage evidence incomplete')
    return norm({'batch_id':batch['batch_id'],'account_code':batch['account_code'],'revision':batch['revision'],
        'status':batch['status'],'report':item['raw_fields']['report'],'file':dict(files[0]),
        'review_authentication':'unknown'})


def _verified_fact(cur, row, verifier):
    if row['source_table'] == 'ibkr_flex_cash_transaction':
        from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
        proof=verify_ibkr_fact(cur,trade_id=row['trade_id'])
        required={'trade_date','account_code','currency','side','gross_amount','net_cash','source_key','source_id'}
        return proof.get('status') in {'consistent','partial'} and not required.intersection(proof.get('unknown_fields',[]))
    return verifier.verify(table='pams_investment_trade_fact',fact_id=row['trade_id']).get('status')=='verified'


def has_unvalued_non_cash_transfer(cur, account, first, last):
    # These facts may alter NAV without cash.  Their valuation is not proven
    # by the ordinary source report or by a matching official bridge alone.
    cur.execute("""SELECT 1 FROM pams_investment_trade_fact
        WHERE account_code=%s AND trade_date BETWEEN %s AND %s
          AND COALESCE(LOWER(review_status),'auto') NOT IN ('ignored','cancelled','canceled','void')
          AND (trade_type IN ('transfer_in_kind','transfer_out_kind','position_adjustment')
               OR (NOT affects_cash AND (trade_type IS NULL OR LOWER(trade_type)='unknown')))
        LIMIT 1""", (account,first,last))
    return bool(cur.fetchone())


def _events(cur, report, mappings):
    account=report['account_code']; first=report['period_start']; last=report['period_end']
    if has_unvalued_non_cash_transfer(cur, account, first, last):
        raise ValueError('unvalued non-cash transfer prevents period coverage')
    cur.execute("""SELECT f.trade_id,f.account_code,f.trade_date,f.currency,f.trade_type,f.side,f.net_cash,
        f.affects_cash,f.review_status,f.source_table,f.source_key,f.source_id,f.source_hash,f.source_item_id,
        i.source_uid,i.external_id FROM pams_investment_trade_fact f
        LEFT JOIN source_items i ON i.source_item_id=f.source_item_id
        WHERE f.account_code=%s AND f.trade_date BETWEEN %s AND %s AND f.affects_cash
          AND COALESCE(LOWER(f.review_status),'auto') NOT IN ('ignored','cancelled','canceled','void')
        ORDER BY f.trade_id""",(account,first,last))
    all_rows=[dict(row) for row in cur.fetchall()]
    if any(str(row['trade_type'] or '').lower() not in KNOWN_CASH_TYPES for row in all_rows):
        raise ValueError('unclassified cash fact prevents period coverage')
    external=[row for row in all_rows if row['trade_type'] in {'deposit','withdraw'}]
    events=report['events']
    if (not isinstance(mappings,dict) or set(mappings)!={item['external_id'] for item in events}
        or any(not isinstance(value,int) or isinstance(value,bool) or value<=0 for value in mappings.values())
        or len(set(mappings.values()))!=len(mappings) or {row['trade_id'] for row in external}!=set(mappings.values())):
        raise ValueError('coverage facts are incomplete or ambiguous')
    by_id={row['trade_id']:row for row in external}
    verifier=NativeFactVerifier(cur)
    selected=[]
    for event in events:
        row=by_id[mappings[event['external_id']]]
        ids={str(value) for value in (row['source_key'],row['source_uid'],row['external_id']) if value}
        if (event['external_id'] not in ids or str(row['trade_date'])!=event['event_date'] or
            row['currency']!=event['currency'] or row['trade_type']!=event['side'] or
            row['side']!=event['side'] or row['net_cash'] is None or Decimal(row['net_cash'])!=Decimal(event['net_amount']) or
            not _verified_fact(cur,row,verifier)):
            raise ValueError('coverage row conflicts with verified original fact')
        selected.append({'external_id':event['external_id'],'trade_id':row['trade_id'],
                         'fact_hash':_digest(norm(row))})
    return sorted(selected,key=lambda item:item['external_id'])


def publish(cur, *, batch_id, revision, mappings, note, confirmed):
    _require(cur)
    cur.execute('SAVEPOINT external_flow_coverage_publish')
    try:
        result=_publish(cur,batch_id=batch_id,revision=revision,mappings=mappings,note=note,confirmed=confirmed)
        cur.execute('RELEASE SAVEPOINT external_flow_coverage_publish')
        return result
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT external_flow_coverage_publish')
        cur.execute('RELEASE SAVEPOINT external_flow_coverage_publish')
        raise


def _publish(cur, *, batch_id, revision, mappings, note, confirmed):
    from domain.imports.native_source import enabled
    if not enabled() or cur.connection.isolation_level != 3:
        raise ValueError('coverage publication requires enabled serializable transaction')
    if confirmed is not True or not isinstance(note,str) or not note.strip() or len(note)>500:
        raise ValueError('coverage requires explicit review and a note')
    batch_id=UUID(str(batch_id))
    cur.execute('SELECT account_code FROM source_batches WHERE batch_id=%s',(str(batch_id),))
    row=cur.fetchone()
    if not row:
        raise ValueError('coverage batch missing')
    lock_holding_accounts(cur,[row['account_code']])
    cur.execute("SELECT * FROM source_batches WHERE batch_id=%s AND workflow='external_flow_coverage' FOR UPDATE",(str(batch_id),))
    batch=cur.fetchone()
    if not batch or batch['revision']!=revision:
        raise ValueError('coverage batch is stale or missing')
    if batch['status']=='published':
        report, stored, _ = _scope(cur,batch)
        if ({item['external_id']:item['trade_id'] for item in stored} != mappings):
            raise ValueError('published coverage decisions differ')
        cur.execute("SELECT review_note FROM source_items WHERE batch_id=%s AND source_uid='scope'",(str(batch_id),))
        reviewed=cur.fetchone()
        if not reviewed or reviewed['review_note']!=note.strip():
            raise ValueError('published coverage review differs')
        return {**inspect(cur,batch_id),'replayed':True}
    if batch['status'] not in {'draft','review'}:
        raise ValueError('coverage batch is not editable')
    cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope' FOR UPDATE",(str(batch_id),))
    item=cur.fetchone()
    cur.execute('SELECT * FROM source_files WHERE batch_id=%s FOR SHARE',(str(batch_id),))
    files=cur.fetchall()
    if not item or len(files)!=1:
        raise ValueError('coverage source is incomplete')
    file=files[0]; content=bytes(file['content']); raw=item['raw_fields']
    report=parse_report(content,account_code=batch['account_code'])
    if (sha256(content).hexdigest()!=str(file['sha256']).strip() or raw.get('source_sha256')!=str(file['sha256']).strip()
        or raw.get('report')!=report or str(item['file_id'])!=str(file['file_id'])):
        raise ValueError('coverage original changed')
    selected=_events(cur,report,mappings)
    decisions={'facts':selected,'note':note.strip(),'authentication':'unknown'}
    cur.execute("""UPDATE source_items SET review_status='confirmed',review_note=%s,parsed_fields=%s
        WHERE source_item_id=%s""",(note.strip(),Json(decisions),str(item['source_item_id'])))
    cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(str(batch_id),))
    final_revision=cur.fetchone()['revision']
    snapshot=lock_source_review(cur,batch_id=batch_id,expected_revision=final_revision)
    if snapshot['snapshot']['blockers']:
        raise ValueError('coverage source review is incomplete')
    cur.execute("""UPDATE source_batches SET status='published',published_source_hash=%s,
        published_result=%s WHERE batch_id=%s""",(snapshot['source_snapshot_sha256'],Json({'kind':'external_flow_coverage'}),str(batch_id)))
    return {**inspect(cur,batch_id),'replayed':False}


def _scope(cur,batch):
    if (batch['status']!='published' or batch['workflow']!='external_flow_coverage' or
        batch['business_type']!='cash' or batch['completeness']!='complete' or
        not _reviewed_snapshot(cur,batch,str(batch['batch_id']),locking=False,snapshot_cache={})):
        raise ValueError('coverage source is not published and verified')
    cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'",(batch['batch_id'],))
    item=cur.fetchone()
    cur.execute('SELECT * FROM source_files WHERE batch_id=%s',(batch['batch_id'],))
    files=cur.fetchall()
    if not item or item['review_status']!='confirmed' or len(files)!=1 or str(item['file_id'])!=str(files[0]['file_id']):
        raise ValueError('coverage source is incomplete')
    file=files[0]; content=bytes(file['content']); raw=item['raw_fields']; decision=item['parsed_fields']
    report=parse_report(content,account_code=batch['account_code'])
    if (report!=raw.get('report') or sha256(content).hexdigest()!=raw.get('source_sha256') or
        raw.get('source_sha256')!=str(file['sha256']).strip() or not decision.get('note') or
        batch['date_from'].isoformat()!=report['period_start'] or batch['date_to'].isoformat()!=report['period_end']):
        raise ValueError('coverage original or period changed')
    stored=decision.get('facts')
    if not isinstance(stored,list) or any(not isinstance(item,dict) or set(item)!={'external_id','trade_id','fact_hash'} for item in stored):
        raise ValueError('coverage decisions are invalid')
    mappings={item['external_id']:item['trade_id'] for item in stored}
    if len(mappings)!=len(stored) or _events(cur,report,mappings)!=stored:
        raise ValueError('coverage facts have changed')
    return report, stored, raw['source_sha256']


def _snapshot_total(cur,account_code,day):
    proof=load_account_snapshot_coverage(cur,{(account_code,day)}).get((account_code,day),{})
    if proof.get('status')!='complete':
        return None
    cur.execute("""SELECT currency,asset_liability,market_value FROM pams_holdings_snapshot
        WHERE account_code=%s AND as_of=%s ORDER BY id""",(account_code,day))
    rows=cur.fetchall()
    if not rows:
        return None
    from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
    total=Decimal(0)
    for row in rows:
        value=Decimal(str(row['market_value']))
        if not value.is_finite():
            return None
        if row['asset_liability']=='\u8d1f\u503a':
            value=-abs(value)
        if value == 0:
            continue
        fx=historical_fx(cur,currency=row['currency'],base_currency='CNY',as_of=day)
        if fx.get('status')!='ok' or not str(fx.get('source') or '').strip():
            return None
        total+=value*Decimal(str(fx['rate']))
    return total


def verified_period(cur, *, account_code, start, end, ending_gross_cny):
    """Prove contiguous source periods, complete facts, exact-day FX and NAV."""
    if (type(start) is not date or type(end) is not date or start>=end or end>_view_date()
        or cur.connection.autocommit):
        return None
    cur.execute("SELECT current_setting('transaction_isolation') AS isolation")
    mode=cur.fetchone()
    if not mode or mode['isolation'] not in {'repeatable read','serializable'}:
        return None
    opening=_snapshot_total(cur,account_code,start)
    closing=_snapshot_total(cur,account_code,end)
    if opening is None or closing is None or abs(closing-ending_gross_cny)>Decimal('0.01'):
        return None
    cur.execute("""SELECT * FROM source_batches WHERE account_code=%s AND workflow='external_flow_coverage'
        AND status='published' AND date_from<=%s AND date_to>%s ORDER BY date_from,date_to,batch_id LIMIT 201""",
        (account_code,end,start))
    batches=cur.fetchall()
    if len(batches)>200:
        return None
    scopes=[]
    for batch in batches:
        try:
            report, facts, digest=_scope(cur,batch)
            scopes.append({'from':batch['date_from'],'to':batch['date_to'],'report':report,'facts':facts,'digest':digest})
        except (ValueError,TypeError,KeyError,InvalidOperation):
            continue
    # A DAG of inclusive intervals supports contiguous multiple batches while
    # ignoring duplicates: each path advances beyond its previous end, so a
    # fact cannot be counted twice. Every segment revalidates the entire set.
    paths={start:[]}
    boundaries=[start]+sorted({min(scope['to'],end) for scope in scopes if scope['to']>start})
    for boundary in boundaries:
        if boundary not in paths or boundary>=end:
            continue
        for scope in scopes:
            first=boundary+timedelta(days=1)
            if scope['from']<=first<=scope['to']:
                last=min(scope['to'],end)
                paths.setdefault(last,paths[boundary]+[(scope,first,last)])
    chain=paths.get(end)
    if chain is None:
        return None
    from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
    flow=Decimal(0); seen=set()
    for scope,first,last in chain:
        for event,record in zip(sorted(scope['report']['events'],key=lambda x:x['external_id']),scope['facts']):
            day=date.fromisoformat(event['event_date'])
            if not first <= day <= last:
                continue
            if record['trade_id'] in seen:
                return None
            seen.add(record['trade_id'])
            fx=historical_fx(cur,currency=event['currency'],base_currency='CNY',as_of=day)
            if fx.get('status')!='ok' or not str(fx.get('source') or '').strip():
                return None
            flow+=Decimal(event['net_amount'])*Decimal(str(fx['rate']))
    return {'start':start,'end':end,'opening_gross_cny':opening,'closing_gross_cny':closing,
            'external_flow_gross_cny':flow,'source_sha256':[scope['digest'] for scope,_,_ in chain],
            'basis':'reviewed_source_reports','authentication':'unknown'}


def latest_verified_period(cur, *, account_code, end, ending_gross_cny):
    cur.execute('SELECT MAX(as_of) AS previous_day FROM account_snapshot_version WHERE account_code=%s AND as_of<%s',
                (account_code,end))
    row=cur.fetchone(); start=row.get('previous_day') if row else None
    if type(start) is not date:
        return None
    return verified_period(cur,account_code=account_code,start=start,end=end,ending_gross_cny=ending_gross_cny)
