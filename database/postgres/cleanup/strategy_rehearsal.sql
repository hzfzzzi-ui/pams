-- Isolated rehearsal only. This script must never run against an application database.
-- Shared policy, fundamental/industry, sentiment, simulation and market data are retained.
-- No CASCADE: unknown dependencies abort the transaction.
BEGIN;

DO $guard$
BEGIN
    IF current_database() !~ '^pams_cleanup_test_[0-9a-f]{32}$'
       OR current_setting('pams.cleanup_rehearsal', true) IS DISTINCT FROM 'on'
       OR (SELECT shobj_description(oid, 'pg_database') FROM pg_database
           WHERE datname = current_database()) IS DISTINCT FROM 'pams-isolated-strategy-cleanup' THEN
        RAISE EXCEPTION 'Strategy cleanup requires an explicitly marked disposable test database';
    END IF;
END
$guard$;

DROP VIEW IF EXISTS public.pams_order_drafts;
DROP VIEW IF EXISTS public.pams_strategy_candidates;
DROP VIEW IF EXISTS public.pams_strategy_state;
DROP VIEW IF EXISTS public.pams_strategy_blacklist;
DROP VIEW IF EXISTS public.pams_watchlist_log;
DROP VIEW IF EXISTS public.pams_watchlist;
DROP VIEW IF EXISTS public.pams_event_monitor;
DROP VIEW IF EXISTS public.pams_position_cooldown;
DROP VIEW IF EXISTS public.pams_risk_snapshot;

DROP TABLE IF EXISTS pams_module.pams_order_drafts;
DROP TABLE IF EXISTS pams_module.pams_strategy_candidates;
DROP TABLE IF EXISTS pams_module.pams_strategy_state;
DROP TABLE IF EXISTS pams_module.pams_strategy_blacklist;
DROP TABLE IF EXISTS pams_module.pams_watchlist_log;
DROP TABLE IF EXISTS pams_module.pams_watchlist;
DROP TABLE IF EXISTS pams_module.pams_event_monitor;
DROP TABLE IF EXISTS pams_module.pams_position_cooldown;
DROP TABLE IF EXISTS pams_module.pams_risk_snapshot;

COMMIT;
