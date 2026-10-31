-- Candidate unified source layer. Not part of the production or empty-database
-- manifest: apply only to a disposable database for isolated development.
-- Existing import flows and fact tables remain authoritative until migrated.

CREATE TABLE public.source_batches (
    batch_id UUID PRIMARY KEY,
    account_code VARCHAR(50) NOT NULL REFERENCES public.pams_accounts(account_code),
    business_type TEXT NOT NULL CHECK (business_type IN ('holding', 'trade', 'cash', 'balance')),
    institution TEXT NOT NULL CHECK (length(trim(institution)) > 0),
    acquisition_method TEXT NOT NULL CHECK (acquisition_method IN ('api', 'upload', 'manual')),
    date_from DATE,
    date_to DATE,
    completeness TEXT NOT NULL DEFAULT 'unknown' CHECK (completeness IN ('complete', 'partial', 'unknown')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'review', 'published', 'voided')),
    published_source_hash CHAR(64) CHECK (published_source_hash IS NULL OR published_source_hash ~ '^[0-9a-f]{64}$'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (date_from IS NULL OR date_to IS NULL OR date_from <= date_to),
    CHECK (status <> 'published' OR published_source_hash IS NOT NULL)
);
CREATE INDEX idx_source_batches_account ON public.source_batches (account_code, created_at DESC);

CREATE TABLE public.source_files (
    file_id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES public.source_batches(batch_id),
    filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
    file_format TEXT NOT NULL CHECK (file_format IN ('csv', 'tsv', 'xlsx', 'xls', 'pdf', 'png', 'jpeg')),
    content_type TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content BYTEA NOT NULL CHECK (octet_length(content) <= 10485760),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, sha256),
    UNIQUE (batch_id, file_id)
);

CREATE TABLE public.source_items (
    source_item_id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES public.source_batches(batch_id),
    file_id UUID,
    source_uid TEXT NOT NULL CHECK (length(trim(source_uid)) > 0),
    external_id TEXT,
    location TEXT,
    parse_method TEXT NOT NULL CHECK (parse_method IN ('template', 'ocr', 'manual', 'api')),
    raw_fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_fields) = 'object'),
    parsed_fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(parsed_fields) = 'object'),
    record_hash CHAR(64) NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'confirmed', 'excluded')),
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, source_uid),
    FOREIGN KEY (batch_id, file_id) REFERENCES public.source_files(batch_id, file_id),
    CHECK ((parse_method IN ('manual', 'api')) OR file_id IS NOT NULL)
);
CREATE INDEX idx_source_items_batch_review ON public.source_items (batch_id, review_status);

-- Source evidence is immutable; only parsed candidates and review decisions
-- can be edited. Replacing evidence requires a new source record or file.
CREATE FUNCTION public.source_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'source_items' THEN
        IF (NEW.source_item_id, NEW.batch_id, NEW.file_id, NEW.source_uid, NEW.external_id, NEW.location,
            NEW.parse_method, NEW.raw_fields, NEW.record_hash, NEW.created_at)
           IS DISTINCT FROM
           (OLD.source_item_id, OLD.batch_id, OLD.file_id, OLD.source_uid, OLD.external_id, OLD.location,
            OLD.parse_method, OLD.raw_fields, OLD.record_hash, OLD.created_at) THEN
            RAISE EXCEPTION 'source item evidence is immutable' USING ERRCODE = '23514';
        END IF;
        UPDATE public.source_batches SET revision = revision + 1, updated_at = now()
        WHERE batch_id = OLD.batch_id AND status IN ('draft', 'review');
        IF NOT FOUND THEN
            RAISE EXCEPTION 'source batch is not editable' USING ERRCODE = '23514';
        END IF;
    ELSIF (NEW.file_id, NEW.batch_id, NEW.filename, NEW.file_format, NEW.content_type, NEW.sha256, NEW.content, NEW.created_at)
          IS DISTINCT FROM
          (OLD.file_id, OLD.batch_id, OLD.filename, OLD.file_format, OLD.content_type, OLD.sha256, OLD.content, OLD.created_at) THEN
        RAISE EXCEPTION 'source file evidence is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER source_items_evidence_immutable BEFORE UPDATE ON public.source_items
    FOR EACH ROW EXECUTE FUNCTION public.source_evidence_immutable();
CREATE TRIGGER source_files_evidence_immutable BEFORE UPDATE ON public.source_files
    FOR EACH ROW EXECUTE FUNCTION public.source_evidence_immutable();

-- Publication freezes the batch and its evidence at the database boundary.
-- Locking the parent also serializes child insertion with publication.
CREATE FUNCTION public.source_batch_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'published' AND (TG_OP = 'DELETE' OR NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'published source batch is immutable' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER source_batch_published_immutable BEFORE UPDATE OR DELETE ON public.source_batches
    FOR EACH ROW EXECUTE FUNCTION public.source_batch_freeze();

CREATE FUNCTION public.source_child_write_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_status TEXT;
BEGIN
    UPDATE public.source_batches SET revision = revision + 1, updated_at = now()
    WHERE batch_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.batch_id ELSE NEW.batch_id END
      AND status IN ('draft', 'review')
    RETURNING status INTO parent_status;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source batch is not editable' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER source_files_insert_delete_guard BEFORE INSERT OR DELETE ON public.source_files
    FOR EACH ROW EXECUTE FUNCTION public.source_child_write_guard();
CREATE TRIGGER source_items_insert_delete_guard BEFORE INSERT OR DELETE ON public.source_items
    FOR EACH ROW EXECUTE FUNCTION public.source_child_write_guard();
