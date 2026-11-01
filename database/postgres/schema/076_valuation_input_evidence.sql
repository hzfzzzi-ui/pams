-- A valuation version and its complete input evidence are stored atomically.
-- Source snapshots are recorded evidence, not mutable foreign-key projections.
CREATE TABLE IF NOT EXISTS public.valuation_evidence (
    version_key text PRIMARY KEY CHECK (version_key ~ '^[0-9a-f]{64}$'),
    result_kind text NOT NULL CHECK (result_kind IN ('holding', 'portfolio')),
    scope_key text NOT NULL CHECK (scope_key <> ''),
    account_code text,
    business_date date NOT NULL,
    base_currency text NOT NULL CHECK (base_currency <> ''),
    calculation_version text NOT NULL CHECK (calculation_version <> ''),
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    inputs jsonb NOT NULL CHECK (jsonb_typeof(inputs) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (result_kind <> 'holding' OR (account_code IS NOT NULL AND account_code <> ''))
);
CREATE INDEX IF NOT EXISTS idx_pams_valuation_result_current
    ON public.valuation_evidence (account_code, scope_key, business_date, result_hash)
    WHERE result_kind = 'holding';

CREATE INDEX IF NOT EXISTS idx_valuation_evidence_inputs
    ON public.valuation_evidence USING gin (inputs jsonb_path_ops);

CREATE OR REPLACE FUNCTION public.pams_valuation_evidence_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'valuation evidence versions are immutable';
END;
$$;

CREATE OR REPLACE FUNCTION public.pams_valuation_evidence_validate_inputs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(NEW.inputs) AS value
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
          OR NOT (value ?& ARRAY['input_hash','input_kind','evidence'])
          OR value->>'input_hash' IS NULL
          OR value->>'input_hash' !~ '^[0-9a-f]{64}$'
          OR value->>'input_kind' IS NULL
          OR value->>'input_kind' NOT IN ('holding','trade','quote','fx','source_item')
          OR jsonb_typeof(value->'evidence') IS DISTINCT FROM 'object'
          OR value->'evidence'->>'kind' IS DISTINCT FROM value->>'input_kind'
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(NEW.inputs) AS value
        GROUP BY value->>'input_hash' HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'invalid or duplicate valuation input evidence';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.valuation_evidence'::regclass AND tgname = 'pams_valuation_evidence_immutable') THEN
        CREATE TRIGGER pams_valuation_evidence_immutable BEFORE UPDATE OR DELETE ON public.valuation_evidence
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.valuation_evidence'::regclass AND tgname = 'pams_valuation_evidence_validate_inputs') THEN
        CREATE TRIGGER pams_valuation_evidence_validate_inputs BEFORE INSERT ON public.valuation_evidence
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_validate_inputs();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.valuation_evidence'::regclass AND tgname = 'pams_valuation_evidence_no_truncate') THEN
        CREATE TRIGGER pams_valuation_evidence_no_truncate BEFORE TRUNCATE ON public.valuation_evidence
            FOR EACH STATEMENT EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
END;
$$;

-- Immutable account-scope valuation evidence. A partial member set never
-- establishes a complete account or a zero cash balance.
CREATE TABLE IF NOT EXISTS public.account_snapshot_version (
    version_id uuid PRIMARY KEY,
    version_number bigserial UNIQUE NOT NULL,
    account_code varchar(50) NOT NULL REFERENCES public.pams_accounts(account_code),
    as_of date NOT NULL,
    coverage_status text NOT NULL CHECK (coverage_status IN ('complete','partial','unknown')),
    cash_status text NOT NULL CHECK (cash_status IN ('known','unknown')),
    cash_amount numeric(28,10),
    source_batch_id uuid REFERENCES public.source_batches(batch_id),
    source_revision integer,
    source_sha256 char(64),
    scope_attestation jsonb,
    calculation_version text NOT NULL CHECK (length(trim(calculation_version)) > 0),
    reason text NOT NULL CHECK (length(trim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((cash_status='known' AND (cash_amount IS NOT NULL OR
        COALESCE((jsonb_typeof(scope_attestation->'cash_amounts')='object' AND scope_attestation->'cash_amounts' <> '{}'::jsonb),false))) OR
        (cash_status='unknown' AND cash_amount IS NULL)),
    CHECK (coverage_status <> 'complete' OR (cash_status='known' AND source_batch_id IS NOT NULL AND
        source_revision IS NOT NULL AND source_revision > 0 AND source_sha256 IS NOT NULL AND
        source_sha256 ~ '^[0-9a-f]{64}$' AND scope_attestation IS NOT NULL AND jsonb_typeof(scope_attestation)='object'))
);
CREATE INDEX IF NOT EXISTS idx_account_snapshot_version_scope ON public.account_snapshot_version(account_code,as_of,version_number DESC);
CREATE TABLE IF NOT EXISTS public.account_snapshot_member (
    version_id uuid NOT NULL REFERENCES public.account_snapshot_version(version_id) DEFERRABLE INITIALLY DEFERRED,
    holding_id bigint NOT NULL,
    holding_row_hash char(32) NOT NULL CHECK (holding_row_hash ~ '^[0-9a-f]{32}$'),
    source_item_id uuid REFERENCES public.source_items(source_item_id),
    PRIMARY KEY(version_id,holding_id)
);
CREATE OR REPLACE FUNCTION public.pams_snapshot_member_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('account-snapshot:' || NEW.version_id::text, 0));
    IF EXISTS (SELECT 1 FROM public.account_snapshot_version WHERE version_id=NEW.version_id) THEN
        RAISE EXCEPTION 'cannot append to a published account snapshot version';
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION public.pams_snapshot_version_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('account-import:' || NEW.account_code, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended('account-snapshot:' || NEW.version_id::text, 0));
    IF NOT EXISTS (SELECT 1 FROM public.account_snapshot_member WHERE version_id=NEW.version_id)
       OR EXISTS (SELECT 1 FROM public.account_snapshot_member m
          LEFT JOIN public.pams_holdings_snapshot h ON h.id=m.holding_id
          WHERE m.version_id=NEW.version_id AND (h.id IS NULL OR h.account_code IS DISTINCT FROM NEW.account_code
              OR h.as_of IS DISTINCT FROM NEW.as_of OR md5(to_jsonb(h)::text) IS DISTINCT FROM m.holding_row_hash)) THEN
        RAISE EXCEPTION 'account snapshot members are missing or changed';
    END IF;
    RETURN NEW;
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_member'::regclass AND tgname='account_snapshot_member_no_append') THEN
        CREATE TRIGGER account_snapshot_member_no_append BEFORE INSERT ON public.account_snapshot_member
            FOR EACH ROW EXECUTE FUNCTION public.pams_snapshot_member_before_insert();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_version'::regclass AND tgname='account_snapshot_version_check') THEN
        CREATE TRIGGER account_snapshot_version_check BEFORE INSERT ON public.account_snapshot_version
            FOR EACH ROW EXECUTE FUNCTION public.pams_snapshot_version_before_insert();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_version'::regclass AND tgname='account_snapshot_version_immutable') THEN
        CREATE TRIGGER account_snapshot_version_immutable BEFORE UPDATE OR DELETE ON public.account_snapshot_version
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_member'::regclass AND tgname='account_snapshot_member_immutable') THEN
        CREATE TRIGGER account_snapshot_member_immutable BEFORE UPDATE OR DELETE ON public.account_snapshot_member
            FOR EACH ROW EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_version'::regclass AND tgname='account_snapshot_version_no_truncate') THEN
        CREATE TRIGGER account_snapshot_version_no_truncate BEFORE TRUNCATE ON public.account_snapshot_version
            FOR EACH STATEMENT EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.account_snapshot_member'::regclass AND tgname='account_snapshot_member_no_truncate') THEN
        CREATE TRIGGER account_snapshot_member_no_truncate BEFORE TRUNCATE ON public.account_snapshot_member
            FOR EACH STATEMENT EXECUTE FUNCTION public.pams_valuation_evidence_immutable();
    END IF;
END;
$$;

-- Serialize every holding mutation with account-scope review, including direct SQL writers.
CREATE OR REPLACE FUNCTION public.pams_holding_account_scope_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE first_account text;
DECLARE second_account text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        first_account := NEW.account_code;
    ELSIF TG_OP = 'DELETE' THEN
        first_account := OLD.account_code;
    ELSE
        first_account := LEAST(OLD.account_code, NEW.account_code);
        second_account := GREATEST(OLD.account_code, NEW.account_code);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('account-import:' || first_account, 0));
    IF second_account IS NOT NULL AND second_account <> first_account THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('account-import:' || second_account, 0));
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='public.pams_holdings_snapshot'::regclass AND tgname='holding_account_scope_lock') THEN
        CREATE TRIGGER holding_account_scope_lock BEFORE INSERT OR UPDATE OR DELETE ON public.pams_holdings_snapshot
            FOR EACH ROW EXECUTE FUNCTION public.pams_holding_account_scope_lock();
    END IF;
END;
$$;
