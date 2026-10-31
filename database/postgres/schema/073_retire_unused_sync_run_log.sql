-- Candidate for an isolated upgrade only. See docs/CONFIGURATION.md before use.
-- RESTRICT is intentional: never remove dependent database objects implicitly.
DROP TABLE IF EXISTS public.pams_sync_run_log RESTRICT;
