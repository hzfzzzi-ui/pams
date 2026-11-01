"""Explicitly link a later bank statement to an existing derived bank leg."""
from decimal import Decimal, InvalidOperation
from uuid import UUID
from psycopg2.extras import Json
from domain.imports.isolation import source_enabled


def evidence(cur, *, cashflow_id, source_item_id, lock=False):
    from domain.imports.source_fact_evidence import _reviewed_snapshot, verify_native_fact
    proof = verify_native_fact(cur, table='pams_cashflow_fact', fact_id=cashflow_id)
    if proof.get('status') != 'verified' or proof.get('source_role') != 'derived_bank_leg':
        raise ValueError('derived bank leg source evidence is unavailable or inconsistent')
    cur.execute("SHOW transaction_read_only")
    row = cur.fetchone()
    readonly = next(iter(row.values())) == 'on' if isinstance(row, dict) else row[0] == 'on'
    cur.execute("SHOW transaction_isolation")
    row = cur.fetchone()
    isolation = next(iter(row.values())) if isinstance(row, dict) else row[0]
    if readonly and isolation not in {'repeatable read', 'serializable'}:
        raise ValueError('bank source requires a consistent snapshot')
    share = '' if readonly else ' FOR SHARE'
    cur.execute('SELECT batch_id FROM source_items WHERE source_item_id=%s', (str(source_item_id),))
    link = cur.fetchone()
    if not link:
        raise ValueError('bank source item missing')
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s' + share, (str(link['batch_id']),))
    batch = cur.fetchone()
    if not batch or batch['status'] != 'published' or batch['workflow'] != 'account_import':
        raise ValueError('bank source batch is not published')
    if not _reviewed_snapshot(cur, batch, str(batch['batch_id']), locking=not readonly, snapshot_cache={}):
        raise ValueError('bank source snapshot is inconsistent')
    result = batch.get('published_result') or {}
    records = result.get('bank_source_confirmations') or []
    matches = [r for r in records if str(r.get('source_item_id')) == str(source_item_id)
               and str(r.get('cashflow_id')) == str(cashflow_id)]
    if len(matches) != 1:
        raise ValueError('bank source publication association is inconsistent')
    cur.execute('''SELECT f.cashflow_id,f.account_code,f.counter_account_code,f.amount,f.currency,f.event_date,
            f.source_item_id AS original_source_item_id,f.source_table,f.source_id,f.raw_json,f.review_status AS fact_review,
            i.source_item_id,i.record_hash,i.raw_fields,i.parsed_fields,i.review_status,
            b.account_code AS source_account,b.business_type,b.status
        FROM pams_cashflow_fact f JOIN source_items i ON i.source_item_id=%s
        JOIN source_batches b ON b.batch_id=i.batch_id
        WHERE f.cashflow_id=%s''' + (' FOR UPDATE OF f' if lock else ''), (str(source_item_id),cashflow_id))
    r = cur.fetchone()
    if not r:
        raise ValueError('derived leg or bank source is missing')
    raw = r['raw_json'] if isinstance(r['raw_json'], dict) else {}
    item_raw = r['raw_fields'] if isinstance(r['raw_fields'], dict) else {}
    parsed = r['parsed_fields'] if isinstance(r['parsed_fields'], dict) else {}
    fields = parsed.get('fields') if isinstance(parsed.get('fields'),dict) else {}
    try:
        amount = Decimal(str(fields.get('net_cash')))
    except InvalidOperation as exc:
        raise ValueError('bank statement amount is invalid') from exc
    if (raw.get('evidence_role') != 'derived_bank_leg' or raw.get('independent_bank_evidence') is not False or
        str(r['original_source_item_id']) == str(r['source_item_id']) or r['source_account'] != r['account_code'] or
        str(r['fact_review'] or '').lower() not in {'confirmed','reviewed','auto'} or
        r['business_type'] != 'cash' or r['status'] != 'published' or r['review_status'] != 'confirmed' or
        not isinstance(item_raw.get('source'), dict) or item_raw['source'].get('statement') is not True or
        parsed.get('acknowledged') is not True or fields.get('action') != 'transfer' or
        fields.get('counter_account_code') != r['counter_account_code'] or
        fields.get('business_date') != r['event_date'].isoformat() or fields.get('currency') != r['currency'] or
        not amount.is_finite() or amount != r['amount'] or str(fields.get('derived_cashflow_id') or '') != str(cashflow_id)):
        raise ValueError('bank statement does not independently confirm the derived leg')
    return {'cashflow_id':cashflow_id, 'source_item_id':str(r['source_item_id']),
            'original_source_item_id':str(r['original_source_item_id']), 'source_table':r['source_table'],
            'source_id':r['source_id'], 'account_code':r['account_code'], 'counter_account_code':r['counter_account_code'],
            'amount':str(r['amount']), 'currency':r['currency'], 'event_date':r['event_date'].isoformat(),
            'record_hash':r['record_hash'], 'parsed_fields':parsed, 'fact_review':r['fact_review']}


