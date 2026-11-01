-- Explicit confirmations are immutable business decisions; revocation is a new event.
CREATE TABLE public.evidence_confirmation (
    confirmation_id uuid PRIMARY KEY,
    confirmation_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    confirmation_type text NOT NULL CHECK (confirmation_type IN ('transfer', 'bank_source', 'payment_proxy', 'account_review')),
    from_cashflow_id bigint REFERENCES public.pams_cashflow_fact(cashflow_id),
    from_trade_id bigint REFERENCES public.pams_investment_trade_fact(trade_id),
    to_cashflow_id bigint REFERENCES public.pams_cashflow_fact(cashflow_id),
    to_trade_id bigint REFERENCES public.pams_investment_trade_fact(trade_id),
    from_account_code text REFERENCES public.pams_accounts(account_code),
    to_account_code text REFERENCES public.pams_accounts(account_code),
    amount numeric(28,10), currency text,
    from_evidence jsonb, to_evidence jsonb,
    cashflow_id bigint REFERENCES public.pams_cashflow_fact(cashflow_id),
    source_item_id uuid REFERENCES public.source_items(source_item_id),
    evidence jsonb,
    note text NOT NULL CHECK (confirmation_type = 'account_review' OR length(trim(note)) > 0),
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revocation_note text,
    CHECK ((revoked_at IS NULL AND revocation_note IS NULL) OR
           (revoked_at IS NOT NULL AND revocation_note IS NOT NULL AND length(trim(revocation_note)) > 0)),
    CHECK (
      (confirmation_type IN ('transfer', 'payment_proxy') AND num_nonnulls(from_cashflow_id,from_trade_id)=1
       AND (confirmation_type <> 'payment_proxy' OR (from_cashflow_id IS NOT NULL AND to_cashflow_id IS NOT NULL))
       AND num_nonnulls(to_cashflow_id,to_trade_id)=1 AND from_account_code IS NOT NULL
       AND to_account_code IS NOT NULL AND from_account_code <> to_account_code
       AND amount IS NOT NULL AND amount > 0 AND amount::text NOT IN ('NaN','Infinity','-Infinity') AND currency IS NOT NULL
       AND jsonb_typeof(from_evidence)='object' AND jsonb_typeof(to_evidence)='object'
       AND from_evidence IS NOT NULL AND to_evidence IS NOT NULL
       AND cashflow_id IS NULL AND source_item_id IS NULL AND evidence IS NULL)
      OR
      (confirmation_type = 'bank_source' AND cashflow_id IS NOT NULL AND source_item_id IS NOT NULL
       AND evidence IS NOT NULL AND jsonb_typeof(evidence)='object' AND from_cashflow_id IS NULL AND from_trade_id IS NULL
       AND to_cashflow_id IS NULL AND to_trade_id IS NULL AND from_account_code IS NULL
       AND to_account_code IS NULL AND amount IS NULL AND currency IS NULL
       AND from_evidence IS NULL AND to_evidence IS NULL)
      OR
      (confirmation_type = 'account_review' AND from_account_code IS NOT NULL AND to_account_code IS NULL
       AND jsonb_typeof(evidence)='object' AND evidence IS NOT NULL
       AND from_cashflow_id IS NULL AND from_trade_id IS NULL AND to_cashflow_id IS NULL AND to_trade_id IS NULL
       AND amount IS NULL AND currency IS NULL AND from_evidence IS NULL AND to_evidence IS NULL
       AND cashflow_id IS NULL AND source_item_id IS NULL)
    )
);
CREATE FUNCTION public.pams_prevent_confirmation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.revoked_at IS NULL AND OLD.revocation_note IS NULL
       AND NEW.revoked_at IS NOT NULL AND length(trim(NEW.revocation_note)) > 0
       AND (to_jsonb(NEW) - 'revoked_at' - 'revocation_note') =
           (to_jsonb(OLD) - 'revoked_at' - 'revocation_note') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'confirmation evidence is immutable; only a single revocation is allowed';
END;
$$;
CREATE TRIGGER immutable_confirmation BEFORE UPDATE OR DELETE ON public.evidence_confirmation
    FOR EACH ROW EXECUTE FUNCTION public.pams_prevent_confirmation_mutation();
