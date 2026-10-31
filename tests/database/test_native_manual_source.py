"""Source-first manual facts without legacy batch or file writes."""
# ruff: noqa: F811
import sys
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))

from domain.imports import account_import_statement as statement
from domain.imports.native_source import (
    add_manual_record,
    create_manual_batch,
    preview_native,
    publish_native,
)
from domain.imports.unified_source import review_item

from tests.imports.test_account_import_publish import DAY, client, holding, trade  # noqa: F401


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        for name in ('074_unified_source.sql', '075_source_fact_links.sql', '076_valuation_input_evidence.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    return params


def create_reviewed(params, fields, *, account='tonghuashun'):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        batch = create_manual_batch(cur, account_code=account, kind=fields['kind'],
                                    completeness='partial', institution='synthetic')
        item = add_manual_record(cur, batch_id=batch, fields=fields)
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (str(batch),))
        revision = cur.fetchone()['revision']
        cur.execute('SELECT parsed_fields FROM source_items WHERE source_item_id=%s', (str(item),))
        parsed = cur.fetchone()['parsed_fields']
        revision = review_item(cur, batch_id=batch, item_id=item, expected_revision=revision,
                               parsed_fields=parsed, review_status='confirmed')
        preview = preview_native(cur, batch_id=batch, revision=revision)
        return batch, item, revision, preview


def publish(cur, batch, revision, preview):
    return publish_native(cur, batch_id=batch, revision=revision, source_sha256=preview['source_sha256'],
                          preview_token=preview['preview']['preview_token'], prepared_at=preview['prepared_at'])


@pytest.mark.parametrize('fields', [trade(source_ref='native-unique-trade'), holding()])
def test_source_first_trade_and_holding_publish_without_legacy_drafts(isolated, fields):
    batch, item, revision, preview = create_reviewed(isolated, fields)
    assert preview['preview']['can_publish']
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish(cur, batch, revision, preview)
            assert not result['replayed'] and len(result['records']) == 1
            cur.execute('SELECT count(*) AS n FROM pams_account_import_batch')
            assert cur.fetchone()['n'] == 0
            cur.execute('SELECT count(*) AS n FROM pams_account_import_file')
            assert cur.fetchone()['n'] == 0
            table = result['records'][0][0]
            cur.execute(f'SELECT source_item_id FROM {table}')
            assert str(cur.fetchone()['source_item_id']) == str(item)
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert publish(cur, batch, revision, preview)['replayed']


def test_native_funding_creates_cash_leg_and_replays(isolated):
    fields = {'kind': 'cash', 'business_date': DAY, 'currency': 'CNY', 'action': 'deposit',
              'net_cash': '500', 'counter_account_code': 'bank_test'}
    batch, item, revision, preview = create_reviewed(isolated, fields)
    assert preview['preview']['can_publish']
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = publish(cur, batch, revision, preview)
            assert len(result['records']) == 2
            cur.execute('SELECT cashflow_id,source_item_id FROM pams_investment_trade_fact')
            trade_fact = cur.fetchone()
            cur.execute('SELECT cashflow_id,source_item_id,amount FROM pams_cashflow_fact')
            cash = cur.fetchone()
            assert trade_fact['cashflow_id'] == cash['cashflow_id'] and cash['amount'] == -500
            assert str(trade_fact['source_item_id']) == str(cash['source_item_id']) == str(item)
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert publish(cur, batch, revision, preview)['replayed']


def test_native_bank_cashflow_is_source_first(isolated, monkeypatch):
    monkeypatch.setattr(statement, 'config', lambda: {'profiles': {'test': {'account_code': 'bank_test',
        'profile_code': 'test', 'statement_template': 'manual_statement_csv', 'currency': 'CNY'}}, 'rules': [], 'amount_rules': []})
    fields = {'kind': 'cash', 'business_date': DAY, 'currency': 'CNY', 'asset_type': 'cash',
              'action': 'expense', 'net_cash': '-25.10', 'note': 'synthetic'}
    batch, item, revision, preview = create_reviewed(isolated, fields, account='bank_test')
    assert preview['preview']['can_publish']
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            assert len(publish(cur, batch, revision, preview)['records']) == 1
            cur.execute('SELECT amount,source_item_id,source_table FROM pams_cashflow_fact')
            fact = cur.fetchone()
            assert str(fact['amount']) == '-25.1000' and str(fact['source_item_id']) == str(item)
            assert fact['source_table'] == 'source_items'


def test_native_failed_publication_rolls_back_facts_and_batch(isolated):
    batch, _, revision, preview = create_reviewed(isolated, trade())
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION reject_native_source() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic failure'; END $$""")
        cur.execute('CREATE TRIGGER reject_native_source BEFORE UPDATE OF source_item_id ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION reject_native_source()')
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(psycopg2.errors.RaiseException):
                publish(cur, batch, revision, preview)
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 0
            cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (str(batch),))
            assert cur.fetchone()['status'] == 'draft'


def test_unexpected_draft_association_blocks_whole_publication(isolated):
    batch, item, revision, preview = create_reviewed(isolated, trade(source_ref='extra-association'))
    with psycopg2.connect(**isolated) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO pams_cashflow_fact(event_date,account_code,amount,amount_cny,source_table,
            source_id,source_hash,source_item_id) VALUES (%s,'bank_test',10,10,'unexpected','1',%s,%s)""",
            (DAY, 'a' * 64, str(item)))
    with psycopg2.connect(**isolated) as conn:
        conn.set_session(isolation_level='SERIALIZABLE')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            with pytest.raises(ValueError, match='unexpected source fact association'):
                publish(cur, batch, revision, preview)
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 0
            cur.execute('SELECT status FROM source_batches WHERE batch_id=%s', (str(batch),))
            assert cur.fetchone()['status'] == 'draft'
