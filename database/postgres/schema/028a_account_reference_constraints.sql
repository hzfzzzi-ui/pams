-- Account-reference constraints. No account seed or historical backfill.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_cashflow_fact_account_code'
    ) THEN
        ALTER TABLE pams_cashflow_fact
            ADD CONSTRAINT fk_pams_cashflow_fact_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_cashflow_fact_counter_account_code'
    ) THEN
        ALTER TABLE pams_cashflow_fact
            ADD CONSTRAINT fk_pams_cashflow_fact_counter_account_code
            FOREIGN KEY (counter_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_source_row_account_code'
    ) THEN
        ALTER TABLE pams_source_row
            ADD CONSTRAINT fk_pams_source_row_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_external_transaction_source_account_code'
    ) THEN
        ALTER TABLE pams_external_transaction_source
            ADD CONSTRAINT fk_pams_external_transaction_source_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_investment_trade_fact_account_code'
    ) THEN
        ALTER TABLE pams_investment_trade_fact
            ADD CONSTRAINT fk_pams_investment_trade_fact_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_investment_trade_snapshot_account_code'
    ) THEN
        ALTER TABLE pams_investment_trade_snapshot
            ADD CONSTRAINT fk_pams_investment_trade_snapshot_account_code
            FOREIGN KEY (account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_reconciliation_case_from_account_code'
    ) THEN
        ALTER TABLE pams_reconciliation_case
            ADD CONSTRAINT fk_pams_reconciliation_case_from_account_code
            FOREIGN KEY (from_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_pams_reconciliation_case_to_account_code'
    ) THEN
        ALTER TABLE pams_reconciliation_case
            ADD CONSTRAINT fk_pams_reconciliation_case_to_account_code
            FOREIGN KEY (to_account_code)
            REFERENCES pams_accounts(account_code)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
            NOT VALID;
    END IF;
END $$;

ALTER TABLE pams_cashflow_fact VALIDATE CONSTRAINT fk_pams_cashflow_fact_account_code;
ALTER TABLE pams_cashflow_fact VALIDATE CONSTRAINT fk_pams_cashflow_fact_counter_account_code;
ALTER TABLE pams_source_row VALIDATE CONSTRAINT fk_pams_source_row_account_code;
ALTER TABLE pams_external_transaction_source VALIDATE CONSTRAINT fk_pams_external_transaction_source_account_code;
ALTER TABLE pams_investment_trade_fact VALIDATE CONSTRAINT fk_pams_investment_trade_fact_account_code;
ALTER TABLE pams_investment_trade_snapshot VALIDATE CONSTRAINT fk_pams_investment_trade_snapshot_account_code;
ALTER TABLE pams_reconciliation_case VALIDATE CONSTRAINT fk_pams_reconciliation_case_from_account_code;
ALTER TABLE pams_reconciliation_case VALIDATE CONSTRAINT fk_pams_reconciliation_case_to_account_code;

COMMIT;
