-- Additive broker archives. Existing source_records.content is retained for historical sources.
CREATE TABLE public.tonghuashun_source_originals (
    sha256 varchar(64) PRIMARY KEY,
    content bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 10485760),
    CHECK (sha256 = encode(pg_catalog.sha256(content), 'hex'))
);
CREATE TABLE public.ibkr_source_originals (
    sha256 varchar(64) PRIMARY KEY,
    content bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 33554432),
    CHECK (sha256 = encode(pg_catalog.sha256(content), 'hex'))
);

CREATE FUNCTION public.broker_source_original_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'broker source original is immutable' USING ERRCODE='23514';
END $$;
CREATE TRIGGER ths_source_original_immutable BEFORE UPDATE OR DELETE ON public.tonghuashun_source_originals
    FOR EACH ROW EXECUTE FUNCTION public.broker_source_original_immutable();
CREATE TRIGGER ibkr_source_original_immutable BEFORE UPDATE OR DELETE ON public.ibkr_source_originals
    FOR EACH ROW EXECUTE FUNCTION public.broker_source_original_immutable();

-- Replace only the original content/digest and file-presence constraints.
ALTER TABLE public.source_records
    DROP CONSTRAINT source_records_check2,
    DROP CONSTRAINT source_records_check3,
    ADD CONSTRAINT source_record_original_location CHECK (
        (content IS NULL AND sha256 IS NULL) OR
        (sha256 IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND
            (content IS NOT NULL OR account_code IN ('tonghuashun', 'ibkr')))
    ),
    ADD CONSTRAINT source_record_file_original CHECK (
        acquisition_method NOT IN ('file','screenshot') OR
        (sha256 IS NOT NULL AND filename IS NOT NULL AND file_format IS NOT NULL)
    );

CREATE FUNCTION public.source_original_content(account text, digest text, inline_content bytea)
RETURNS bytea LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN inline_content IS NOT NULL OR digest IS NULL THEN inline_content
        WHEN account = 'tonghuashun' THEN (SELECT content FROM public.tonghuashun_source_originals WHERE sha256 = digest)
        WHEN account = 'ibkr' THEN (SELECT content FROM public.ibkr_source_originals WHERE sha256 = digest)
    END
$$;

CREATE FUNCTION public.source_record_archive_required() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sha256 IS NOT NULL AND NEW.content IS NULL
       AND public.source_original_content(NEW.account_code, NEW.sha256, NULL) IS NULL THEN
        RAISE EXCEPTION 'broker source archive original is missing' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER source_record_archive_required AFTER INSERT ON public.source_records
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.source_record_archive_required();
