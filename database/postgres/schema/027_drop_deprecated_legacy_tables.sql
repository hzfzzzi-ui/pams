-- 027_drop_deprecated_legacy_tables.sql
--
-- Final cleanup after 026_core_architecture_refactor.sql:
-- - Deprecated PAMS source/mapping tables were migrated to target P0/P1 tables.
-- - The old tables were moved to pams_legacy and exposed through public views.
-- - This migration removes the deprecated legacy schema and dependent public
--   compatibility views.
--
-- Restore note:
-- A pre-drop archive is kept under
-- database/postgres/archive/pams_legacy_drop_20260606_103604/.

BEGIN;

DROP SCHEMA IF EXISTS pams_legacy CASCADE;

COMMIT;
