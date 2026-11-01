"""Read only independently published bank statement facts in the new schema."""
from decimal import Decimal, InvalidOperation


def rows(cur, *, since=None, limit=1000):
    cur.execute('''SELECT f.cashflow_id,f.account_code,f.event_date,f.posting_date,f.amount,f.currency,
          f.counter_account_code,f.cashflow_type,f.category,f.subcategory,f.summary,f.description,f.counterparty,
          f.source_item_id,b.batch_id,b.institution,b.created_at AS imported_at,
          i.raw_fields->'source' AS source,sf.filename,sf.sha256
        FROM pams_cashflow_fact f
        JOIN source_items i ON i.source_item_id=f.source_item_id AND i.review_status <> 'excluded'
        JOIN source_batches b ON b.batch_id=i.batch_id
        LEFT JOIN source_files sf ON sf.file_id=i.file_id AND sf.batch_id=i.batch_id
        WHERE i.raw_fields->'source'->'statement'='true'::jsonb
          AND lower(coalesce(f.review_status,'')) IN ('auto','confirmed','reviewed')
          AND (%s::date IS NULL OR f.event_date >= %s::date)
        ORDER BY f.event_date DESC,f.cashflow_id DESC LIMIT %s''', (since,since,limit))
    result = [dict(r) for r in cur.fetchall()]
    from domain.imports.source_fact_evidence import NativeFactVerifier
    verifier = NativeFactVerifier(cur)
    for row in result:
        proof = verifier.verify(table='pams_cashflow_fact', fact_id=row['cashflow_id'])
        row['source_evidence_status'] = proof.get('status', 'unknown')
        row['source_evidence_reason'] = proof.get('reason')
    cur.execute("""SELECT f.cashflow_id,f.account_code,f.event_date,f.posting_date,f.amount,f.currency,
          f.counter_account_code,f.cashflow_type,f.category,f.subcategory,f.summary,f.description,f.counterparty,
          i.source_item_id,b.batch_id,b.institution,b.created_at AS imported_at,
          i.raw_fields->'source' AS source,sf.filename,sf.sha256,c.confirmation_id
        FROM evidence_confirmation c
        JOIN pams_cashflow_fact f ON f.cashflow_id=c.cashflow_id
        JOIN source_items i ON i.source_item_id=c.source_item_id
        JOIN source_batches b ON b.batch_id=i.batch_id
        LEFT JOIN source_files sf ON sf.file_id=i.file_id AND sf.batch_id=i.batch_id
        WHERE c.confirmation_type='bank_source' AND c.revoked_at IS NULL AND (%s::date IS NULL OR f.event_date >= %s::date)
        ORDER BY f.event_date DESC,f.cashflow_id DESC LIMIT %s""", (since,since,limit))
    indirect = [dict(r) for r in cur.fetchall()]
    from domain.ledger.bank_source_confirmation import verified
    for row in indirect:
        row['source_evidence_status'] = 'verified' if verified(cur, row['confirmation_id']) else 'unknown'
        row['source_evidence_reason'] = None if row['source_evidence_status'] == 'verified' else 'bank_source_unavailable'
        result.append(row)
    result.sort(key=lambda row: (row['event_date'],row['cashflow_id']), reverse=True)
    return result[:limit]


