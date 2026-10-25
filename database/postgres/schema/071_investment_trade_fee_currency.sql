-- Nullable metadata only. No historical UPDATE, default unit or runtime migration.
ALTER TABLE public.pams_investment_trade_fact
    ADD COLUMN IF NOT EXISTS fee_currency varchar(3);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid='public.pams_investment_trade_fact'::regclass
          AND conname='ck_trade_fact_fee_currency'
    ) THEN
        ALTER TABLE public.pams_investment_trade_fact
            ADD CONSTRAINT ck_trade_fact_fee_currency
            CHECK (fee_currency IS NULL OR fee_currency ~ '^[A-Z]{3}$');
    END IF;
END $$;

COMMENT ON COLUMN public.pams_investment_trade_fact.fee_currency IS
    'Explicit commission unit from source evidence; NULL is unknown. Not a cash settlement or extra fee deduction.';
