-- Unified source record: one row per uploaded file, screenshot, API response,
-- or manual entry. This schema is not an existing-database migration.
CREATE TABLE public.source_templates (
    account_code VARCHAR(80) NOT NULL REFERENCES public.pams_accounts(account_code),
    document_type VARCHAR(30) NOT NULL CHECK (document_type IN ('cash','trade','holding')),
    mapping JSONB NOT NULL CHECK (jsonb_typeof(mapping) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_code, document_type)
);
CREATE TABLE public.source_records (
    source_id UUID PRIMARY KEY,
    import_group_id UUID,
    account_code VARCHAR(80) NOT NULL REFERENCES public.pams_accounts(account_code),
    source_name VARCHAR(80) NOT NULL CHECK (length(btrim(source_name)) > 0),
    acquisition_method VARCHAR(20) NOT NULL CHECK (acquisition_method IN ('file','screenshot','api','manual')),
    document_type VARCHAR(30) NOT NULL CHECK (document_type IN ('cash','trade','holding','statement')),
    filename VARCHAR(180),
    file_format VARCHAR(20),
    content_type VARCHAR(100),
    content BYTEA,
    sha256 VARCHAR(64),
    period_start DATE,
    period_end DATE,
    scope_note TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','voided')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (period_start IS NULL OR period_end IS NULL OR period_start <= period_end),
    CHECK (content IS NULL OR octet_length(content) <= CASE WHEN file_format='xml' THEN 33554432 ELSE 10485760 END),
    CHECK ((content IS NULL AND sha256 IS NULL) OR
           (content IS NOT NULL AND sha256 IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$')),
    CHECK (acquisition_method NOT IN ('file','screenshot') OR
           (content IS NOT NULL AND filename IS NOT NULL AND file_format IS NOT NULL))
);
CREATE INDEX idx_source_records_account ON public.source_records (account_code, created_at DESC);
CREATE INDEX idx_source_records_group ON public.source_records (import_group_id) WHERE import_group_id IS NOT NULL;

CREATE FUNCTION public.redesigned_source_record_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'draft' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'final source is not editable' USING ERRCODE='23514';
    END IF;
    IF (NEW.source_id,NEW.account_code,NEW.source_name,NEW.acquisition_method,NEW.document_type,
        NEW.filename,NEW.file_format,NEW.content_type,NEW.content,NEW.sha256,NEW.created_at)
       IS DISTINCT FROM
       (OLD.source_id,OLD.account_code,OLD.source_name,OLD.acquisition_method,OLD.document_type,
        OLD.filename,OLD.file_format,OLD.content_type,OLD.content,OLD.sha256,OLD.created_at) THEN
        RAISE EXCEPTION 'original source record is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER redesigned_source_record_immutable BEFORE UPDATE ON public.source_records
    FOR EACH ROW EXECUTE FUNCTION public.redesigned_source_record_immutable();
