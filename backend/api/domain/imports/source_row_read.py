"""Unified source/fact projection for account and synchronization readers."""

_UNIFIED_ROWS = """
    SELECT CASE WHEN i.raw_fields->'source'->'statement' = 'true'::jsonb
                THEN 'bank_statement_row' ELSE 'unified_source' END::text AS source_system,
           f.source_table::text AS source_table, f.source_id::text AS source_id,
           i.source_uid::text AS source_uid, f.account_code::text AS account_code,
           f.event_date AS event_date, i.created_at AS imported_at, i.created_at,
           'unified_source_item'::text AS source_projection_origin,
           CASE WHEN f.cashflow_id = ANY(__VERIFIED_FACTS__) THEN 'verified' ELSE 'unknown' END::text AS source_evidence_status,
           (i.raw_fields->'source'->'statement' = 'true'::jsonb) IS TRUE AS is_bank_statement
    FROM pams_cashflow_fact f
    JOIN source_items i ON i.source_item_id=f.source_item_id
    JOIN source_batches b ON b.batch_id=i.batch_id
    WHERE i.review_status <> 'excluded'
      AND lower(coalesce(f.review_status,'')) IN ('auto','confirmed','reviewed')
"""

_CONFIRMED_BANK_ROWS = """
    UNION ALL
    SELECT 'bank_statement_row'::text AS source_system, f.source_table::text, f.source_id::text,
           i.source_uid::text, f.account_code::text, f.event_date,
           i.created_at AS imported_at, i.created_at, 'confirmed_bank_source'::text,
           CASE WHEN c.confirmation_id = ANY(__VERIFIED_CONFIRMATIONS__) THEN 'verified' ELSE 'unknown' END::text, true AS is_bank_statement
    FROM evidence_confirmation c
    JOIN pams_cashflow_fact f ON f.cashflow_id=c.cashflow_id
    JOIN source_items i ON i.source_item_id=c.source_item_id
    JOIN source_batches b ON b.batch_id=i.batch_id
    WHERE c.confirmation_type='bank_source' AND c.revoked_at IS NULL
      AND i.review_status <> 'excluded'
      AND i.raw_fields->'source'->'statement'='true'::jsonb
      AND f.raw_json->>'evidence_role'='derived_bank_leg'
      AND f.raw_json->'independent_bank_evidence'='false'::jsonb
      AND c.evidence->>'cashflow_id'=f.cashflow_id::text
      AND c.evidence->>'source_item_id'=i.source_item_id::text
      AND c.evidence->>'original_source_item_id'=f.source_item_id::text
      AND c.evidence->>'source_table'=f.source_table AND c.evidence->>'source_id'=f.source_id
      AND c.evidence->>'account_code'=f.account_code
      AND c.evidence->>'counter_account_code' IS NOT DISTINCT FROM f.counter_account_code
      AND c.evidence->>'amount'=f.amount::text AND c.evidence->>'currency'=f.currency
      AND c.evidence->>'event_date'=f.event_date::text AND c.evidence->>'record_hash'=i.record_hash::text
      AND c.evidence->'parsed_fields'=i.parsed_fields AND c.evidence->>'fact_review'=f.review_status
"""


