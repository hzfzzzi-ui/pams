-- Source item contract for the redesigned source-record model.
-- Integrate into the bootstrap manifest only with all dependent writers/readers.
CREATE TABLE public.source_items (
    source_item_id UUID PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES public.source_records(source_id) ON DELETE RESTRICT,
    source_uid VARCHAR(180),
    source_position JSONB,
    parse_method VARCHAR(20) NOT NULL CHECK (parse_method IN ('template','ocr','api','manual')),
    raw_fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_fields) = 'object'),
    parsed_fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(parsed_fields) = 'object'),
    record_hash VARCHAR(64) NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    review_status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','confirmed','excluded')),
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_position IS NULL OR jsonb_typeof(source_position) = 'object'),
    CHECK (source_uid IS NULL OR length(btrim(source_uid)) > 0),
    UNIQUE (source_id, source_uid)
);
CREATE INDEX idx_source_items_source_review ON public.source_items(source_id, review_status);

-- Original evidence is immutable after insertion; reviewed fields remain editable.
CREATE FUNCTION public.redesigned_source_item_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_status TEXT;
BEGIN
    SELECT status INTO source_status FROM public.source_records WHERE source_id=OLD.source_id FOR SHARE;
    IF source_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'published source item is not editable' USING ERRCODE='23514';
    END IF;
    IF (NEW.source_item_id,NEW.source_id,NEW.source_uid,NEW.source_position,NEW.parse_method,NEW.raw_fields,NEW.record_hash,NEW.created_at)
       IS DISTINCT FROM
       (OLD.source_item_id,OLD.source_id,OLD.source_uid,OLD.source_position,OLD.parse_method,OLD.raw_fields,OLD.record_hash,OLD.created_at) THEN
        RAISE EXCEPTION 'original source evidence is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER redesigned_source_item_immutable BEFORE UPDATE ON public.source_items
    FOR EACH ROW EXECUTE FUNCTION public.redesigned_source_item_immutable();

-- Published evidence must not gain or lose rows, including excluded rows.
CREATE FUNCTION public.redesigned_source_item_membership_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_status TEXT;
BEGIN
    SELECT status INTO source_status FROM public.source_records
        WHERE source_id = CASE WHEN TG_OP = 'INSERT' THEN NEW.source_id ELSE OLD.source_id END
        FOR SHARE;
    IF source_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'final source evidence cannot be changed' USING ERRCODE='23514';
    END IF;
    IF TG_OP = 'INSERT' THEN RETURN NEW; ELSE RETURN OLD; END IF;
END $$;
CREATE TRIGGER redesigned_source_item_membership_guard BEFORE INSERT OR DELETE ON public.source_items
    FOR EACH ROW EXECUTE FUNCTION public.redesigned_source_item_membership_guard();
