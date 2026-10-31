-- Isolated candidate; not included in empty-database bootstrap or any runtime.
-- A result version is immutable. Sources are recorded references, not foreign
-- keys: their mutable current rows cannot by themselves prove a historical input.
CREATE TABLE IF NOT EXISTS public.pams_valuation_result_version (
    version_key text PRIMARY KEY CHECK (version_key ~ '^[0-9a-f]{64}$'),
    result_kind text NOT NULL CHECK (result_kind IN ('holding', 'portfolio')),
    scope_key text NOT NULL CHECK (scope_key <> ''),
    account_code text,
    business_date date NOT NULL,
    base_currency text NOT NULL CHECK (base_currency <> ''),
    calculation_version text NOT NULL CHECK (calculation_version <> ''),
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (result_kind <> 'holding' OR (account_code IS NOT NULL AND account_code <> ''))
);
CREATE INDEX IF NOT EXISTS idx_pams_valuation_result_current
    ON public.pams_valuation_result_version (account_code, scope_key, business_date, result_hash)
    WHERE result_kind = 'holding';

CREATE TABLE IF NOT EXISTS public.pams_valuation_input_evidence (
    version_key text NOT NULL REFERENCES public.pams_valuation_result_version(version_key)
        DEFERRABLE INITIALLY DEFERRED,
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    input_kind text NOT NULL CHECK (input_kind IN ('holding', 'trade', 'quote', 'fx', 'source_item')),
    evidence jsonb NOT NULL,
    PRIMARY KEY (version_key, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_pams_valuation_input_source_item
    ON public.pams_valuation_input_evidence ((evidence->'source_ref'->'key'->>'source_item_id'))
    WHERE input_kind = 'source_item';

CREATE OR REPLACE FUNCTION public.pams_valuation_evidence_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'valuation evidence versions are immutable';
END;
$$;

CREATE OR REPLACE FUNCTION public.pams_valuation_evidence_before_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.pams_valuation_result_version WHERE version_key = NEW.version_key) THEN
        RAISE EXCEPTION 'cannot add evidence to an existing valuation version';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.pams_valuation_result_version'::regclass AND tgname = 'pams_valuation_result_immutable') THEN
        CREATE TRIGGER pams_valuation_result_immutable BEFORE UPDATE OR DELETE ON public.pams_valuation_result_version
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.pams_valuation_input_evidence'::regclass AND tgname = 'pams_valuation_input_immutable') THEN
        CREATE TRIGGER pams_valuation_input_immutable BEFORE UPDATE OR DELETE ON public.pams_valuation_input_evidence
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.pams_valuation_input_evidence'::regclass AND tgname = 'pams_valuation_input_no_append') THEN
        CREATE TRIGGER pams_valuation_input_no_append BEFORE INSERT ON public.pams_valuation_input_evidence
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_before_insert();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.pams_valuation_result_version'::regclass AND tgname = 'pams_valuation_result_no_truncate') THEN
        CREATE TRIGGER pams_valuation_result_no_truncate BEFORE TRUNCATE ON public.pams_valuation_result_version
            FOR EACH STATEMENT EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.pams_valuation_input_evidence'::regclass AND tgname = 'pams_valuation_input_no_truncate') THEN
        CREATE TRIGGER pams_valuation_input_no_truncate BEFORE TRUNCATE ON public.pams_valuation_input_evidence
            FOR EACH STATEMENT EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
END;
$$;
