"""Account snapshot coverage proofs for isolated, source-bound valuation."""
from uuid import UUID, uuid4
from psycopg2.extras import Json, RealDictCursor, execute_values

from domain.imports.isolation import source_enabled
from domain.investments.holding_evidence_writer import lock_holding_accounts


def record_partial(conn, *, account_code, as_of, holding_ids, reason, calculation_version):
    """Save actual member identities and row hashes without asserting completeness."""
    if conn.autocommit or not account_code or not as_of or not reason or not calculation_version:
        raise ValueError('account snapshot scope or transaction is invalid')
    if not isinstance(holding_ids, list) or not holding_ids or len(set(holding_ids)) != len(holding_ids) or any(
            not isinstance(ident,int) or isinstance(ident,bool) or ident < 1 for ident in holding_ids):
        raise ValueError('account snapshot members must be nonempty distinct holding IDs')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if not source_enabled(cur):
            raise ValueError('account snapshot evidence requires isolated mode')
        lock_holding_accounts(cur, [account_code])
        cur.execute('''SELECT id,account_code,as_of,md5(to_jsonb(h)::text) AS row_hash
            FROM public.pams_holdings_snapshot h WHERE id=ANY(%s::bigint[]) ORDER BY id FOR SHARE''', (holding_ids,))
        actual = cur.fetchall()
        if (len(actual) != len(holding_ids) or any(row['account_code'] != account_code or row['as_of'] != as_of for row in actual)):
            raise ValueError('account snapshot members do not match the requested scope')
        expected = [(row['id'],row['row_hash']) for row in actual]
        cur.execute('''SELECT * FROM public.account_snapshot_version WHERE account_code=%s AND as_of=%s
            ORDER BY version_number DESC LIMIT 1 FOR SHARE''', (account_code,as_of))
        previous = cur.fetchone()
        if previous and previous['coverage_status']=='partial' and previous['cash_status']=='unknown' and previous['reason']==reason and previous['calculation_version']==calculation_version:
            cur.execute('''SELECT holding_id,holding_row_hash FROM public.account_snapshot_member
                WHERE version_id=%s ORDER BY holding_id''', (str(previous['version_id']),))
            if [(row['holding_id'],row['holding_row_hash']) for row in cur.fetchall()] == expected:
                return str(previous['version_id'])
        identifier = uuid4()
        execute_values(cur, '''INSERT INTO public.account_snapshot_member
            (version_id,holding_id,holding_row_hash) VALUES %s''',
            [(str(identifier),ident,digest) for ident,digest in expected])
        cur.execute('''INSERT INTO public.account_snapshot_version
            (version_id,account_code,as_of,coverage_status,cash_status,cash_amount,calculation_version,reason)
            VALUES (%s,%s,%s,'partial','unknown',NULL,%s,%s)''',
            (str(identifier),account_code,as_of,calculation_version,reason))
        return str(identifier)


def _complete_member_proven(cur, holding_id, source_item_id):
    """Verify immutable source and exact holding version using read-only SQL."""
    import json
    from decimal import Decimal
    from domain.investments.valuation_input_evidence import _hash, verify_recorded_version
    cur.execute("""SELECT row_to_json(h)::text AS holding_json,
            json_build_object('item', row_to_json(i), 'batch', row_to_json(b))::text AS source_json
        FROM pams_holdings_snapshot h JOIN source_items i ON i.source_item_id=%s
        JOIN source_batches b ON b.batch_id=i.batch_id WHERE h.id=%s""", (str(source_item_id),holding_id))
    row = cur.fetchone()
    if not row:
        return False
    holding = json.loads(row['holding_json'], parse_float=Decimal)
    source = json.loads(row['source_json'], parse_float=Decimal)
    batch, item = source.get('batch') or {}, source.get('item') or {}
    if (batch.get('status') != 'published' or item.get('review_status') != 'confirmed' or
        batch.get('account_code') != holding.get('account_code') or batch.get('business_type') != 'holding' or
        (batch.get('date_from') and batch['date_from'] > holding.get('as_of')) or
        (batch.get('date_to') and batch['date_to'] < holding.get('as_of'))):
        return False
    cur.execute("""SELECT v.version_key,h.evidence AS holding_evidence,s.evidence AS source_evidence
        FROM valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS h(input_kind text,input_hash text,evidence jsonb)
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS s(input_kind text,input_hash text,evidence jsonb)
        WHERE h.input_kind='holding' AND h.evidence->'source_ref'->'key'->>'id'=%s
          AND s.input_kind='source_item' AND s.evidence->>'role'='current_source'
          AND s.evidence->'source_ref'->'key'->>'source_item_id'=%s""", (str(holding_id),str(source_item_id)))
    rows = cur.fetchall()
    for proof in rows:
        if ((proof['holding_evidence'].get('snapshot') or {}).get('content_hash') == _hash(holding) and
            (proof['source_evidence'].get('snapshot') or {}).get('content_hash') == _hash(source) and
            verify_recorded_version(cur, proof['version_key'])):
            return True
    return False



