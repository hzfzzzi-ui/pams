"""Immutable, explicitly confirmed links between independent transfer facts."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from psycopg2.extras import Json
from domain.imports.isolation import source_enabled


def _require(cur):
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('transfer confirmation requires an isolated transaction')


def _leg(cur, kind, ident, *, lock=False):
    if kind not in {'cashflow', 'trade'} or not isinstance(ident, int) or ident < 1:
        raise ValueError('invalid transfer fact identity')
    from domain.imports.source_fact_evidence import verify_native_fact
    table = 'pams_cashflow_fact' if kind == 'cashflow' else 'pams_investment_trade_fact'
    if kind == 'trade':
        cur.execute('SELECT source_table FROM pams_investment_trade_fact WHERE trade_id=%s', (ident,))
        source = cur.fetchone()
    else:
        source = None
    if source and source['source_table'] == 'ibkr_flex_cash_transaction':
        from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
        proof = verify_ibkr_fact(cur, trade_id=ident)
        required = {'trade_date', 'account_code', 'currency', 'side', 'gross_amount', 'net_cash',
                    'source_key', 'source_id'}
        verified = proof.get('status') in {'consistent', 'partial'} and not required.intersection(proof.get('unknown_fields', []))
    else:
        proof = verify_native_fact(cur, table=table, fact_id=ident)
        verified = proof.get('status') == 'verified'
    if not verified:
        raise ValueError('transfer leg source evidence is unavailable or inconsistent')
    if kind == 'cashflow':
        sql = '''SELECT f.cashflow_id AS id,f.account_code,f.currency,f.amount AS signed_amount,
                        f.event_date AS day,f.cashflow_type AS direction,f.counter_account_code,f.source_item_id,
                        f.source_table,f.source_id,f.review_status AS fact_review,b.business_type,i.review_status,b.status AS batch_status,
                        b.account_code AS source_account
            FROM public.pams_cashflow_fact f
            JOIN public.source_items i ON i.source_item_id=f.source_item_id
            JOIN public.source_batches b ON b.batch_id=i.batch_id
            WHERE f.cashflow_id=%s'''
    else:
        sql = '''SELECT f.trade_id AS id,f.account_code,f.currency,f.net_cash AS signed_amount,
                        f.trade_date AS day,f.side AS direction,NULL::text AS counter_account_code,f.source_item_id,
                        f.source_table,f.source_id,f.review_status AS fact_review,b.business_type,i.review_status,b.status AS batch_status,
                        b.account_code AS source_account
            FROM public.pams_investment_trade_fact f
            JOIN public.source_items i ON i.source_item_id=f.source_item_id
            JOIN public.source_batches b ON b.batch_id=i.batch_id AND b.account_code=f.account_code
            WHERE f.trade_id=%s'''
    cur.execute(sql + (' FOR UPDATE OF f' if lock else ''), (ident,))
    row = cur.fetchone()
    if (not row or row['review_status'] != 'confirmed' or row['batch_status'] != 'published' or
            str(row['fact_review'] or '').lower() not in {'confirmed','reviewed','auto'}):
        raise ValueError('transfer leg requires an independently published confirmed source')
    creation_source_item_id = str(row['source_item_id'])
    bank_source_confirmation_id = None
    if kind == 'cashflow':
        if row['source_account'] != row['account_code'] or row['business_type'] != 'cash':
            cur.execute("""SELECT c.confirmation_id,c.source_item_id FROM evidence_confirmation c
                WHERE c.confirmation_type='bank_source' AND c.cashflow_id=%s AND c.revoked_at IS NULL""", (ident,))
            candidates = cur.fetchall()
            from domain.ledger.bank_source_confirmation import verified as verified_bank_source
            if len(candidates) != 1 or not verified_bank_source(cur, candidates[0]['confirmation_id']):
                raise ValueError('bank leg requires confirmed cash source, not an inferred bank leg')
            row = dict(row)
            row['source_item_id'] = candidates[0]['source_item_id']
            bank_source_confirmation_id = str(candidates[0]['confirmation_id'])
        if row['direction'] not in {'internal_transfer','investment_funding','investment_withdrawal'}:
            raise ValueError('bank leg is not a confirmed transfer')
    elif row['business_type'] not in {'trade','cash'} or row['direction'] not in {'deposit','withdraw'}:
        raise ValueError('investment leg requires a confirmed deposit or withdrawal source')
    amount = row['signed_amount']
    if amount is None or not Decimal(amount).is_finite() or amount == 0 or not row['currency'] or not row['day']:
        raise ValueError('transfer leg amount, currency or business day is unknown')
    if kind == 'trade' and ((row['direction'] == 'deposit') != (amount > 0)):
        raise ValueError('investment transfer direction conflicts with amount')
    return {'kind':kind, 'id':ident, 'account_code':row['account_code'], 'currency':row['currency'],
            'signed_amount':str(amount), 'day':row['day'].isoformat(), 'direction':row['direction'],
            'counter_account_code':row['counter_account_code'], 'source_item_id':str(row['source_item_id']),
            'creation_source_item_id':creation_source_item_id, 'bank_source_confirmation_id':bank_source_confirmation_id,
            'source_table':row['source_table'], 'source_id':row['source_id'], 'fact_review':row['fact_review']}


def _lock_accounts(cur, accounts):
    for account in sorted(set(accounts)):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))


def confirm(cur, *, from_kind, from_id, to_kind, to_id, note, confirmed, transfer_id=None):
    _require(cur)
    if confirmed is not True or not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError('explicit confirmation and a note are required')
    accounts = set()
    discovered = {}
    for kind, ident in ((from_kind, from_id), (to_kind, to_id)):
        if kind not in {'cashflow', 'trade'} or not isinstance(ident, int) or isinstance(ident, bool) or ident < 1:
            raise ValueError('invalid transfer fact identity')
        table, pk = ('pams_cashflow_fact', 'cashflow_id') if kind == 'cashflow' else ('pams_investment_trade_fact', 'trade_id')
        counter = 'counter_account_code' if kind == 'cashflow' else 'NULL::text'
        cur.execute(f'SELECT account_code,{counter} AS counter_account_code FROM {table} WHERE {pk}=%s', (ident,))
        item = cur.fetchone()
        if not item or not item['account_code']:
            raise ValueError('transfer fact is missing')
        discovered[(kind, ident)] = (item['account_code'], item['counter_account_code'])
        accounts.add(item['account_code'])
        if item['counter_account_code']:
            accounts.add(item['counter_account_code'])
    _lock_accounts(cur, accounts)
    # Lock waiting can overlap an edit. Do not let the verifier acquire an
    # additional account lock outside the original sorted lock set.
    for (kind, ident), expected in discovered.items():
        table, pk = ('pams_cashflow_fact', 'cashflow_id') if kind == 'cashflow' else ('pams_investment_trade_fact', 'trade_id')
        counter = 'counter_account_code' if kind == 'cashflow' else 'NULL::text'
        cur.execute(f'SELECT account_code,{counter} AS counter_account_code FROM {table} WHERE {pk}=%s', (ident,))
        current = cur.fetchone()
        if not current or (current['account_code'], current['counter_account_code']) != expected:
            raise ValueError('transfer account set changed while acquiring locks')
    # Consistent lock order across both orientations prevents cross-confirmation deadlocks.
    keys = sorted(((from_kind, from_id), (to_kind, to_id)))
    locked = {key: _leg(cur, *key, lock=True) for key in keys}
    first, second = locked[(from_kind, from_id)], locked[(to_kind, to_id)]
    if (first['account_code'] == second['account_code'] or first['source_item_id'] == second['source_item_id'] or
        first['currency'] != second['currency'] or Decimal(first['signed_amount']) >= 0 or
        Decimal(second['signed_amount']) <= 0 or -Decimal(first['signed_amount']) != Decimal(second['signed_amount']) or
        (first['counter_account_code'] and first['counter_account_code'] != second['account_code']) or
        (second['counter_account_code'] and second['counter_account_code'] != first['account_code'])):
        raise ValueError('transfer facts conflict in account, source, amount, currency or direction')
    cur.execute('''SELECT c.*,c.confirmation_id AS transfer_id FROM public.evidence_confirmation c
        WHERE c.confirmation_type IN ('transfer','payment_proxy') AND c.revoked_at IS NULL AND (
          (%s='cashflow' AND (c.from_cashflow_id=%s OR c.to_cashflow_id=%s)) OR
          (%s='trade' AND (c.from_trade_id=%s OR c.to_trade_id=%s)) OR
          (%s='cashflow' AND (c.from_cashflow_id=%s OR c.to_cashflow_id=%s)) OR
          (%s='trade' AND (c.from_trade_id=%s OR c.to_trade_id=%s))) FOR UPDATE OF c''',
        (from_kind,from_id,from_id,from_kind,from_id,from_id,to_kind,to_id,to_id,to_kind,to_id,to_id))
    existing = cur.fetchall()
    if existing:
        if len(existing) == 1 and existing[0]['from_evidence'] == first and existing[0]['to_evidence'] == second and existing[0]['note'] == note.strip():
            return {'transfer_id':str(existing[0]['transfer_id']), 'replayed':True}
        raise ValueError('transfer leg already belongs to an active or changed confirmation')
    transfer_id = UUID(str(transfer_id)) if transfer_id else uuid4()
    cur.execute('''INSERT INTO public.evidence_confirmation
        (confirmation_id,confirmation_type,from_cashflow_id,from_trade_id,to_cashflow_id,to_trade_id,from_account_code,to_account_code,
         amount,currency,from_evidence,to_evidence,note)
        VALUES (%s,'transfer',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (str(transfer_id),from_id if from_kind == 'cashflow' else None,from_id if from_kind == 'trade' else None,
         to_id if to_kind == 'cashflow' else None,to_id if to_kind == 'trade' else None,
         first['account_code'],second['account_code'],-Decimal(first['signed_amount']),first['currency'],
         Json(first),Json(second),note.strip()))
    return {'transfer_id':str(transfer_id), 'replayed':False}


def revoke(cur, *, transfer_id, note, confirmed):
    _require(cur)
    if confirmed is not True or not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError('explicit revocation and a note are required')
    cur.execute("SELECT * FROM public.evidence_confirmation WHERE confirmation_id=%s AND confirmation_type='transfer'", (str(transfer_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('transfer confirmation not found')
    _lock_accounts(cur, (row['from_account_code'],row['to_account_code']))
    cur.execute('''UPDATE public.evidence_confirmation SET revoked_at=now(),revocation_note=%s
        WHERE confirmation_id=%s AND revoked_at IS NULL RETURNING confirmation_id''', (note.strip(),str(transfer_id)))
    created = cur.fetchone()
    if not created:
        cur.execute('SELECT revocation_note AS note FROM public.evidence_confirmation WHERE confirmation_id=%s', (str(transfer_id),))
        if cur.fetchone()['note'] != note.strip():
            raise ValueError('transfer already revoked with a different note')
    return {'transfer_id':str(transfer_id), 'replayed':not bool(created)}


def _state(cur, row):
    item = dict(row)
    if item['revoked_at'] is not None:
        item['evidence_status'] = 'revoked'
    else:
        try:
            start, end = item['from_evidence'], item['to_evidence']
            valid = _leg(cur,start['kind'],start['id']) == start and _leg(cur,end['kind'],end['id']) == end
        except ValueError:
            valid = False
        item['evidence_status'] = 'confirmed' if valid else 'invalidated'
    return item


def read_one(cur, transfer_id):
    cur.execute("""SELECT c.*,c.confirmation_id AS transfer_id FROM public.evidence_confirmation c
        WHERE c.confirmation_id=%s AND c.confirmation_type='transfer'""", (str(transfer_id),))
    row = cur.fetchone()
    return _state(cur, row) if row else None


def read(cur, *, limit=200):
    sql = """SELECT c.*,c.confirmation_id AS transfer_id FROM public.evidence_confirmation c
        WHERE c.confirmation_type='transfer' ORDER BY c.confirmed_at DESC,c.confirmation_id DESC"""
    cur.execute(sql + (' LIMIT %s' if limit is not None else ''), (limit,) if limit is not None else ())
    return [_state(cur, row) for row in cur.fetchall()]
