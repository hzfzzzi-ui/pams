"""Attach a reviewed source to an existing investment fact without creating a trade."""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from uuid import uuid4
from pathlib import PurePath

from psycopg2.extras import Json

from domain.imports.source_fact_evidence import verify_native_fact
from domain.investments.investment_trade_fact import classify_trade_type


def stage(cur, *, trade_id, filename, content):
    from io import BytesIO
    from PIL import Image
    from domain.imports.account_import_ocr import _validate_image
    from domain.imports.source_records_v2 import create_source
    if (cur.connection.autocommit or type(trade_id) is not int or trade_id <= 0
            or not filename or len(filename) > 180 or any(c in filename for c in ('/', '\\', '\x00', '\r', '\n'))):
        raise ValueError('invalid trade source')
    suffix = PurePath(filename).suffix.lower().lstrip('.')
    if suffix not in {'png', 'jpg', 'jpeg'}:
        raise ValueError('supplementary source must be PNG or JPEG')
    _validate_image(content)
    with Image.open(BytesIO(content)) as image:
        actual = image.format
    if (suffix == 'png' and actual != 'PNG') or (suffix in {'jpg', 'jpeg'} and actual != 'JPEG'):
        raise ValueError('image format does not match filename')
    cur.execute('SELECT account_code FROM pams_investment_trade_fact WHERE trade_id=%s', (trade_id,))
    fact = cur.fetchone()
    if not fact:
        raise ValueError('investment fact not found')
    account = fact['account_code']
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + account,))
    if verify_native_fact(cur, table='pams_investment_trade_fact', fact_id=trade_id)['status'] != 'verified':
        raise ValueError('investment fact source is not verified')
    digest = sha256(content).hexdigest()
    cur.execute("""SELECT source_id,revision,status,source_name FROM source_records
        WHERE account_code=%s AND document_type='trade' AND sha256=%s AND status IN ('draft','published')""", (account, digest))
    existing = cur.fetchone()
    if existing:
        if existing['source_name'] != 'supplemental_trade_evidence':
            raise ValueError('original screenshot already exists in another source')
        return {'source_id': str(existing['source_id']), 'revision': existing['revision'], 'status': existing['status'], 'replayed': True}
    source_id = uuid4()
    create_source(cur, source_id=source_id, account_code=account, source_name='supplemental_trade_evidence',
                  acquisition_method='screenshot', document_type='trade', filename=filename, file_format=suffix,
                  content_type='image/png' if suffix == 'png' else 'image/jpeg', content=content)
    return {'source_id': str(source_id), 'revision': 1, 'status': 'draft', 'replayed': False}


