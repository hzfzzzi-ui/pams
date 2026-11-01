"""Flex source synchronization checks its schema without runtime DDL."""
# ruff: noqa: F811
import psycopg2
import pytest
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from collectors import ibkr_flex
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_missing_sync_schema_fails_without_creating_it(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        with pytest.raises(ibkr_flex.IBKRFlexError, match='broker_processing_state'):
            ibkr_flex.ensure_sync_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.broker_processing_state')")
            assert cur.fetchone()[0] is None


def test_source_schema_or_isolation_missing_stops_before_network(empty_source_db, monkeypatch):
    monkeypatch.setattr(ibkr_flex, 'request_flex_reference', lambda *a, **k: pytest.fail('network must not run'))
    monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'false')
        with pytest.raises(ValueError, match='requires isolated mode'):
            ibkr_flex.sync_ibkr_flex_to_db(conn, token='test', query_id='test')
        monkeypatch.setenv('PAMS_HOLDING_EVIDENCE_ISOLATED', 'true')
        with conn.cursor() as cur:
            cur.execute('DROP TABLE source_items CASCADE')
        conn.commit()
        with pytest.raises(ibkr_flex.IBKRFlexError, match='source_items'):
            ibkr_flex.sync_ibkr_flex_to_db(conn, token='test', query_id='test')
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.source_items')")
            assert cur.fetchone()[0] is None