def _unresolved_prior_holdings(cur, account_code, as_of):
    """An absent earlier member is not evidence of an exit from the account."""
    cur.execute("""SELECT EXISTS (
        SELECT 1 FROM pams_holdings_snapshot prior
        WHERE prior.account_code=%s AND prior.as_of<%s
          AND NOT EXISTS (SELECT 1 FROM pams_holdings_snapshot current
              WHERE current.account_code=prior.account_code AND current.as_of=%s AND current.name=prior.name
                AND current.currency=prior.currency)
    ) AS unresolved""", (account_code,as_of,as_of))
    return bool(cur.fetchone()['unresolved'])

def record_complete(cur, *, account_code, as_of, members, source_batch_id, source_revision, source_sha256, version_id=None):
    """Publish only a reviewed account-wide human attestation bound to every member."""
    from decimal import Decimal
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('complete scope requires an isolated transaction')
    lock_holding_accounts(cur, [account_code])
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s FOR SHARE', (str(source_batch_id),))
    batch = cur.fetchone()
    attestation = batch['scope_attestation'] if batch else None
    if (not batch or batch['status'] != 'published' or batch['business_type'] != 'holding' or
        batch['workflow'] != 'account_import' or batch['account_code'] != account_code or
        batch['revision'] != source_revision or batch['published_source_hash'] != source_sha256 or
        not isinstance(attestation,dict) or attestation.get('kind') != 'user_confirmed_complete_account' or
        attestation.get('account_code') != account_code or attestation.get('as_of') != as_of.isoformat() or
        attestation.get('revision') != source_revision or not attestation.get('note')):
        raise ValueError('complete scope requires a current published explicit account attestation')
    if not members or len(members) != attestation.get('member_count'):
        raise ValueError('complete scope member count differs')
    item_ids = {str(item['source_item_id']) for item in members}
    if len(item_ids) != len(members) or item_ids != {item['source_item_id'] for item in attestation.get('members',[])}:
        raise ValueError('complete scope source members differ')
    ids = [item['holding_id'] for item in members]
    if len(set(ids)) != len(ids):
        raise ValueError('complete scope holding identity is ambiguous')
    cur.execute('''SELECT id,account_code,as_of,major_category,minor_category,asset_liability,asset_id,currency,
        market_value,symbol,name,quantity,cost,current_price,buy_price,md5(to_jsonb(h)::text) AS row_hash
        FROM pams_holdings_snapshot h WHERE account_code=%s AND as_of=%s ORDER BY id FOR SHARE''', (account_code,as_of))
    rows = cur.fetchall()
    if {row['id'] for row in rows} != set(ids):
        raise ValueError('complete scope does not cover all current account rows')
    if _unresolved_prior_holdings(cur,account_code,as_of):
        raise ValueError('complete scope has unresolved previously known members')
    cash = [row for row in rows if row['major_category']=='现金']
    if len(cash) != 1 or cash[0]['currency'] != 'CNY' or cash[0]['market_value'] != Decimal(attestation['cash_amount']):
        raise ValueError('complete scope cash differs from reviewed evidence')
    refs = {item['holding_id']:str(item['source_item_id']) for item in members}
    attested = {item['source_item_id']: item for item in attestation['members']}
    for row in rows:
        stated = attested[refs[row['id']]]
        if (any(row[field] != stated[field] for field in ('symbol','name','asset_id','currency','major_category','minor_category','asset_liability')) or
            any(row[field] is None or Decimal(stated[field]) != row[field] for field in ('quantity','cost','market_value','current_price','buy_price'))):
            raise ValueError('complete scope member value differs from explicit attestation')
        cur.execute("SELECT 1 FROM source_items WHERE source_item_id=%s AND batch_id=%s AND review_status='confirmed'",
                    (refs[row['id']],str(source_batch_id)))
        if not cur.fetchone() or not _complete_member_proven(cur,row['id'],refs[row['id']]):
            raise ValueError('complete scope member lacks current reviewed source evidence')
    identifier = UUID(str(version_id)) if version_id else uuid4()
    # Source-item FK locks must be acquired in a consistent order across accounts.
    snapshot_members = sorted(
        [(str(identifier),row['id'],row['row_hash'],refs[row['id']]) for row in rows],
        key=lambda member: (member[3], member[1]),
    )
    execute_values(cur, '''INSERT INTO public.account_snapshot_member
        (version_id,holding_id,holding_row_hash,source_item_id) VALUES %s''', snapshot_members)
    cur.execute('''INSERT INTO public.account_snapshot_version
        (version_id,account_code,as_of,coverage_status,cash_status,cash_amount,source_batch_id,source_revision,
         source_sha256,scope_attestation,calculation_version,reason)
        VALUES (%s,%s,%s,'complete','known',%s,%s,%s,%s,%s,'native-user-scope-v1','user confirmed all account members and cash')''',
        (str(identifier),account_code,as_of,cash[0]['market_value'],str(source_batch_id),source_revision,
         source_sha256,Json(attestation)))
    return str(identifier)


