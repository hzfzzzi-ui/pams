"""Persist snapshot publication evidence in the active source schema."""
from hashlib import sha256
import json
from uuid import uuid4

from psycopg2.extras import Json

from domain.imports.source_records_v2 import create_source


def published_results(cur, account_code, digest=None):
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS redesigned")
    if cur.fetchone()['redesigned']:
        cur.execute("""SELECT public.source_original_content(account_code,sha256,content) AS content,sha256 FROM source_records
                       WHERE account_code=%s AND source_name='pams_trade_snapshot'
                         AND document_type='holding' AND acquisition_method='api' AND status='published'
                       ORDER BY created_at DESC""", (account_code,))
        results = []
        for row in cur.fetchall():
            payload = bytes(row['content'] or b'')
            if sha256(payload).hexdigest() != row['sha256']:
                raise ValueError('snapshot publication evidence changed')
            result = json.loads(payload)
            if digest is None or result['digest'] == digest:
                results.append(result)
        return results
    cur.execute("""SELECT published_result FROM source_batches
                   WHERE account_code=%s AND workflow='trade_snapshot' AND status='published'
                     AND (%s::text IS NULL OR published_source_hash=%s) ORDER BY created_at DESC""",
                (account_code, digest, digest))
    return [row['published_result'] or {} for row in cur.fetchall()]


def save_publication(cur, *, account_code, as_of, digest, result):
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS redesigned")
    source_id = uuid4()
    scope = {'securities_coverage_confirmed': True, 'cash_coverage': 'unknown'}
    if cur.fetchone()['redesigned']:
        payload = json.dumps({**result, 'digest': digest, **scope}, sort_keys=True, separators=(',', ':')).encode('utf-8')
        create_source(cur, source_id=source_id, account_code=account_code, source_name='pams_trade_snapshot',
                      acquisition_method='api', document_type='holding', file_format='json',
                      content_type='application/json', content=payload)
        cur.execute("""UPDATE source_records SET period_end=%s, status='published'
                       WHERE source_id=%s""", (as_of, str(source_id)))
        return
    cur.execute('''INSERT INTO source_batches
        (batch_id,account_code,business_type,workflow,institution,acquisition_method,date_to,
         completeness,scope_attestation,status,published_source_hash,published_result)
        VALUES (%s,%s,'holding','trade_snapshot','PAMS trade-derived','api',%s,
                'partial',%s,'published',%s,%s)''',
        (str(source_id), account_code, as_of, Json(scope), digest, Json(result)))
