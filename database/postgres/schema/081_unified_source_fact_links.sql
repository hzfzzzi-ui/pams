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

-- Financial values may still follow their existing correction paths. Once a
-- source batch is published, changing or deleting its fact association must
-- instead go through a future explicit, auditable correction workflow.
-- Lock both parents in a deterministic order to serialize with publication.
CREATE FUNCTION public.source_fact_link_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_item UUID; new_item UUID; parent RECORD;
BEGIN
    IF TG_OP <> 'INSERT' THEN old_item := OLD.source_item_id; END IF;
    IF TG_OP <> 'DELETE' THEN new_item := NEW.source_item_id; END IF;
    IF old_item IS NOT DISTINCT FROM new_item THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    FOR parent IN
        SELECT b.status FROM public.source_items i
        JOIN public.source_batches b ON b.batch_id = i.batch_id
        WHERE i.source_item_id IN (old_item, new_item)
        ORDER BY b.batch_id, i.source_item_id FOR SHARE OF b
    LOOP
        IF parent.status = 'published' THEN
            RAISE EXCEPTION 'published source fact association is immutable' USING ERRCODE = '23514';
        END IF;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

CREATE TRIGGER cashflow_source_fact_link_guard BEFORE INSERT OR UPDATE OR DELETE ON public.pams_cashflow_fact
    FOR EACH ROW EXECUTE FUNCTION public.source_fact_link_guard();
CREATE TRIGGER trade_source_fact_link_guard BEFORE INSERT OR UPDATE OR DELETE ON public.pams_investment_trade_fact
    FOR EACH ROW EXECUTE FUNCTION public.source_fact_link_guard();
CREATE TRIGGER holding_source_fact_link_guard BEFORE INSERT OR UPDATE OR DELETE ON public.pams_holdings_snapshot
    FOR EACH ROW EXECUTE FUNCTION public.source_fact_link_guard();