def _decimal(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _source_intact(row):
    """An explicit manual statement is evidence, not an external document."""
    if row['acquisition_method'] == 'manual':
        return (row['content'] is None and row['sha256'] is None
                and row['raw_fields'].get('entered_fields') == row['parsed_fields'])
    return (row['acquisition_method'] in {'screenshot', 'file'}
            and row['content'] is not None
            and sha256(bytes(row['content'])).hexdigest() == row['sha256'])


def attach(cur, *, source_id, item_id, trade_id, expected_revision, note, confirmed):
    if (cur.connection.autocommit or confirmed is not True or type(trade_id) is not int or trade_id <= 0
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(note, str) or not note.strip() or len(note) > 1000):
        raise ValueError('explicit confirmation, revision and note required')
    cur.execute('SELECT account_code FROM pams_investment_trade_fact WHERE trade_id=%s', (trade_id,))
    fact = cur.fetchone()
    if not fact:
        raise ValueError('investment fact not found')
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + fact['account_code'],))
    if verify_native_fact(cur, table='pams_investment_trade_fact', fact_id=trade_id)['status'] != 'verified':
        raise ValueError('investment fact source is not verified')
    cur.execute("""SELECT f.trade_id,f.account_code,f.trade_date,f.asset_id,f.symbol,f.trade_type,f.currency,f.net_cash,
        i.source_item_id,i.raw_fields,i.parsed_fields,i.record_hash,i.review_status,
        r.source_id,r.account_code AS source_account,r.status,r.revision,r.document_type,r.acquisition_method,
        r.content,r.sha256 FROM pams_investment_trade_fact f
        JOIN source_records r ON r.source_id=%s
        JOIN source_items i ON i.source_id=r.source_id AND i.source_item_id=%s
        WHERE f.trade_id=%s FOR UPDATE OF r,i""", (str(source_id), str(item_id), trade_id))
    row = cur.fetchone()
    if not row:
        raise ValueError('supplementary source not found')
    cur.execute("SELECT confirmation_id,from_trade_id,note FROM evidence_confirmation WHERE confirmation_type='trade_source' AND source_item_id=%s AND revoked_at IS NULL FOR UPDATE", (str(item_id),))
    previous = cur.fetchone()
    if previous:
        if previous['from_trade_id'] != trade_id or previous['note'] != note.strip():
            raise ValueError('source already attached with a different note')
        return {'confirmation_id': str(previous['confirmation_id']), 'replayed': True}
    cur.execute('SELECT count(*) AS n FROM source_items WHERE source_id=%s', (str(source_id),))
    if (row['status'] not in {'draft', 'published'} or row['revision'] != expected_revision or row['review_status'] != 'confirmed'
            or row['document_type'] != 'trade' or not _source_intact(row)
            or row['source_account'] != row['account_code'] or cur.fetchone()['n'] != 1
            ):
        raise ValueError('reviewed original source is unavailable or changed')
    raw = json.dumps(row['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    fields = row['parsed_fields']
    if sha256(raw).hexdigest() != row['record_hash']:
        raise ValueError('supplementary source item changed')
    raw_type = fields.get('trade_type') or fields.get('action')
    if (fields.get('business_date') != row['trade_date'].isoformat() or fields.get('symbol') != row['symbol']
            or fields.get('currency') != row['currency'] or _decimal(fields.get('net_cash')) != row['net_cash']
            or classify_trade_type({'side': raw_type, 'symbol': row['symbol']}) != row['trade_type']):
        raise ValueError('supplementary source differs from existing trade identity or amount')
    if fields.get('complete_exit') is True:
        ids = fields.get('complete_trade_ids')
        if (row['trade_type'] not in {'sell', 'fund_redemption'} or fields.get('complete_history') is not True
                or not row['asset_id'] or fields.get('asset_id') != row['asset_id']
                or not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids)
                or len(ids) != len(set(ids)) or trade_id not in ids):
            raise ValueError('complete exit requires an explicit complete trade identity list')
    cur.execute('SELECT 1 FROM pams_investment_trade_fact WHERE source_item_id=%s LIMIT 1', (str(item_id),))
    if cur.fetchone():
        raise ValueError('supplementary source already generated a trade')
    proof = {'source_id': str(source_id), 'item_id': str(item_id), 'sha256': row['sha256'],
             'record_hash': row['record_hash'], 'trade_id': trade_id, 'fields': fields}
    ident = str(uuid4())
    cur.execute("""INSERT INTO evidence_confirmation
        (confirmation_id,confirmation_type,from_trade_id,source_item_id,evidence,note)
        VALUES (%s,'trade_source',%s,%s,%s,%s)""", (ident, trade_id, str(item_id), Json(proof), note.strip()))
    if row['status'] == 'draft':
        cur.execute("UPDATE source_records SET status='published',revision=revision+1,updated_at=now() WHERE source_id=%s", (str(source_id),))
    return {'confirmation_id': ident, 'replayed': False}


def read(cur, *, trade_id):
    if type(trade_id) is not int or trade_id <= 0:
        raise ValueError('invalid trade identity')
    status = verify_native_fact(cur, table='pams_investment_trade_fact', fact_id=trade_id)['status']
    cur.execute('SELECT account_code,trade_date,asset_id,symbol,trade_type,currency,net_cash FROM pams_investment_trade_fact WHERE trade_id=%s', (trade_id,))
    fact = cur.fetchone()
    cur.execute("""SELECT e.confirmation_id,e.evidence,e.note,i.source_item_id,i.raw_fields,i.parsed_fields,i.record_hash,
        i.review_status,r.source_id,r.account_code,r.status,r.sha256,r.content,r.acquisition_method
        FROM evidence_confirmation e JOIN source_items i ON i.source_item_id=e.source_item_id
        JOIN source_records r ON r.source_id=i.source_id
        WHERE e.confirmation_type='trade_source' AND e.from_trade_id=%s AND e.revoked_at IS NULL
        ORDER BY e.confirmed_at,e.confirmation_id""", (trade_id,))
    rows = cur.fetchall()
    result = []
    for row in rows:
        proof = row['evidence']
        raw = json.dumps(row['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        fields = row['parsed_fields']
        verified = (status == 'verified' and fact is not None and row['account_code'] == fact['account_code']
                    and fields.get('business_date') == fact['trade_date'].isoformat()
                    and fields.get('symbol') == fact['symbol'] and fields.get('currency') == fact['currency']
                    and (fields.get('complete_exit') is not True or
                         (fact['asset_id'] is not None and fields.get('asset_id') == fact['asset_id']))
                    and _decimal(fields.get('net_cash')) == fact['net_cash']
                    and classify_trade_type({'side': fields.get('trade_type') or fields.get('action'), 'symbol': fact['symbol']}) == fact['trade_type']
                    and row['status'] == 'published' and row['review_status'] == 'confirmed'
                    and _source_intact(row)
                    and sha256(raw).hexdigest() == row['record_hash']
                    and proof.get('trade_id') == trade_id and proof.get('item_id') == str(row['source_item_id'])
                    and proof.get('source_id') == str(row['source_id']) and proof.get('sha256') == row['sha256']
                    and proof.get('record_hash') == row['record_hash'] and proof.get('fields') == row['parsed_fields'])
        result.append({'confirmation_id': str(row['confirmation_id']), 'status': 'verified' if verified else 'unknown',
                       'note': row['note'], 'fields': row['parsed_fields'] if verified else None})
    return result


def revoke(cur, *, confirmation_id, note, confirmed):
    if cur.connection.autocommit or confirmed is not True or not isinstance(note, str) or not note.strip():
        raise ValueError('explicit revocation and note required')
    cur.execute("""SELECT f.account_code FROM evidence_confirmation e JOIN pams_investment_trade_fact f
        ON f.trade_id=e.from_trade_id WHERE e.confirmation_id=%s AND e.confirmation_type='trade_source'""", (str(confirmation_id),))
    row = cur.fetchone()
    if not row:
        raise ValueError('supplementary evidence confirmation not found')
    cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + row['account_code'],))
    cur.execute("""UPDATE evidence_confirmation SET revoked_at=now(),revocation_note=%s
        WHERE confirmation_id=%s AND confirmation_type='trade_source' AND revoked_at IS NULL RETURNING confirmation_id""",
        (note.strip(), str(confirmation_id)))
    if cur.fetchone():
        return {'confirmation_id': str(confirmation_id), 'replayed': False}
    cur.execute('SELECT revocation_note FROM evidence_confirmation WHERE confirmation_id=%s', (str(confirmation_id),))
    if cur.fetchone()['revocation_note'] != note.strip():
        raise ValueError('evidence already revoked with a different note')
    return {'confirmation_id': str(confirmation_id), 'replayed': True}
