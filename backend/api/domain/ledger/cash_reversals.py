"""User-confirmed reversal of a payment in the same account."""
from decimal import Decimal
from uuid import uuid4

from psycopg2.extras import Json

from domain.ledger.confirmed_transfers_v2 import _leg, occupied_cashflow_ids


def _valid(left, right):
    return (left['kind'] == right['kind'] == 'cashflow' and left['id'] != right['id']
            and left['account_code'] == right['account_code'] and left['currency'] == right['currency']
            and left['source_item_id'] != right['source_item_id'] and left['day'] <= right['day']
            and Decimal(left['amount']) < 0 < Decimal(right['amount'])
            and -Decimal(left['amount']) == Decimal(right['amount']))


def confirm(cur, *, debit_id, credit_id, note, confirmed):
    if (cur.connection.autocommit or confirmed is not True or not isinstance(note, str)
            or not note.strip() or len(note) > 1000 or type(debit_id) is not int
            or type(credit_id) is not int or debit_id <= 0 or credit_id <= 0 or debit_id == credit_id):
        raise ValueError('explicit confirmation and two different cash facts are required')
    cur.execute('SELECT DISTINCT account_code FROM pams_cashflow_fact WHERE cashflow_id=ANY(%s)', ([debit_id, credit_id],))
    accounts = sorted(row['account_code'] for row in cur.fetchall())
    if len(accounts) != 1:
        raise ValueError('reversal legs must belong to the same account')
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + accounts[0],))
    left, right = _leg(cur, 'cashflow', debit_id), _leg(cur, 'cashflow', credit_id)
    if not _valid(left, right):
        raise ValueError('reversal facts do not match')
    cur.execute("""SELECT * FROM evidence_confirmation WHERE revoked_at IS NULL
        AND confirmation_type IN ('transfer','payment_proxy','cash_reversal')
        AND (from_cashflow_id=ANY(%s) OR to_cashflow_id=ANY(%s)) FOR UPDATE""",
        ([debit_id, credit_id], [debit_id, credit_id]))
    existing = cur.fetchall()
    if existing:
        row = existing[0]
        if (len(existing) == 1 and row['confirmation_type'] == 'cash_reversal'
                and row['from_evidence'] == left and row['to_evidence'] == right and row['note'] == note.strip()):
            return {'confirmation_id': str(row['confirmation_id']), 'replayed': True}
        raise ValueError('cash fact already belongs to a confirmation')
    if {debit_id, credit_id} & occupied_cashflow_ids(cur):
        raise ValueError('cash fact already belongs to a confirmation')
    ident = str(uuid4())
    cur.execute("""INSERT INTO evidence_confirmation
        (confirmation_id,confirmation_type,from_cashflow_id,to_cashflow_id,from_account_code,to_account_code,
         amount,currency,from_evidence,to_evidence,note)
        VALUES (%s,'cash_reversal',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (ident, debit_id, credit_id, left['account_code'], right['account_code'], -Decimal(left['amount']),
         left['currency'], Json(left), Json(right), note.strip()))
    return {'confirmation_id': ident, 'replayed': False}


def confirmed_cashflow_ids(cur):
    """Only exclude facts whose original evidence still verifies."""
    cur.execute("""SELECT from_evidence,to_evidence FROM evidence_confirmation
        WHERE confirmation_type='cash_reversal' AND revoked_at IS NULL""")
    rows = cur.fetchall()
    ids = set()
    for row in rows:
        left, right = row['from_evidence'], row['to_evidence']
        try:
            if (_valid(left, right) and _leg(cur, 'cashflow', left['id'], lock=False) == left
                    and _leg(cur, 'cashflow', right['id'], lock=False) == right):
                ids.update((left['id'], right['id']))
        except (KeyError, ValueError, TypeError):
            continue
    return ids


def revoke(cur, *, confirmation_id, note, confirmed):
    if cur.connection.autocommit or confirmed is not True or not isinstance(note, str) or not note.strip():
        raise ValueError('explicit revocation and note required')
    cur.execute("SELECT from_account_code FROM evidence_confirmation WHERE confirmation_id=%s AND confirmation_type='cash_reversal'", (str(confirmation_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('reversal confirmation not found')
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + row['from_account_code'],))
    cur.execute("""UPDATE evidence_confirmation SET revoked_at=now(),revocation_note=%s
        WHERE confirmation_id=%s AND confirmation_type='cash_reversal' AND revoked_at IS NULL RETURNING confirmation_id""",
        (note.strip(), str(confirmation_id)))
    if cur.fetchone():
        return {'confirmation_id': str(confirmation_id), 'replayed': False}
    cur.execute('SELECT revocation_note FROM evidence_confirmation WHERE confirmation_id=%s', (str(confirmation_id),))
    if cur.fetchone()['revocation_note'] != note.strip():
        raise ValueError('reversal already revoked with a different note')
    return {'confirmation_id': str(confirmation_id), 'replayed': True}
