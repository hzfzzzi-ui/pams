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
    verify_historical_holding_source,
)
from domain.investments.valuation_input_evidence import (
    bound_holding_freshness,
    current_holding_source,
    verify_recorded_version,
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
    return web, params


def publish_holding(web, **values):
    batch = draft(web, [holding(**values)])
    check = preview(web, batch)
    assert check['can_publish'], check
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    return batch, check, response.json()


def test_multiple_holdings_reuse_verified_unchanged_snapshot(isolated, monkeypatch):
    from domain.imports import source_fact_evidence
    web, _ = isolated
    batch = draft(web, [holding(), holding(symbol='011613', asset_type='fund')])
    check = preview(web, batch)
    assert check['can_publish'], check
    observed = []
    original = source_fact_evidence._reviewed_snapshot
    def observe(cur, batch, batch_id, *, locking, snapshot_cache):
        key = (batch_id, str(batch['published_source_hash']).strip())
        observed.append(key in snapshot_cache)
        return original(cur, batch, batch_id, locking=locking, snapshot_cache=snapshot_cache)
    monkeypatch.setattr(source_fact_evidence, '_reviewed_snapshot', observe)
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    assert len(response.json()['records']) == 2
    assert observed == [False]


def test_historical_lookup_rechecks_account_after_waiting_for_lock(isolated):
    from queue import Queue
    from time import monotonic, sleep
    web, params = isolated
    batch, _, _ = publish_holding(web)
    item = batch['rows'][0]['row_id']
    holding_id = sql(params, 'SELECT id FROM pams_holdings_snapshot')[0]['id']
    before = sql(params, 'SELECT count(*) AS n FROM valuation_evidence')[0]['n']
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute('ALTER TABLE pams_holdings_snapshot DISABLE TRIGGER USER')
    pid_queue = Queue()
    def lookup():
        with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT pg_backend_pid() AS pid')
            pid_queue.put(cur.fetchone()['pid'])
            result = verify_historical_holding_source(cur, holding_id=holding_id, source_item_id=item)
            conn.rollback()
            return result
    try:
        with psycopg2.connect(**params) as holder, holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:tonghuashun',0))")
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(lookup)
                pid = pid_queue.get(timeout=5)
                deadline = monotonic() + 5
                with psycopg2.connect(**params) as observer, observer.cursor() as check:
                    while monotonic() < deadline:
                        check.execute('SELECT pg_stat_clear_snapshot()')
                        check.execute('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)', (pid,))
                        if check.fetchone()[0]:
                            break
                        sleep(.02)
                    else:
                        pytest.fail('historical lookup did not wait for the account lock')
                cur.execute("UPDATE pams_holdings_snapshot SET account_code='bank_test' WHERE id=%s", (holding_id,))
                holder.commit()
                assert task.result(timeout=10) is False
        assert sql(params, 'SELECT count(*) AS n FROM valuation_evidence')[0]['n'] == before
    finally:
        with psycopg2.connect(**params) as conn, conn.cursor() as cur:
            cur.execute("UPDATE pams_holdings_snapshot SET account_code='tonghuashun' WHERE id=%s", (holding_id,))
            cur.execute('ALTER TABLE pams_holdings_snapshot ENABLE TRIGGER USER')


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
        first_versions = versions_for_source_item(cur, first['rows'][0]['row_id'])
        second_versions = versions_for_source_item(cur, second['rows'][0]['row_id'])
        roles = {row['role'] for row in first_versions}
        assert 'holding_creation_source' in roles and 'current_source' in roles
        creation_keys = {row['version_key'] for row in first_versions if row['role'] == 'holding_creation_source'}
        current_keys = {row['version_key'] for row in second_versions if row['role'] == 'current_source'}
        shared = creation_keys & current_keys
        assert len(shared) == 1 and first['batch_id'] != second['batch_id']
        version_key = shared.pop()
        assert verify_recorded_version(cur, version_key)
        # The immutable references are recorded; they do not prove the input set is complete.
        assert bound_holding_freshness(cur, version_key) == 'unknown'
        assert not any(row['role'] == 'current_source' and row['version_key'] == version_key for row in first_versions)
    versions_before = sql(params, "SELECT version_key FROM valuation_evidence ORDER BY version_key")
    assert publish(web, first, first_check).json()['replayed']
    assert publish(web, second, second_check).json()['replayed']
    assert sql(params, "SELECT version_key FROM valuation_evidence ORDER BY version_key") == versions_before
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        # This deliberate corruption is confined to a rolled-back disposable transaction.
        proof_cache = {}
        assert verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        cur.execute('SAVEPOINT item_corruption')
        cur.execute('ALTER TABLE source_items DISABLE TRIGGER USER')
        cur.execute("""UPDATE source_items SET parsed_fields=jsonb_set(parsed_fields,'{fields,market_value}', '"999"')
            WHERE source_item_id=%s""", (second['rows'][0]['row_id'],))
        cur.execute('ALTER TABLE source_items ENABLE TRIGGER USER')
        assert current_holding_source(cur, current['id']) == {'status':'unknown', 'source_item_id':None}
        assert not verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        assert verify_recorded_version(cur, version_key)
        cur.execute('ROLLBACK TO SAVEPOINT item_corruption')
        assert verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        conn.rollback()
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        from domain.imports.native_source import _replay
        proof_cache = {}
        assert verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        cur.execute('SAVEPOINT result_corruption')
        cur.execute('SELECT revision,published_source_hash FROM source_batches WHERE batch_id=%s', (second['batch_id'],))
        original = cur.fetchone()
        cur.execute('ALTER TABLE source_batches DISABLE TRIGGER USER')
        cur.execute("""UPDATE source_batches SET published_result=jsonb_set(published_result,
            '{records,0,after,market_value}', '"999"') WHERE batch_id=%s""", (second['batch_id'],))
        cur.execute('ALTER TABLE source_batches ENABLE TRIGGER USER')
        assert current_holding_source(cur, current['id']) == {'status':'unknown', 'source_item_id':None}
        assert not verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        with pytest.raises(ValueError, match='holding update source evidence'):
            _replay(cur, second['batch_id'], original['revision'], original['published_source_hash'])
        cur.execute('ROLLBACK TO SAVEPOINT result_corruption')
        assert verify_historical_holding_source(cur, holding_id=current['id'], source_item_id=second['rows'][0]['row_id'], snapshot_cache=proof_cache)
        conn.rollback()
    snapshots = sql(params, """SELECT evidence FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb)
        WHERE input_kind='holding' ORDER BY evidence->'snapshot'->'content'->>'quantity'""")
    contents = [row['evidence']['snapshot']['content'] for row in snapshots]
    assert any(str(row['quantity']) in {'100.000000', '100'} and str(row['market_value']) in {'1000.0000', '1000'} for row in contents)
    assert any(str(row['quantity']) in {'110.000000', '110'} and str(row['market_value']) in {'1100.0000', '1100'} for row in contents)
    new_item = second['rows'][0]['row_id']
    assert sql(params, """SELECT 1 FROM valuation_evidence v CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS x(input_kind text,input_hash text,evidence jsonb) WHERE input_kind='source_item'
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
        cur.execute('CREATE TRIGGER reject_new_version BEFORE INSERT ON valuation_evidence FOR EACH ROW EXECUTE FUNCTION reject_new_version()')
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
    assert not check['can_publish']
    scope = web.post(f"/api/account-import/batches/{confirmed['batch_id']}/confirm-scope", json={
        'revision':confirmed['revision'], 'as_of':DAY, 'member_count':2, 'cash_amount':'50',
        'note':'已核对退出成员、其他成员与现金', 'confirmed':True})
    assert scope.status_code == 200, scope.text
    confirmed = web.get(f"/api/account-import/batches/{confirmed['batch_id']}").json()
    check = preview(web, confirmed)
    assert check['can_publish'], check
    response = publish(web, confirmed, check)
    assert response.status_code == 200, response.text
    rows = sql(params, 'SELECT symbol,quantity,market_value,cost FROM pams_holdings_snapshot ORDER BY symbol')
    retired = next(row for row in rows if row['symbol'] == '000001')
    assert retired['quantity'] == retired['market_value'] == retired['cost'] == 0
    assert publish(web, confirmed, check).json()['replayed']


def test_current_source_must_describe_same_holding_and_values(isolated):
    from domain.investments.valuation_input_evidence import record_bound_holding_version
    web, params = isolated
    sql(params, """INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency,status)
                   VALUES ('TEST_STOCK_2','000002','Synthetic second','stock','CNY','active')""")
    batch = draft(web, [holding(), holding(symbol='000002')])
    check = preview(web, batch)
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    rows = sql(params, 'SELECT id,symbol FROM pams_holdings_snapshot ORDER BY symbol')
    first_id, second_id = rows[0]['id'], rows[1]['id']
    first_item, second_item = [r['row_id'] for r in batch['rows']]
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert current_holding_source(cur, first_id) == {'status': 'recorded', 'source_item_id': first_item}
        assert current_holding_source(cur, second_id) == {'status': 'recorded', 'source_item_id': second_item}
        with pytest.raises(ValueError, match='conflicts with holding'):
            record_bound_holding_version(cur, holding_id=first_id, calculation_version='wrong-symbol',
                input_refs=[{'kind':'source_item', 'key':{'source_item_id':second_item}, 'role':'current_source'}])
    corrected, _, _ = publish_holding(web, quantity='110', market_value='1100', cost='1000')
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ValueError, match='conflicts with holding'):
            record_bound_holding_version(cur, holding_id=first_id, calculation_version='old-values',
                input_refs=[{'kind':'source_item', 'key':{'source_item_id':first_item}, 'role':'current_source'}])
        assert current_holding_source(cur, first_id)['source_item_id'] == corrected['rows'][0]['row_id']


def test_current_source_resolves_a_unique_reviewed_asset_alias(isolated):
    web, params = isolated
    sql(params, "INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value) VALUES ('TEST_STOCK','symbol','SYN-ALIAS')")
    batch = draft(web, [holding(symbol='SYN-ALIAS')])
    check = preview(web, batch)
    assert check['can_publish'], check
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    row = sql(params, 'SELECT id,symbol FROM pams_holdings_snapshot')[0]
    assert row['symbol'] == '000001'
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert current_holding_source(cur, row['id']) == {'status':'recorded', 'source_item_id':batch['rows'][0]['row_id']}
