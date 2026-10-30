-- Isolated rehearsal only; never execute against an application database.
-- The simulation account subsystem is being removed by user direction.
-- Keep asset facts, transaction sync, lifecycle, sentiment and market tables.
-- No CASCADE: unexpected dependencies abort the whole transaction.
BEGIN;

DO $guard$
BEGIN
    IF current_database() !~ '^pams_sim_cleanup_test_[0-9a-f]{32}$'
       OR current_setting('pams.sim_cleanup_rehearsal', true) IS DISTINCT FROM 'on'
       OR (SELECT shobj_description(oid, 'pg_database') FROM pg_database
           WHERE datname = current_database()) IS DISTINCT FROM 'pams-isolated-simulation-cleanup' THEN
        RAISE EXCEPTION 'Simulation cleanup requires an explicitly marked disposable test database';
    END IF;
END
$guard$;

DROP VIEW IF EXISTS public.pams_sim_reconcile_summaries;
DROP VIEW IF EXISTS public.pams_sim_sync_logs;
DROP VIEW IF EXISTS public.pams_sim_cash_ledger;
DROP VIEW IF EXISTS public.pams_sim_holdings_snapshots;
DROP VIEW IF EXISTS public.pams_sim_accounts;

DROP TABLE IF EXISTS pams_module.pams_sim_reconcile_summaries;
DROP TABLE IF EXISTS pams_module.pams_sim_sync_logs;
DROP TABLE IF EXISTS pams_module.pams_sim_cash_ledger;
DROP TABLE IF EXISTS pams_module.pams_sim_holdings_snapshots;
DROP TABLE IF EXISTS pams_module.pams_sim_accounts;

COMMIT;
