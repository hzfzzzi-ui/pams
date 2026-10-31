"""Isolated bridge from verified unified source items to legacy source reads."""

from domain.imports.isolation import source_enabled


_UNIFIED_ROWS = """
    SELECT CASE WHEN i.raw_fields->'legacy_source_row' ? 'source_system'
                      AND i.raw_fields->'legacy_source_row' @> jsonb_build_object(
                          'source_table',f.source_table,'source_id',f.source_id,
                          'source_uid',i.source_uid,'account_code',f.account_code,'event_date',f.event_date)
                THEN i.raw_fields->'legacy_source_row'->>'source_system'
                WHEN f.source_table='pams_bank_statement_rows' THEN 'bank_statement_row'
                ELSE 'unified_source' END::text AS source_system,
           f.source_table::text AS source_table, f.source_id::text AS source_id,
           i.source_uid::text AS source_uid, f.account_code::text AS account_code,
           f.event_date AS event_date,
           CASE
             WHEN i.raw_fields ? 'legacy_source_row' THEN
               CASE WHEN i.raw_fields->'legacy_source_row' @> jsonb_build_object(
                           'source_table',f.source_table,'source_id',f.source_id,
                           'source_uid',i.source_uid,'account_code',f.account_code,'event_date',f.event_date)
                          AND i.raw_fields->'legacy_source_row'->>'imported_at' ~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
                          AND pg_input_is_valid(i.raw_fields->'legacy_source_row'->>'imported_at','timestamp with time zone')
                    THEN (i.raw_fields->'legacy_source_row'->>'imported_at')::timestamptz ELSE NULL END
             WHEN i.raw_fields ? 'id' AND f.source_table='pams_bank_statement_rows' THEN NULL
             ELSE i.created_at END AS imported_at,
           CASE
             WHEN i.raw_fields ? 'legacy_source_row' THEN
               CASE WHEN i.raw_fields->'legacy_source_row' @> jsonb_build_object(
                           'source_table',f.source_table,'source_id',f.source_id,
                           'source_uid',i.source_uid,'account_code',f.account_code,'event_date',f.event_date)
                          AND i.raw_fields->'legacy_source_row'->>'created_at' ~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
                          AND pg_input_is_valid(i.raw_fields->'legacy_source_row'->>'created_at','timestamp with time zone')
                    THEN (i.raw_fields->'legacy_source_row'->>'created_at')::timestamptz ELSE NULL END
             WHEN i.raw_fields ? 'id' AND f.source_table='pams_bank_statement_rows' THEN NULL
             ELSE i.created_at END AS created_at,
           'unified_source_item'::text AS source_projection_origin
    FROM pams_cashflow_fact f
    JOIN source_items i ON i.source_item_id=f.source_item_id
    JOIN source_batches b ON b.batch_id=i.batch_id
    WHERE b.status='published' AND i.review_status='confirmed' AND b.account_code=f.account_code
"""


def source_row_relation(cur) -> str:
    """Read published source/fact links and conditionally bridge actual old rows.

    The bridge requires a published/confirmed bank item, linked cashflow fact,
    matching source identity and a complete unchanged legacy projection. New
    databases do not need the legacy table. Missing historical timestamps stay
    null rather than being replaced with a later migration timestamp.
    """
    if not source_enabled(cur):
        return "pams_source_row"
    cur.execute("SELECT to_regclass('public.pams_source_row') AS table_name")
    row = cur.fetchone()
    if not (row.get('table_name') if isinstance(row, dict) else row[0] if row else None):
        return "(" + _UNIFIED_ROWS + ")"
    return """(
        WITH matches AS (
            SELECT s.source_table, s.source_id, i.raw_fields->'legacy_source_row' AS old,
                   count(*) OVER (PARTITION BY s.source_table,s.source_id) AS matches
            FROM pams_source_row s
            JOIN pams_cashflow_fact f ON f.source_table=s.source_table AND f.source_id=s.source_id
            JOIN source_items i ON i.source_item_id=f.source_item_id
            JOIN source_batches b ON b.batch_id=i.batch_id
            WHERE s.source_table='pams_bank_statement_rows'
              AND b.business_type='cash' AND b.status='published' AND i.review_status='confirmed'
              AND b.account_code=s.account_code AND i.source_uid=s.source_uid
              AND i.raw_fields->>'id'=s.source_id
              AND i.raw_fields->'legacy_source_row'=jsonb_build_object(
                    'source_system',s.source_system,'source_table',s.source_table,'source_id',s.source_id,
                    'source_uid',s.source_uid,'account_code',s.account_code,'event_date',s.event_date,
                    'imported_at',s.imported_at,'created_at',s.created_at)
        ), valid AS (SELECT * FROM matches WHERE matches=1)
        SELECT old->>'source_system' AS source_system, old->>'source_table' AS source_table,
               old->>'source_id' AS source_id, old->>'source_uid' AS source_uid,
               old->>'account_code' AS account_code, (old->>'event_date')::date AS event_date,
               (old->>'imported_at')::timestamptz AS imported_at,
               (old->>'created_at')::timestamptz AS created_at,
               'unified_source_item'::text AS source_projection_origin
        FROM valid
        UNION ALL
        SELECT s.source_system,s.source_table,s.source_id,s.source_uid,s.account_code,
               s.event_date,s.imported_at,s.created_at,'legacy_source_row'::text AS source_projection_origin
        FROM pams_source_row s
        WHERE NOT EXISTS (SELECT 1 FROM valid v WHERE v.source_table=s.source_table AND v.source_id=s.source_id)
        UNION ALL
        SELECT u.* FROM (""" + _UNIFIED_ROWS + """) AS u
        WHERE NOT EXISTS (SELECT 1 FROM pams_source_row s WHERE s.source_table=u.source_table AND s.source_id=u.source_id)
    )"""