def verified_complete(cur, version_id):
    """Check an immutable complete scope against all current members and source."""
    from decimal import Decimal, InvalidOperation
    cur.execute("""SELECT v.*,b.status AS batch_status,b.account_code AS source_account,
            b.business_type,b.workflow,b.completeness,b.revision AS batch_revision,
            b.published_source_hash,b.scope_attestation AS current_attestation
        FROM account_snapshot_version v JOIN source_batches b ON b.batch_id=v.source_batch_id
        WHERE v.version_id=%s""", (str(version_id),))
    v = cur.fetchone()
    if not v or v['coverage_status']!='complete' or v['cash_status']!='known':
        return False
    a = v['scope_attestation']
    if isinstance(a,dict) and a.get('kind')=='ibkr_reviewed_complete_account':
        from domain.brokers.ibkr.ibkr_account_scope import verified_complete as verified_ibkr_complete
        return verified_ibkr_complete(cur,v)
    if (not isinstance(a,dict) or a != v['current_attestation'] or a.get('kind')!='user_confirmed_complete_account' or
        not a.get('note') or a.get('account_code')!=v['account_code'] or a.get('as_of')!=v['as_of'].isoformat() or
        a.get('revision')!=v['source_revision'] or v['batch_status']!='published' or
        v['source_account']!=v['account_code'] or v['business_type']!='holding' or v['workflow']!='account_import' or
        v['completeness']!='complete' or v['batch_revision']!=v['source_revision'] or v['published_source_hash']!=v['source_sha256']):
        return False
    cur.execute("""SELECT m.holding_id,m.holding_row_hash,m.source_item_id,h.account_code,h.as_of,h.symbol,h.name,
            h.quantity,h.cost,h.market_value,h.current_price,h.buy_price,h.major_category,h.minor_category,
            h.asset_liability,h.asset_id,h.currency,md5(to_jsonb(h)::text) AS current_hash,
            i.batch_id,i.review_status
        FROM account_snapshot_member m LEFT JOIN pams_holdings_snapshot h ON h.id=m.holding_id
        LEFT JOIN source_items i ON i.source_item_id=m.source_item_id WHERE m.version_id=%s ORDER BY m.holding_id""", (str(version_id),))
    rows = cur.fetchall()
    if not rows or len(rows)!=a.get('member_count'):
        return False
    cur.execute('SELECT id FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s', (v['account_code'],v['as_of']))
    if {r['id'] for r in cur.fetchall()} != {r['holding_id'] for r in rows}:
        return False
    if _unresolved_prior_holdings(cur,v['account_code'],v['as_of']):
        return False
    entries = a.get('members')
    if not isinstance(entries,list) or any(not isinstance(item,dict) for item in entries):
        return False
    members = {item.get('source_item_id'):item for item in entries}
    if len(members)!=len(rows) or set(members)!={str(row['source_item_id']) for row in rows}:
        return False
    cash = []
    try:
        for row in rows:
            statement = members[str(row['source_item_id'])]
            if (row['account_code']!=v['account_code'] or row['as_of']!=v['as_of'] or
                row['holding_row_hash']!=row['current_hash'] or row['batch_id']!=v['source_batch_id'] or
                row['review_status']!='confirmed' or
                any(row[field]!=statement.get(field) for field in ('symbol','name','asset_id','currency','major_category','minor_category','asset_liability')) or
                any(row[field] is None or Decimal(str(statement.get(field)))!=row[field] for field in ('quantity','cost','market_value','current_price','buy_price')) or
                not _complete_member_proven(cur,row['holding_id'],row['source_item_id'])):
                return False
            if row['major_category']=='现金':
                cash.append(row)
        return (len(cash)==1 and cash[0]['currency']=='CNY' and cash[0]['market_value']==v['cash_amount']
                and Decimal(str(a.get('cash_amount')))==v['cash_amount'])
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return False
