"""Native same-day holding correction with immutable source/version evidence."""
# ruff: noqa: F811
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/api'))

from domain.investments.holding_evidence_writer import (
    finish_holding_change,
    prepare_holding_change,
)
from domain.investments.valuation_input_evidence import (
    current_holding_source,
    versions_for_source_item,
)

from tests.imports.test_account_import_publish import (  # noqa: F401
    DAY,
    client,
    draft,
    holding,
    preview,
    publish,
    sql,
)


@pytest.fixture
def isolated(client, monkeypatch):
    web, params = client
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', 'a1' * 32)
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        for name in ('074_unified_source.sql', '075_source_fact_links.sql',
                     '076_valuation_input_evidence.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    return web, params


def publish_holding(web, **values):
    batch = draft(web, [holding(**values)])
    check = preview(web, batch)
    assert check['can_publish'], check
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    return batch, check, response.json()


def test_same_day_update_keeps_old_source_and_records_both_versions(isolated):
    web, params = isolated
    first, first_check, _ = publish_holding(web)
    old = sql(params, 'SELECT id,source_item_id,quantity,market_value,cost FROM pams_holdings_snapshot')[0]
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert current_holding_source(cur, old['id']) == {'status':'recorded', 'source_item_id':first['rows'][0]['row_id']}
    second, second_check, result = publish_holding(web, quantity='110', market_value='1100', cost='1000')
    current = sql(params, 'SELECT id,source_item_id,quantity,market_value,cost FROM pams_holdings_snapshot')[0]
    assert current['id'] == old['id'] and current['source_item_id'] == old['source_item_id']
    assert current['quantity'] == 110 and current['market_value'] == 1100
    assert result['records'][0]['role'] == 'holding_update'
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert current_holding_source(cur, current['id']) == {'status':'recorded', 'source_item_id':second['rows'][0]['row_id']}
        roles = {row['role'] for row in versions_for_source_item(cur, first['rows'][0]['row_id'])}
        assert 'holding_creation_source' in roles and 'current_source' in roles
    assert publish(web, first, first_check).json()['replayed']
    assert publish(web, second, second_check).json()['replayed']
    snapshots = sql(params, """SELECT evidence FROM pams_valuation_input_evidence
        WHERE input_kind='holding' ORDER BY evidence->'snapshot'->'content'->>'quantity'""")
    contents = [row['evidence']['snapshot']['content'] for row in snapshots]
    assert any(str(row['quantity']) in {'100.000000', '100'} and str(row['market_value']) in {'1000.0000', '1000'} for row in contents)
    assert any(str(row['quantity']) in {'110.000000', '110'} and str(row['market_value']) in {'1100.0000', '1100'} for row in contents)
    new_item = second['rows'][0]['row_id']
    assert sql(params, """SELECT 1 FROM pams_valuation_input_evidence WHERE input_kind='source_item'
        AND evidence->>'role'='current_source' AND evidence->'source_ref'->'key'->>'source_item_id'=%s""", (new_item,))


def test_version_failure_rolls_back_holding_and_source_status(isolated):
    web, params = isolated
    publish_holding(web)
    batch = draft(web, [holding(quantity='110', market_value='1100', cost='1000')])
    check = preview(web, batch)
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION reject_new_version() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.calculation_version='holding-change-after' THEN RAISE EXCEPTION 'synthetic version failure';
            END IF; RETURN NEW; END $$""")
        cur.execute('CREATE TRIGGER reject_new_version BEFORE INSERT ON pams_valuation_result_version FOR EACH ROW EXECUTE FUNCTION reject_new_version()')
    response = publish(web, batch, check)
    assert response.status_code == 503
    current = sql(params, 'SELECT quantity,market_value FROM pams_holdings_snapshot')[0]
    assert current['quantity'] == 100 and current['market_value'] == 1000
    assert sql(params, 'SELECT status FROM source_batches WHERE batch_id=%s', (batch['batch_id'],))[0]['status'] == 'draft'


def test_concurrent_same_day_corrections_do_not_silently_overwrite(isolated):
    web, params = isolated
    publish_holding(web)
    one = draft(web, [holding(quantity='110', market_value='1100', cost='1000')])
    two = draft(web, [holding(quantity='120', market_value='1200', cost='1100')])
    checks = [preview(web, one), preview(web, two)]
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait(timeout=5)
        return publish(web, [one, two][index], checks[index]).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(attempt, (0, 1)))
    assert sorted(codes) == [200, 409], codes
    assert sql(params, 'SELECT quantity FROM pams_holdings_snapshot')[0]['quantity'] in (110, 120)


def test_cash_balance_source_can_be_versioned_and_updated(isolated):
    web, params = isolated
    fields = {'kind':'cash', 'business_date':DAY, 'currency':'CNY', 'action':'balance', 'net_cash':'100'}
    first = draft(web, [fields])
    check = preview(web, first)
    assert check['can_publish'], check
    response = publish(web, first, check)
    assert response.status_code == 200, response.text
    second = draft(web, [{**fields, 'net_cash':'150'}])
    check = preview(web, second)
    assert check['can_publish'], check
    response = publish(web, second, check)
    assert response.status_code == 200, response.text
    assert sql(params, 'SELECT market_value FROM pams_holdings_snapshot')[0]['market_value'] == 150
    assert publish(web, second, check).json()['replayed']


def test_noop_prepare_preserves_known_current_source_and_rejects_spoofed_role(isolated):
    web, params = isolated
    first, _, _ = publish_holding(web)
    row = sql(params, 'SELECT id,account_code,name,as_of FROM pams_holdings_snapshot')[0]
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        prepare_holding_change(cur, account_code=row['account_code'], name=row['name'], as_of=row['as_of'], source_item_id=None)
        assert current_holding_source(cur, row['id']) == {'status':'recorded', 'source_item_id':first['rows'][0]['row_id']}
        with pytest.raises(ValueError, match='controlled by the writer'):
            finish_holding_change(cur, holding_id=row['id'], source_item_id=None,
                                  input_refs=[{'kind':'source_item', 'key':None, 'role':'current_source'}])


def test_complete_snapshot_preserves_unchanged_member_without_duplicate_fact(isolated):
    web, params = isolated
    initial = draft(web, [holding(), holding(asset_type='cash', symbol=None, action='balance', net_cash='50')],
                    completeness='complete')
    assert publish(web, initial, preview(web, initial)).status_code == 200
    changed = draft(web, [holding(), holding(asset_type='cash', symbol=None, action='balance', net_cash='60')],
                    completeness='complete')
    check = preview(web, changed)
    assert check['can_publish'] and check['counts']['duplicates'] == 1 and check['counts']['new'] == 1
    result = publish(web, changed, check)
    assert result.status_code == 200, result.text
    assert publish(web, changed, check).json()['replayed']
    rows = sql(params, 'SELECT name,market_value FROM pams_holdings_snapshot ORDER BY name')
    assert len(rows) == 2 and sorted(float(row['market_value']) for row in rows) == [60, 1000]


def test_zero_exit_requires_complete_scope_and_preserves_unknown_omission(isolated):
    web, params = isolated
    first = draft(web, [holding(), holding(asset_type='cash', symbol=None, action='balance', net_cash='50')],
                  completeness='complete')
    assert publish(web, first, preview(web, first)).status_code == 200
    zero = holding(quantity='0', market_value='0', cost='0')
    partial = draft(web, [zero], completeness='partial')
    check = preview(web, partial)
    assert not check['can_publish'] and any(i['code'] == 'exit_scope_unverified' for r in check['rows'] for i in r['issues'])
    incomplete = draft(web, [zero], completeness='complete')
    check = preview(web, incomplete)
    assert not check['can_publish'] and any(i['code'] == 'incomplete_snapshot' for r in check['rows'] for i in r['issues'])
    confirmed = draft(web, [zero, holding(asset_type='cash', symbol=None, action='balance', net_cash='50')],
                      completeness='complete')
    check = preview(web, confirmed)
    assert check['can_publish'], check
    response = publish(web, confirmed, check)
    assert response.status_code == 200, response.text
    rows = sql(params, 'SELECT symbol,quantity,market_value,cost FROM pams_holdings_snapshot ORDER BY symbol')
    retired = next(row for row in rows if row['symbol'] == '000001')
    assert retired['quantity'] == retired['market_value'] == retired['cost'] == 0
    assert publish(web, confirmed, check).json()['replayed']
