-- Existing database upgrade; never part of empty-database bootstrap.
BEGIN;
ALTER TABLE public.evidence_confirmation DROP CONSTRAINT evidence_confirmation_confirmation_type_check;
ALTER TABLE public.evidence_confirmation ADD CONSTRAINT evidence_confirmation_confirmation_type_check
    CHECK (confirmation_type IN ('transfer','bank_source','payment_proxy','account_review','cash_reversal'));
ALTER TABLE public.evidence_confirmation DROP CONSTRAINT evidence_confirmation_check2;
ALTER TABLE public.evidence_confirmation ADD CONSTRAINT evidence_confirmation_check2 CHECK (
  (confirmation_type IN ('transfer','payment_proxy') AND num_nonnulls(from_cashflow_id,from_trade_id)=1
   AND (confirmation_type <> 'payment_proxy' OR (from_cashflow_id IS NOT NULL AND to_cashflow_id IS NOT NULL))
   AND num_nonnulls(to_cashflow_id,to_trade_id)=1 AND from_account_code IS NOT NULL AND to_account_code IS NOT NULL
   AND from_account_code <> to_account_code AND amount IS NOT NULL AND amount > 0
   AND amount::text NOT IN ('NaN','Infinity','-Infinity') AND currency IS NOT NULL
   AND jsonb_typeof(from_evidence)='object' AND jsonb_typeof(to_evidence)='object'
   AND from_evidence IS NOT NULL AND to_evidence IS NOT NULL
   AND cashflow_id IS NULL AND source_item_id IS NULL AND evidence IS NULL)
  OR
  (confirmation_type='cash_reversal' AND from_cashflow_id IS NOT NULL AND to_cashflow_id IS NOT NULL
   AND from_cashflow_id<>to_cashflow_id AND from_trade_id IS NULL AND to_trade_id IS NULL
   AND from_account_code IS NOT NULL AND to_account_code IS NOT NULL AND from_account_code=to_account_code
   AND amount IS NOT NULL AND amount>0 AND amount::text NOT IN ('NaN','Infinity','-Infinity') AND currency IS NOT NULL
   AND jsonb_typeof(from_evidence)='object' AND jsonb_typeof(to_evidence)='object'
   AND from_evidence IS NOT NULL AND to_evidence IS NOT NULL
   AND cashflow_id IS NULL AND source_item_id IS NULL AND evidence IS NULL)
  OR
  (confirmation_type='bank_source' AND cashflow_id IS NOT NULL AND source_item_id IS NOT NULL
   AND evidence IS NOT NULL AND jsonb_typeof(evidence)='object' AND from_cashflow_id IS NULL AND from_trade_id IS NULL
   AND to_cashflow_id IS NULL AND to_trade_id IS NULL AND from_account_code IS NULL AND to_account_code IS NULL
   AND amount IS NULL AND currency IS NULL AND from_evidence IS NULL AND to_evidence IS NULL)
  OR
  (confirmation_type='account_review' AND from_account_code IS NOT NULL AND to_account_code IS NULL
   AND jsonb_typeof(evidence)='object' AND evidence IS NOT NULL AND from_cashflow_id IS NULL AND from_trade_id IS NULL
   AND to_cashflow_id IS NULL AND to_trade_id IS NULL AND amount IS NULL AND currency IS NULL
   AND from_evidence IS NULL AND to_evidence IS NULL AND cashflow_id IS NULL AND source_item_id IS NULL)
);
COMMIT;
