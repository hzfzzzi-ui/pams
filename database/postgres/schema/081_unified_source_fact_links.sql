-- Isolated candidate only: not a production upgrade or bootstrap manifest.
-- Preserve existing source_table/source_id/hash until each writer is migrated.
-- One source item may generate multiple facts, including a counter-account
-- cash leg. Do not add a uniqueness or same-account constraint here.
ALTER TABLE public.pams_cashflow_fact
    ADD COLUMN source_item_id UUID REFERENCES public.source_items(source_item_id) ON DELETE RESTRICT;
ALTER TABLE public.pams_investment_trade_fact
    ADD COLUMN source_item_id UUID REFERENCES public.source_items(source_item_id) ON DELETE RESTRICT;
ALTER TABLE public.pams_holdings_snapshot
    ADD COLUMN source_item_id UUID REFERENCES public.source_items(source_item_id) ON DELETE RESTRICT;

CREATE INDEX idx_cashflow_source_item ON public.pams_cashflow_fact(source_item_id);
CREATE INDEX idx_trade_source_item ON public.pams_investment_trade_fact(source_item_id);
CREATE INDEX idx_holding_source_item ON public.pams_holdings_snapshot(source_item_id);
