"""Explicit immutable confirmation of duplicate payment-channel cash facts."""
from decimal import Decimal
from uuid import uuid4
from psycopg2.extras import Json
from domain.imports.isolation import source_enabled
from domain.imports.source_fact_evidence import verify_native_fact


def _leg(cur, ident):
    proof = verify_native_fact(cur, table='pams_cashflow_fact', fact_id=ident)
    if proof.get('status') != 'verified' or proof.get('source_role') == 'derived_bank_leg':
        raise ValueError('payment source evidence is unavailable or not independent')
    cur.execute('''SELECT f.cashflow_id,f.account_code,f.counter_account_code,f.event_date,f.currency,f.amount,
                    f.source_item_id,i.batch_id,i.raw_fields->'source'->'statement' AS statement_source,
                    sf.sha256 AS file_sha256 FROM pams_cashflow_fact f
                    JOIN source_items i ON i.source_item_id=f.source_item_id
                    LEFT JOIN source_files sf ON sf.file_id=i.file_id AND sf.batch_id=i.batch_id WHERE f.cashflow_id=%s''', (ident,))
    r=cur.fetchone()
    if not r or r['statement_source'] is not True or not r['file_sha256']:
        raise ValueError('payment leg requires an independent original statement file')
    if r['amount'] is None or not Decimal(r['amount']).is_finite() or r['amount'] >= 0:
        raise ValueError('payment source must be a known debit')
    return {'id':r['cashflow_id'], 'account_code':r['account_code'], 'counter_account_code':r['counter_account_code'],
            'date':r['event_date'].isoformat(), 'currency':r['currency'], 'amount':str(r['amount']),
            'source_item_id':str(r['source_item_id']), 'batch_id':str(r['batch_id']),
            'file_sha256':str(r['file_sha256']).strip()}


def _unambiguous(cur, left, right):
    cur.execute("""SELECT cashflow_id,account_code FROM pams_cashflow_fact
        WHERE account_code=ANY(%s) AND event_date=%s AND currency=%s AND amount=%s
          AND lower(coalesce(review_status,'')) IN ('auto','reviewed','confirmed')""",
        ([left['account_code'],right['account_code']],left['date'],left['currency'],Decimal(left['amount'])))
    same=cur.fetchall()
    return all(sum(1 for r in same if r['account_code']==account)==1
               for account in (left['account_code'],right['account_code']))


def confirm(cur, *, source_id, target_id, note, confirmed):
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('payment confirmation requires an isolated transaction')
    if confirmed is not True or not isinstance(note,str) or not note.strip() or len(note)>1000:
        raise ValueError('explicit confirmation and note required')
    if any(not isinstance(i,int) or isinstance(i,bool) or i<1 for i in (source_id,target_id)) or source_id==target_id:
        raise ValueError('two distinct payment facts are required')
    ids=(source_id,target_id)
    cur.execute('SELECT cashflow_id,account_code,counter_account_code FROM pams_cashflow_fact WHERE cashflow_id=ANY(%s)', (list(ids),))
    initial={r['cashflow_id']:(r['account_code'],r['counter_account_code']) for r in cur.fetchall()}
    if set(initial)!=set(ids):
        raise ValueError('payment fact missing')
    accounts={a for pair in initial.values() for a in pair if a}
    for account in sorted(accounts):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:'+account,))
    cur.execute('SELECT cashflow_id,account_code,counter_account_code FROM pams_cashflow_fact WHERE cashflow_id=ANY(%s)', (list(ids),))
    if {r['cashflow_id']:(r['account_code'],r['counter_account_code']) for r in cur.fetchall()} != initial:
        raise ValueError('payment account set changed while acquiring locks')
    left,right=(_leg(cur,i) for i in ids)
    if (left['account_code']==right['account_code'] or left['batch_id']==right['batch_id'] or
        left['source_item_id']==right['source_item_id'] or left['file_sha256']==right['file_sha256'] or left['date']!=right['date'] or
        left['currency']!=right['currency'] or Decimal(left['amount'])!=Decimal(right['amount'])):
        raise ValueError('payment facts conflict in source, account, date, currency or amount')
    # Even explicit selection cannot make an ambiguous same-day duplicate unique.
    if not _unambiguous(cur, left, right):
        raise ValueError('ambiguous payment evidence requires prior resolution')
    cur.execute('''SELECT c.* FROM evidence_confirmation c
        WHERE c.confirmation_type IN ('payment_proxy','transfer') AND c.revoked_at IS NULL AND
        (c.from_cashflow_id=ANY(%s) OR c.to_cashflow_id=ANY(%s)) FOR UPDATE OF c''', (list(ids),list(ids)))
    prior=cur.fetchall()
    if prior:
        if len(prior)==1 and prior[0]['confirmation_type']=='payment_proxy' and prior[0]['from_evidence']==left and prior[0]['to_evidence']==right and prior[0]['note']==note.strip():
            return {'confirmation_id':str(prior[0]['confirmation_id']),'replayed':True}
        raise ValueError('payment fact belongs to another active confirmation')
    ident=str(uuid4())
    cur.execute('''INSERT INTO evidence_confirmation
      (confirmation_id,confirmation_type,from_cashflow_id,to_cashflow_id,from_account_code,to_account_code,
       amount,currency,from_evidence,to_evidence,note)
      VALUES (%s,'payment_proxy',%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
      (ident,source_id,target_id,left['account_code'],right['account_code'],-Decimal(left['amount']),left['currency'],Json(left),Json(right),note.strip()))
    return {'confirmation_id':ident,'replayed':False}


def decisions(cur):
    cur.execute('''SELECT c.* FROM evidence_confirmation c
                   WHERE c.confirmation_type='payment_proxy' ORDER BY c.confirmed_at DESC''')
    records=[dict(r) for r in cur.fetchall()]
    for r in records:
        if r['revoked_at'] is not None:
            r['evidence_status']='revoked'
        else:
            try:
                left, right = _leg(cur,r['from_cashflow_id']), _leg(cur,r['to_cashflow_id'])
                r['evidence_status']='confirmed' if left==r['from_evidence'] and right==r['to_evidence'] and _unambiguous(cur,left,right) else 'invalidated'
            except ValueError:
                r['evidence_status']='invalidated'
    return records


def revoke(cur, *, confirmation_id, note, confirmed):
    if cur.connection.autocommit or not source_enabled(cur) or confirmed is not True or not isinstance(note,str) or not note.strip() or len(note)>1000:
        raise ValueError('explicit isolated revocation and note required')
    cur.execute("SELECT from_account_code,to_account_code FROM evidence_confirmation WHERE confirmation_id=%s AND confirmation_type='payment_proxy'", (str(confirmation_id),))
    row=cur.fetchone()
    if not row:
        raise ValueError('payment confirmation not found')
    for account in sorted({row['from_account_code'],row['to_account_code']}):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:'+account,))
    cur.execute('UPDATE evidence_confirmation SET revoked_at=now(),revocation_note=%s WHERE confirmation_id=%s AND revoked_at IS NULL RETURNING confirmation_id', (note.strip(),str(confirmation_id)))
    created=cur.fetchone()
    if not created:
        cur.execute('SELECT revocation_note AS note FROM evidence_confirmation WHERE confirmation_id=%s',(str(confirmation_id),))
        if cur.fetchone()['note']!=note.strip():
            raise ValueError('payment confirmation already revoked with different note')
    return {'confirmation_id':str(confirmation_id),'replayed':not bool(created)}
