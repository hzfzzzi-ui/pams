-- Applied only in disposable rehearsal databases for this release; no runtime DDL.
CREATE TABLE IF NOT EXISTS public.pams_ibkr_source_link_repair_audit (
    request_sha256 text PRIMARY KEY CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    approval_sha256 text NOT NULL CHECK (approval_sha256 ~ '^[0-9a-f]{64}$'),
    purpose text NOT NULL CHECK (purpose = 'isolated_source_link_rehearsal'),
    database_scope jsonb NOT NULL CHECK (jsonb_typeof(database_scope) = 'object'),
    request_json jsonb NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
    approval_json jsonb NOT NULL CHECK (jsonb_typeof(approval_json) = 'object'),
    rows_affected integer NOT NULL CHECK (rows_affected > 0 AND rows_affected <= 500),
    executed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    database_operator text NOT NULL DEFAULT session_user,
    CHECK (request_json->>'request_sha256' = request_sha256),
    CHECK (approval_json->>'request_sha256' = request_sha256),
    CHECK (jsonb_array_length(request_json->'mappings') = rows_affected)
);

COMMENT ON TABLE public.pams_ibkr_source_link_repair_audit IS
    'Atomic applied-only isolated rehearsal journal. No production execution permission; no failed attempts recorded as applied.';