def confirm(cur, *, cashflow_id, source_item_id, confirmation_id):
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('bank source confirmation requires an isolated transaction')
    proof = evidence(cur, cashflow_id=cashflow_id, source_item_id=source_item_id, lock=True)
    cur.execute('''SELECT c.* FROM evidence_confirmation c
        WHERE c.confirmation_type='bank_source' AND c.revoked_at IS NULL AND (c.cashflow_id=%s OR c.source_item_id=%s) FOR UPDATE OF c''',
        (cashflow_id,str(source_item_id)))
    old = cur.fetchall()
    if old:
        if len(old)==1 and old[0]['evidence']==proof:
            return str(old[0]['confirmation_id'])
        raise ValueError('bank source or derived leg already confirmed elsewhere')
    confirmation_id = UUID(str(confirmation_id))
    cur.execute('''INSERT INTO evidence_confirmation(confirmation_id,confirmation_type,cashflow_id,source_item_id,evidence,note)
        VALUES (%s,'bank_source',%s,%s,%s,%s)''', (str(confirmation_id),cashflow_id,str(source_item_id),Json(proof),
        '用户核准真实银行资料与已有同源派生腿关联'))
    return str(confirmation_id)


def verified(cur, confirmation_id):
    cur.execute('''SELECT c.* FROM evidence_confirmation c
        WHERE c.confirmation_id=%s AND c.confirmation_type='bank_source' AND c.revoked_at IS NULL''', (str(confirmation_id),))
    row = cur.fetchone()
    if not row:
        return False
    try:
        return evidence(cur, cashflow_id=row['cashflow_id'], source_item_id=row['source_item_id']) == row['evidence']
    except ValueError:
        return False


def revoke(cur, *, confirmation_id, note, confirmed):
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('bank source revocation requires an isolated transaction')
    if confirmed is not True or not isinstance(note,str) or not note.strip() or len(note) > 1000:
        raise ValueError('explicit revocation and a note are required')
    cur.execute("""SELECT c.*,f.account_code,f.counter_account_code FROM evidence_confirmation c
        JOIN pams_cashflow_fact f ON f.cashflow_id=c.cashflow_id WHERE c.confirmation_id=%s AND c.confirmation_type='bank_source'""", (str(confirmation_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('bank source confirmation not found')
    from domain.investments.holding_evidence_writer import lock_holding_accounts
    lock_holding_accounts(cur, (row['account_code'],row['counter_account_code']))
    cur.execute("""UPDATE evidence_confirmation SET revoked_at=now(),revocation_note=%s
        WHERE confirmation_id=%s AND revoked_at IS NULL RETURNING confirmation_id""", (note.strip(),str(confirmation_id)))
    created = cur.fetchone()
    if not created:
        cur.execute('SELECT revocation_note AS note FROM evidence_confirmation WHERE confirmation_id=%s', (str(confirmation_id),))
        if cur.fetchone()['note'] != note.strip():
            raise ValueError('bank source confirmation already revoked with a different note')
    return {'confirmation_id':str(confirmation_id), 'replayed':not bool(created)}
