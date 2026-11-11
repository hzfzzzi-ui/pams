"""Explicit transfer confirmation for source-record based facts."""
from __future__ import annotations
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from uuid import uuid4
from psycopg2.extras import Json
from domain.imports.account_import_types import LICAITONG_CURRENT_PLUS
from domain.imports.source_original_read import SourceOriginals

REDEMPTION_ACCOUNT = 'tencent_licaitong'
REDEMPTION_PURPOSE = 'fund_redemption_receipt'


def _reported_fields(raw):
    return ((raw.get('source') or {}).get('context') or {}).get('reported_fields') or {}


def _redemption_arrival(fields, raw, trade_day):
    reported = _reported_fields(raw)
    text = re.sub(r'\s+', '', reported.get('到账提示') or '').replace('＋', '+')
    match = re.fullmatch(r'(\d{2}-\d{2})\d{2}:\d{2}(?::\d{2})?资金已到账活期\+', text)
    confirmed = date.fromisoformat(fields.get('confirmed_date') or '')
    if (not match or confirmed < trade_day or match[1] != confirmed.strftime('%m-%d')
            or fields.get('action') != 'redeem' or fields.get('status') != 'confirmed'):
        raise ValueError('Redemption receipt destination or date is unverified')
    return confirmed


def _valid_id(ident):
    return type(ident) is int and ident > 0


def _leg_rows(cur, kind, ids, *, lock):
    if kind=='cashflow':
        table,pk,amount,day,kind_clause='pams_cashflow_fact','cashflow_id','amount','event_date','TRUE'
    else:
        table,pk,amount,day,kind_clause='pams_investment_trade_fact','trade_id','net_cash','trade_date',"f.trade_type IN ('deposit','withdraw','fund_redemption')"
    type_column="f.trade_type" if kind=='trade' else "NULL::text"
    cur.execute(f'''SELECT f.{pk} AS id,f.account_code,f.currency,f.{amount} AS amount,f.{day} AS day,{type_column} AS fact_type,
        i.source_item_id,i.source_uid,i.raw_fields,i.parsed_fields,i.record_hash,i.review_status,
        r.source_id AS original_source_id,r.status,r.account_code AS source_account,
        r.content IS NOT NULL AS inline_original,r.sha256,r.acquisition_method
        FROM public.{table} f JOIN public.source_items i ON i.source_item_id=f.source_item_id
        JOIN public.source_records r ON r.source_id=i.source_id WHERE f.{pk}=ANY(%s) AND {kind_clause}
        {"FOR UPDATE OF f FOR SHARE OF i,r" if lock else ""}''',(ids,))
    return cur.fetchall()