def imports(cur):
    entries = rows(cur, limit=10000)
    batches, summaries = {}, {}
    for row in entries:
        source = row['source'] if isinstance(row['source'], dict) else {}
        original = source.get('statement_row') if isinstance(source.get('statement_row'), dict) else {}
        batch_id = str(row['batch_id'])
        if batch_id not in batches:
            batches[batch_id] = {'id':batch_id, 'profile_code':original.get('profile_code'),
                'bank':row['institution'], 'account_code':row['account_code'], 'account_kind':original.get('account_kind'),
                'source_file':row['filename'], 'source_sha256':row['sha256'], 'statement_start':None,
                'statement_end':None, 'statement_balance':None, 'row_count':0, 'imported_at':row['imported_at'],
                'coverage_status':'unknown'}
        batches[batch_id]['row_count'] += 1
        key = (row['account_code'], original.get('profile_code'), row['institution'], original.get('account_kind'), row['currency'])
        if key not in summaries:
            summaries[key] = {'profile_code':key[1], 'account_code':key[0], 'bank':key[2], 'account_kind':key[3], 'currency':key[4],
                'row_count':0, 'inflow':Decimal(0), 'outflow':Decimal(0), 'net_flow':Decimal(0),
                'latest_transaction_date':row['event_date'], 'latest_balance':None, 'latest_balance_date':None,
                'balance_source':None, 'net_asset_balance':None, 'coverage_status':'unknown'}
        item = summaries[key]
        amount = row['amount']
        item['row_count'] += 1
        if row.get('source_evidence_status') != 'verified':
            item['source_evidence_status'] = 'unknown'
            item['inflow'] = item['outflow'] = item['net_flow'] = None
            item['latest_balance'] = item['latest_balance_date'] = item['balance_source'] = None
            item['net_asset_balance'] = None
            item['balance_status'] = 'unknown'
            continue
        if item.get('source_evidence_status') == 'unknown':
            continue
        item['source_evidence_status'] = 'verified'
        item['inflow'] += max(amount,Decimal(0))
        item['outflow'] += max(-amount,Decimal(0))
        item['net_flow'] += amount
        raw_balance = original.get('balance')
        if raw_balance not in (None, ''):
            try:
                balance = Decimal(str(raw_balance))
            except (InvalidOperation, ValueError):
                balance = None
            if balance is not None and balance.is_finite():
                if item['latest_balance_date'] is None or row['event_date'] > item['latest_balance_date']:
                    item['latest_balance'], item['latest_balance_date'] = balance, row['event_date']
                    item['balance_source'], item['balance_status'] = 'statement_row', 'recorded'
                    item['net_asset_balance'] = -abs(balance) if key[3] == 'credit_card' else None
                elif row['event_date'] == item['latest_balance_date'] and item.get('latest_balance') != balance:
                    item['latest_balance'], item['balance_source'], item['net_asset_balance'] = None, None, None
                    item['balance_status'] = 'ambiguous'
    return list(batches.values()), list(summaries.values())


def unmatched(cur, *, since, limit):
    from domain.ledger import confirmed_transfers
    recorded = confirmed_transfers.read(cur, limit=10000)
    linked = {leg['id'] for match in recorded if match['evidence_status'] == 'confirmed'
              for leg in (match['from_evidence'], match['to_evidence']) if leg['kind'] == 'cashflow'}
    result = []
    for row in rows(cur, since=since, limit=limit):
        if row['cashflow_type'] not in {'internal_transfer','investment_funding','investment_withdrawal'} or row['cashflow_id'] in linked:
            continue
        source = row['source'] if isinstance(row['source'],dict) else {}
        original = source.get('statement_row') if isinstance(source.get('statement_row'),dict) else {}
        result.append({'issue_type':'unmatched_row', 'row_id':row['cashflow_id'],
            'account_code':row['account_code'], 'transaction_date':row['event_date'], 'amount':row['amount'],
            'currency':row['currency'], 'category':row['category'], 'subcategory':row['subcategory'],
            'summary':row['summary'], 'description':row['description'], 'counterparty':row['counterparty'],
            'profile_code':original.get('profile_code'), 'match_type':None, 'relation_code':None,
            'from_account_code':None, 'to_account_code':None, 'confidence':None,
            'original_amount':original.get('original_amount'), 'original_currency':original.get('original_currency'),
            'source_evidence_status':row.get('source_evidence_status', 'unknown'),
            'issue_note':('已核对本侧流水；对侧来源尚未人工确认' if row.get('source_evidence_status') == 'verified'
                          else '本侧来源证据缺失或不一致，待核对')})
    return result
