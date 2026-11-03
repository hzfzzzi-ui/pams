-- Link holdings to the redesigned source item; completeness is separate evidence.
ALTER TABLE public.pams_holdings_snapshot
    ADD COLUMN source_item_id UUID REFERENCES public.source_items(source_item_id) ON DELETE RESTRICT;
CREATE INDEX idx_holding_source_item ON public.pams_holdings_snapshot(source_item_id);
