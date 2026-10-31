"""Synthetic Flex position evidence; missing fields never imply retirement."""
# ruff: noqa: F811
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from domain.imports.ibkr_holding_source_candidate import stage_report, retirement_status
from domain.imports.unified_source import review_item, lock_source_review

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 22)


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('ibkr','Synthetic')")
        cur.execute((ROOT / 'database/postgres/schema/080_unified_source_staging.sql').read_text(encoding='utf-8'))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    return params


def xml(*positions):
    return ('<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022">'
            '<OpenPositions>' + ''.join(positions) + '</OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>').encode()


ZERO = '<OpenPosition accountId="U_TEST" symbol="TEST" currency="USD" reportDate="20261022" position="0" costBasisMoney="0" marketValue="0" />'


def stage(params, report):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return stage_report(cur, batch_id=uuid4(), account_code='ibkr', source_account_id='U_TEST',
                            business_date=DAY, content=report)


def confirm(params, staged, *, complete=True):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        batch = staged['batch_id']
        cur.execute("UPDATE source_batches SET completeness=%s WHERE batch_id=%s", ('complete' if complete else 'partial', batch))
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s', (batch,))
        revision = cur.fetchone()['revision']
        revision = review_item(cur, batch_id=UUID(batch), item_id=UUID(staged['scope_item_id']), expected_revision=revision,
            parsed_fields={'coverage': 'all_positions', 'confirmed_complete': complete},
            review_status='confirmed', review_note='synthetic scope verified')
        for item in staged['position_item_ids']:
            revision = review_item(cur, batch_id=UUID(batch), item_id=UUID(item), expected_revision=revision,
                                   parsed_fields={}, review_status='confirmed', review_note='synthetic position checked')
        snap = lock_source_review(cur, batch_id=UUID(batch), expected_revision=revision)
        cur.execute("UPDATE source_batches SET status='published',published_source_hash=%s WHERE batch_id=%s",
                    (snap['source_snapshot_sha256'], batch))


def status(params, staged, item_id=None, account='ibkr', day=DAY):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return retirement_status(cur, batch_id=staged['batch_id'], item_id=item_id or staged['position_item_ids'][0],
                                 account_code=account, business_date=day)


def test_reviewed_scope_and_explicit_three_zero_fields_allow_retirement(isolated):
    report = xml(ZERO)
    staged = stage(isolated, report)
    assert staged['positions'][0]['state'] == 'explicit_zero'
    assert status(isolated, staged)['state'] == 'unknown'
    confirm(isolated, staged)
    result = status(isolated, staged)
    assert result['state'] == 'confirmed_zero' and result['symbol'] == 'TEST'
    assert result['source_sha256'] == sha256(report).hexdigest()
    scope = sql(isolated, "SELECT raw_fields FROM source_items WHERE source_uid='scope'")[0]['raw_fields']
    assert scope['raw_xml'].encode('utf-8') == report


@pytest.mark.parametrize('position', [
    ZERO.replace(' costBasisMoney="0"', ''),
    ZERO.replace(' marketValue="0"', ''),
    ZERO.replace(' position="0"', ''),
    ZERO.replace(' reportDate="20261022"', ''),
    ZERO.replace(' currency="USD"', ''),
    ZERO.replace('accountId="U_TEST"', 'accountId="OTHER"'),
])
def test_missing_or_conflicting_evidence_is_unknown(isolated, position):
    staged = stage(isolated, xml(position))
    assert staged['positions'][0]['state'] == 'unknown'
    confirm(isolated, staged)
    assert status(isolated, staged)['state'] == 'unknown'


def test_missing_position_or_incomplete_scope_is_never_retirement(isolated):
    staged = stage(isolated, xml(ZERO))
    confirm(isolated, staged, complete=False)
    assert status(isolated, staged)['state'] == 'unknown'
    assert status(isolated, staged, item_id=str(uuid4()))['state'] == 'unknown'
    assert status(isolated, staged, account='other')['state'] == 'unknown'
    assert status(isolated, staged, day=date(2026,10,21))['state'] == 'unknown'


def test_nonzero_and_duplicate_symbol_cannot_authorize_exit(isolated):
    nonzero = stage(isolated, xml(ZERO.replace('marketValue="0"', 'marketValue="1"')))
    confirm(isolated, nonzero)
    assert status(isolated, nonzero)['state'] == 'unknown'
    duplicate = stage(isolated, xml(ZERO, ZERO))
    confirm(isolated, duplicate)
    assert status(isolated, duplicate)['reason'] == 'ambiguous_symbol'


def test_missing_or_ambiguous_open_positions_section_cannot_authorize_exit(isolated):
    report = xml(ZERO).replace(b'<OpenPositions>', b'').replace(b'</OpenPositions>', b'')
    staged = stage(isolated, report)
    confirm(isolated, staged)
    assert status(isolated, staged)['reason'] == 'invalid_position_scope'


@pytest.mark.parametrize('payload', [
    b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
    b'<!DOCTYPE x [<!ENTITY a "1234567890"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>',
    b'<root>' + b'<x>' * 70 + b'</x>' * 70 + b'</root>',
    b'<root>' + b' ' * (10 * 1024 * 1024) + b'</root>',
    b'<FlexQueryResponse>\xff</FlexQueryResponse>',
], ids=['external_entity', 'entity_expansion', 'excess_depth', 'oversize', 'invalid_utf8'])
def test_unsafe_or_oversized_xml_rejected_before_any_source_write(isolated, payload):
    with pytest.raises(ValueError):
        stage(isolated, payload)
    assert sql(isolated, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0
    assert sql(isolated, 'SELECT count(*) AS n FROM source_items')[0]['n'] == 0
