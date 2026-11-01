-- Durable broker publication and cash processing state, not runtime logs.
CREATE TABLE public.broker_processing_state (
    state_id BIGSERIAL PRIMARY KEY,
    state_type TEXT NOT NULL CHECK (state_type IN ('publication', 'cash_progress')),
    publication_id BIGINT GENERATED ALWAYS AS (CASE WHEN state_type='publication' THEN state_id END) STORED,
    progress_id BIGINT GENERATED ALWAYS AS (CASE WHEN state_type='cash_progress' THEN state_id END) STORED,
    source_batch_id UUID NOT NULL REFERENCES public.source_batches(batch_id),

    -- Report publication fields.
    account_code TEXT REFERENCES public.pams_accounts(account_code),
    statement_date DATE,
    source_sha256 CHAR(64) CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    replay_fingerprint CHAR(64) CHECK (replay_fingerprint ~ '^[0-9a-f]{64}$'),
    published_result_sha256 CHAR(64) CHECK (published_result_sha256 ~ '^[0-9a-f]{64}$'),
    holding_count INTEGER CHECK (holding_count >= 0),
    cash_state JSONB CHECK (cash_state IS NULL OR jsonb_typeof(cash_state) = 'object'),
    cash_state_sha256 CHAR(64) CHECK (cash_state_sha256 ~ '^[0-9a-f]{64}$'),

    -- Per-business-day cash processing fields.
    account_hash CHAR(64) CHECK (account_hash ~ '^[0-9a-f]{64}$'),
    business_date DATE,
    report_sha256 CHAR(64) CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    statement_sha256 CHAR(64) CHECK (statement_sha256 ~ '^[0-9a-f]{64}$'),
    trade_version TEXT,
    retryable_count INTEGER CHECK (retryable_count >= 0),
    pending JSONB CHECK (jsonb_typeof(pending) = 'array'),
    state_sha256 CHAR(64) CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),

    CHECK (
      (state_type='publication' AND account_code IS NOT NULL AND statement_date IS NOT NULL
       AND source_sha256 IS NOT NULL AND replay_fingerprint IS NOT NULL
       AND published_result_sha256 IS NOT NULL AND holding_count IS NOT NULL
       AND cash_state_sha256 IS NOT NULL AND account_hash IS NULL AND business_date IS NULL
       AND report_sha256 IS NULL AND statement_sha256 IS NULL AND trade_version IS NULL
       AND retryable_count IS NULL AND pending IS NULL AND state_sha256 IS NULL)
      OR
      (state_type='cash_progress' AND account_hash IS NOT NULL AND business_date IS NOT NULL
       AND report_sha256 IS NOT NULL AND statement_sha256 IS NOT NULL AND retryable_count IS NOT NULL
       AND pending IS NOT NULL AND state_sha256 IS NOT NULL AND account_code IS NULL
       AND statement_date IS NULL AND source_sha256 IS NULL AND replay_fingerprint IS NULL
       AND published_result_sha256 IS NULL AND holding_count IS NULL AND cash_state IS NULL
       AND cash_state_sha256 IS NULL)
    )
);
CREATE INDEX broker_publication_date_idx
    ON public.broker_processing_state (account_code, statement_date DESC, publication_id DESC)
    WHERE state_type='publication';
CREATE INDEX broker_cash_progress_scope_idx
    ON public.broker_processing_state (account_hash, business_date, progress_id DESC)
    WHERE state_type='cash_progress';

CREATE FUNCTION public.pams_validate_broker_processing_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        RAISE EXCEPTION 'broker business processing evidence is immutable';
    END IF;
    IF NEW.state_type='publication' AND NOT EXISTS (SELECT 1 FROM public.source_batches b
        JOIN public.source_items i ON i.batch_id=b.batch_id AND i.source_uid='scope'
        WHERE b.batch_id=NEW.source_batch_id AND b.status='published' AND b.account_code=NEW.account_code
          AND b.account_code='ibkr' AND b.institution='ibkr' AND b.workflow='ibkr_report'
          AND b.date_to=NEW.statement_date AND i.review_status='confirmed'
          AND i.raw_fields->>'kind'='report' AND i.raw_fields->>'sha256'=NEW.source_sha256
          AND i.raw_fields->>'business_date'=NEW.statement_date::text) THEN
        RAISE EXCEPTION 'broker publication source identity mismatch';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER broker_processing_state_guard BEFORE INSERT OR UPDATE OR DELETE ON public.broker_processing_state
    FOR EACH ROW EXECUTE FUNCTION public.pams_validate_broker_processing_state();