def source_row_relation(cur, *, verifier=None) -> str:
    """Expose published unified source items, never recreate old source rows."""
    cur.execute("""SELECT to_regclass('public.source_items') AS source_items,
                           to_regclass('public.source_batches') AS source_batches,
                           to_regclass('public.evidence_confirmation') AS bank_confirmations,
                           EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.evidence_confirmation')
                               AND attname='revoked_at' AND NOT attisdropped) AS bank_revocations,
                           to_regclass('public.pams_cashflow_fact') AS cashflow_facts,
                           EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.pams_cashflow_fact')
                               AND attname='source_item_id' AND NOT attisdropped) AS cashflow_source_link,
                           to_regclass('public.source_files') AS source_files,
                           NOT EXISTS (SELECT 1 FROM unnest(ARRAY['source_item_id','batch_id','file_id','source_uid',
                               'parse_method','raw_fields','parsed_fields','record_hash','review_status','created_at',
                               'source_order','origin_file_id','external_id','location','review_note']) AS required(name)
                               WHERE NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=to_regclass('public.source_items')
                                   AND a.attname=required.name AND NOT a.attisdropped)) AS source_items_columns,
                           NOT EXISTS (SELECT 1 FROM unnest(ARRAY['batch_id','account_code','business_type','workflow',
                               'institution','acquisition_method','date_from','date_to','completeness','scope_attestation',
                               'status','revision','published_result','published_source_hash']) AS required(name)
                               WHERE NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=to_regclass('public.source_batches')
                                   AND a.attname=required.name AND NOT a.attisdropped)) AS source_batches_columns,
                           NOT EXISTS (SELECT 1 FROM unnest(ARRAY['file_id','batch_id','filename','file_format',
                               'content_type','sha256','content','current_metadata','review_fingerprint']) AS required(name)
                               WHERE NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=to_regclass('public.source_files')
                                   AND a.attname=required.name AND NOT a.attisdropped)) AS source_files_columns""")
    row = cur.fetchone()
    if isinstance(row, dict):
        ready = all(row.get(key) for key in ('source_items','source_batches','cashflow_facts','cashflow_source_link',
                                              'source_files','source_items_columns','source_batches_columns','source_files_columns'))
        bank_ready = row.get('bank_confirmations') and row.get('bank_revocations')
    else:
        ready = bool(row and len(row) >= 10 and all(row[i] for i in (0,1,4,5,6,7,8,9)))
        bank_ready = bool(row and len(row) >= 4 and row[2] and row[3])
    if not ready:
        # A marker preserves unknown coverage without inventing source records
        # or making independent account information unavailable.
        return """(SELECT NULL::text AS source_system,NULL::text AS source_table,
            NULL::text AS source_id,NULL::text AS source_uid,NULL::text AS account_code,
            NULL::date AS event_date,NULL::timestamptz AS imported_at,NULL::timestamptz AS created_at,
            'schema_unavailable'::text AS source_projection_origin,'unknown'::text AS source_evidence_status,
            TRUE AS is_bank_statement)"""
    cur.execute("""SELECT f.cashflow_id FROM pams_cashflow_fact f JOIN source_items i ON i.source_item_id=f.source_item_id
                   JOIN source_batches b ON b.batch_id=i.batch_id WHERE i.review_status <> 'excluded'""")
    ids = [int(r['cashflow_id'] if isinstance(r,dict) else r[0]) for r in cur.fetchall()]
    from domain.imports.source_fact_evidence import NativeFactVerifier
    verifier = verifier or NativeFactVerifier(cur)
    proofs = (verifier.verify_many(table='pams_cashflow_fact',fact_ids=ids) if hasattr(verifier,'verify_many')
              else {ident:verifier.verify(table='pams_cashflow_fact',fact_id=ident) for ident in ids})
    verified = [ident for ident in ids if proofs[ident].get('status')=='verified']
    id_array = 'ARRAY[' + ','.join(str(ident) for ident in verified) + ']::bigint[]'
    direct = _UNIFIED_ROWS.replace('__VERIFIED_FACTS__', id_array)
    if not bank_ready:
        return '(' + direct + ')'
    cur.execute("SELECT confirmation_id FROM evidence_confirmation WHERE confirmation_type='bank_source'")
    ids = [r['confirmation_id'] if isinstance(r,dict) else r[0] for r in cur.fetchall()]
    from uuid import UUID
    from domain.ledger.bank_source_confirmation import verified as verified_bank
    confirmed = [str(UUID(str(ident))) for ident in ids if verified_bank(cur,ident)]
    confirmations = 'ARRAY[' + ','.join("'" + ident + "'" for ident in confirmed) + ']::uuid[]'
    return '(' + direct + _CONFIRMED_BANK_ROWS.replace('__VERIFIED_CONFIRMATIONS__',confirmations) + ')'