def _verified_leg(row, kind, originals, *, hash_only=False):
    if not row: raise ValueError('transfer fact not found')
    data=dict(row)
    try:
        fields=data['parsed_fields'];parsed_amount=Decimal(str(fields['net_cash']))
        redemption = kind == 'trade' and data['fact_type'] == 'fund_redemption'
        arrival = None
        if redemption:
            if data['account_code'] != REDEMPTION_ACCOUNT or data['acquisition_method'] != 'screenshot' or data['currency'] != 'CNY':
                raise ValueError('Unsupported redemption receipt')
            arrival = _redemption_arrival(fields, data['raw_fields'], data['day'])
        raw=json.dumps(data['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')
        valid=(data['status']=='published' and data['review_status']=='confirmed' and data['account_code']==data['source_account']
            and data['currency']==fields['currency'] and str(data['day'])==fields['business_date'] and data['amount']==parsed_amount
            and (kind!='trade' or redemption or data['fact_type']==(fields.get('trade_type') or fields.get('action')))
            and (kind!='trade' or (data['fact_type']=='deposit' and parsed_amount>0) or (data['fact_type']=='withdraw' and parsed_amount<0) or (redemption and parsed_amount>0))
            and parsed_amount.is_finite() and parsed_amount!=0 and sha256(raw).hexdigest()==data['record_hash']
            and (originals.verify(data) if hash_only else originals.read(data)[1]))
    except (KeyError,TypeError,ValueError,InvalidOperation): valid=False
    if not valid: raise ValueError('transfer fact source is unverified or inconsistent')
    leg = {'kind':kind,'id':data['id'],'account_code':data['account_code'],'currency':data['currency'],
           'amount':str(data['amount']),'day':str(data['day']),'source_item_id':str(data['source_item_id']),
           'source_id':str(data['original_source_id']),'source_hash':data['sha256']}
    if arrival is not None:
        leg.update(amount=str(-parsed_amount), purpose=REDEMPTION_PURPOSE,
                   receipt_day=str(arrival), target_account=LICAITONG_CURRENT_PLUS)
    return leg


def _leg(cur, kind, ident, *, lock=True):
    if kind not in {'cashflow', 'trade'} or not _valid_id(ident):
        raise ValueError('invalid fact identity')
    rows = _leg_rows(cur, kind, [ident], lock=lock)
    return _verified_leg(rows[0] if rows else None, kind, SourceOriginals(cur))


def is_independent_equal_pair(left, right):
    if left.get('purpose') == REDEMPTION_PURPOSE and (
        right['kind'] != 'cashflow' or right['account_code'] != left['target_account']
        or right['day'] != left['receipt_day']
    ):
        return False
    return (left['account_code'] != right['account_code'] and left['source_item_id'] != right['source_item_id']
            and left['source_id'] != right['source_id']
            and (left['source_hash'] is None or left['source_hash'] != right['source_hash'])
            and left['currency'] == right['currency']
            and Decimal(left['amount']) < 0 < Decimal(right['amount'])
            and -Decimal(left['amount']) == Decimal(right['amount']))


def occupied_cashflow_ids(cur):
    """Use the same active-confirmation scope as the write path."""
    cur.execute('''SELECT from_cashflow_id, to_cashflow_id FROM public.evidence_confirmation
        WHERE confirmation_type IN ('transfer','payment_proxy') AND revoked_at IS NULL''')
    return {row[key] for row in cur.fetchall() for key in ('from_cashflow_id', 'to_cashflow_id') if row[key] is not None}


def confirm(cur, *, from_kind, from_id, to_kind, to_id, note, confirmed):
    if cur.connection.autocommit or confirmed is not True or not isinstance(note,str) or not note.strip() or len(note)>1000:
        raise ValueError('explicit transfer confirmation and a transaction are required')
    keys=[(from_kind,from_id),(to_kind,to_id)]; accounts=set()
    for kind,ident in keys:
        if kind not in {'cashflow','trade'} or not isinstance(ident,int) or isinstance(ident,bool) or ident<1: raise ValueError('invalid fact identity')
        table,pk=('pams_cashflow_fact','cashflow_id') if kind=='cashflow' else ('pams_investment_trade_fact','trade_id')
        cur.execute(f'SELECT account_code FROM public.{table} WHERE {pk}=%s',(ident,));row=cur.fetchone()
        if not row: raise ValueError('transfer fact missing')
        accounts.add(row['account_code'])
    for account in sorted(accounts): cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('account-import:'+account,))
    legs={key:_leg(cur,*key) for key in sorted(keys)};a,b=legs[keys[0]],legs[keys[1]]
    if not is_independent_equal_pair(a, b):
        raise ValueError('transfer facts conflict or are not independent')
    if a.get('purpose') == REDEMPTION_PURPOSE:
        receipt = _leg_rows(cur, 'cashflow', [to_id], lock=False)[0]
        if (receipt['acquisition_method'] != 'screenshot' or not receipt['sha256']
                or receipt['parsed_fields'].get('action') != 'transfer'
                or _reported_fields(receipt['raw_fields']).get('买入方式') != '其他产品买入'):
            raise ValueError('Target screenshot does not establish a receipt from another product')
    cur.execute('''SELECT c.*,c.confirmation_id AS transfer_id FROM public.evidence_confirmation c WHERE confirmation_type IN ('transfer','payment_proxy') AND revoked_at IS NULL AND
        ((%s='cashflow' AND (from_cashflow_id=%s OR to_cashflow_id=%s)) OR (%s='trade' AND (from_trade_id=%s OR to_trade_id=%s)) OR
         (%s='cashflow' AND (from_cashflow_id=%s OR to_cashflow_id=%s)) OR (%s='trade' AND (from_trade_id=%s OR to_trade_id=%s))) FOR UPDATE OF c''',
        (from_kind,from_id,from_id,from_kind,from_id,from_id,to_kind,to_id,to_id,to_kind,to_id,to_id))
    existing = cur.fetchall()
    if existing:
        if (len(existing) == 1 and existing[0]['confirmation_type'] == 'transfer' and
                existing[0]['from_evidence'] == a and existing[0]['to_evidence'] == b and
                existing[0]['note'] == note.strip()):
            return {'transfer_id': str(existing[0]['transfer_id']), 'replayed': True}
        raise ValueError('transfer leg already confirmed')
    transfer_id=uuid4()
    cur.execute('''INSERT INTO public.evidence_confirmation(confirmation_id,confirmation_type,from_cashflow_id,from_trade_id,
        to_cashflow_id,to_trade_id,from_account_code,to_account_code,amount,currency,from_evidence,to_evidence,note)
        VALUES (%s,'transfer',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (str(transfer_id),from_id if from_kind=='cashflow' else None,from_id if from_kind=='trade' else None,
         to_id if to_kind=='cashflow' else None,to_id if to_kind=='trade' else None,a['account_code'],b['account_code'],
         -Decimal(a['amount']),a['currency'],Json(a),Json(b),note.strip()))
    return {'transfer_id':str(transfer_id), 'replayed':False}


def read(cur, *, limit=200, originals=None):
    """Revalidate both legs of every decision without locking in read-only views."""
    sql = """SELECT c.*,c.confirmation_id AS transfer_id FROM public.evidence_confirmation c
        WHERE c.confirmation_type='transfer' ORDER BY c.confirmed_at DESC,c.confirmation_id DESC"""
    cur.execute(sql + (' LIMIT %s' if limit is not None else ''), (limit,) if limit is not None else ())
    rows = [dict(row) for row in cur.fetchall()]
    ids = {'cashflow': set(), 'trade': set()}
    for row in rows:
        if row['revoked_at'] is None:
            for evidence in (row['from_evidence'], row['to_evidence']):
                if isinstance(evidence, dict):
                    kind, ident = evidence.get('kind'), evidence.get('id')
                    if isinstance(kind, str) and kind in ids and _valid_id(ident):
                        ids[kind].add(ident)
    originals = originals if originals is not None else SourceOriginals(cur)
    verified = {}
    for kind, fact_ids in ids.items():
        if fact_ids:
            facts = _leg_rows(cur, kind, sorted(fact_ids), lock=False)
            originals.verify_many(fact for fact in facts if fact['status']=='published'
                                  and fact['review_status']=='confirmed' and fact['account_code']==fact['source_account'])
            for fact in facts:
                try:
                    verified[kind, fact['id']] = _verified_leg(fact, kind, originals, hash_only=True)
                except ValueError:
                    continue
    for row in rows:
        if row['revoked_at'] is not None:
            row['evidence_status'] = 'revoked'
            continue
        try:
            left, right = row['from_evidence'], row['to_evidence']
            valid = (_valid_id(left['id']) and _valid_id(right['id'])
                     and verified.get((left['kind'], left['id'])) == left
                     and verified.get((right['kind'], right['id'])) == right)
        except (ValueError, KeyError, TypeError):
            valid = False
        row['evidence_status'] = 'confirmed' if valid else 'invalidated'
    return rows
