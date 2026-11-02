-- Separate business classification from source-confirmed cashflow facts.
CREATE TABLE public.cashflow_classification (
    cashflow_id BIGINT PRIMARY KEY REFERENCES public.pams_cashflow_fact(cashflow_id) ON DELETE RESTRICT,
    cashflow_type VARCHAR(40), category VARCHAR(80), subcategory VARCHAR(80),
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','suggested','confirmed')),
    classification_source VARCHAR(80), confidence NUMERIC(6,4), note TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (cashflow_type IS NULL OR cashflow_type IN ('operating_income','operating_expense',
        'internal_transfer','investment_funding','investment_withdrawal','investment_return_cash',
        'debt_repayment','debt_drawdown','fee','tax','balance_adjustment','fx_conversion')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    CHECK (status <> 'confirmed' OR (cashflow_type IS NOT NULL AND classification_source IS NOT NULL
        AND length(btrim(classification_source)) > 0))
);
