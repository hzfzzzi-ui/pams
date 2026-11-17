-- One published read model for the investment page. The underlying facts,
-- audited valuations and return snapshots keep their existing identities.
CREATE TABLE IF NOT EXISTS public.investment_page_projection (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    published_revision bigint,
    business_date date,
    published_at timestamptz,
    payload jsonb,
    CHECK (
        (published_revision IS NULL AND business_date IS NULL AND published_at IS NULL AND payload IS NULL)
        OR
        (published_revision IS NOT NULL AND business_date IS NOT NULL AND published_at IS NOT NULL
         AND payload IS NOT NULL AND jsonb_typeof(payload) = 'object')
    )
);

INSERT INTO public.investment_page_projection (singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

-- One marker per committing writer, without making concurrent fact writers lock the same row.
CREATE TABLE IF NOT EXISTS public.investment_projection_invalidation (
    writer xid8 PRIMARY KEY
);

CREATE OR REPLACE FUNCTION public.invalidate_investment_page_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.investment_projection_invalidation (writer) VALUES (pg_current_xact_id())
    ON CONFLICT (writer) DO NOTHING;
    RETURN NULL;
END $$;

DO $$
DECLARE
    input_table text;
BEGIN
    FOREACH input_table IN ARRAY ARRAY[
        'pams_accounts', 'pams_asset_master', 'pams_asset_alias', 'asset_classifications',
        'pams_holdings_snapshot', 'pams_investment_trade_fact', 'pams_cashflow_fact',
        'pams_fx_rates_daily', 'market_observations', 'pams_ohlcv_daily', 'valuation_results',
        'source_records', 'source_items', 'evidence_confirmation', 'broker_processing_state',
        'valuation_evidence', 'account_snapshot_version', 'account_snapshot_member',
        'investment_trade_review', 'cashflow_classification',
        'ibkr_source_originals', 'tonghuashun_source_originals'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS invalidate_investment_page_projection ON public.%I', input_table);
        EXECUTE format('CREATE TRIGGER invalidate_investment_page_projection '
            'AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION public.invalidate_investment_page_projection()', input_table);
    END LOOP;
END $$;
