"""Explicit fee metadata must not recalculate or silently backfill existing money."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.brokers.ibkr.ibkr_fee_currency import currency_code, explicit_ibkr_fee_currency, fact_currency_check  # noqa: E402
from domain.brokers.ibkr.ibkr_fee_review import review_fees  # noqa: E402
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts  # noqa: E402
from tests.brokers.ibkr.test_ibkr_fee_review import make_case, complete_fee_case, XML  # noqa: E402
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows  # noqa: E402

MIGRATION = ROOT / 'database/postgres/schema/071_investment_trade_fee_currency.sql'


@pytest.mark.parametrize('value,expected', [('USD', 'USD'), (' usd ', 'USD'), ('JPY', 'JPY'),
    ('', None), (None, None), (True, None), (123, None), ('US', None), ('USDD', None),
    ('12D', None), ('\uff35\uff33\uff24', None), ('u\u017fd', None), ('\u00dfD', None)])
def test_currency_codes_are_explicit_ascii_units(value, expected):
    assert currency_code(value) == expected


@pytest.mark.parametrize('commission,fee', [('-2', '2'), ('2', '2'), ('0', '0'), ('-0.0000000001', '0.0000000001')])
def test_signed_and_explicit_zero_fees_keep_their_unit_without_conversion(commission, fee):
    row = {'source_table': 'pams_ibkr_trades', 'currency': 'MXN',
           'raw_json': {'ibCommissionCurrency': 'USD', 'ibCommission': commission}}
    before = deepcopy(row)
    assert explicit_ibkr_fee_currency(row, Decimal(fee)) == 'USD' and row == before


@pytest.mark.parametrize('change', ['missing_unit', 'missing_amount', 'null', 'invalid_amount', 'nan',
    'infinite', 'boolean', 'conflicting_unit', 'duplicate_amount', 'wrong_source', 'wrong_amount', 'invalid_unit'])
def test_missing_or_conflicting_raw_evidence_is_not_guessed(change):
    row = {'source_table': 'pams_ibkr_trades', 'currency': 'USD',
           'raw_json': {'ibCommissionCurrency': 'USD', 'ibCommission': '-2'}}
    raw = row['raw_json']
    if change == 'missing_unit': raw.pop('ibCommissionCurrency')
    elif change == 'missing_amount': raw.pop('ibCommission')
    elif change == 'null': raw['ibCommission'] = None
    elif change == 'invalid_amount': raw['ibCommission'] = 'bad'
    elif change == 'nan': raw['ibCommission'] = 'NaN'
    elif change == 'infinite': raw['ibCommission'] = 'Infinity'
    elif change == 'boolean': raw['ibCommission'] = False
    elif change == 'conflicting_unit': raw['IBCOMMISSIONCURRENCY'] = 'JPY'
    elif change == 'duplicate_amount': raw['IBCOMMISSION'] = '-2'
    elif change == 'wrong_source': row['source_table'] = 'manual'
    elif change == 'invalid_unit': raw['ibCommissionCurrency'] = 'USDD'
    else: raw['ibCommission'] = '-3'
    assert explicit_ibkr_fee_currency(row, Decimal('2')) is None


def test_normalization_preserves_all_old_fields_and_immutable_raw_source():
    case = make_case()
    row = deepcopy(case['facts'][0]['raw_json']['source_row'])
    row['trade_date'] = case['facts'][0]['trade_date']
    full = build_investment_trade_fact(row)
    assert full['fee_currency'] == 'USD' and full['currency'] == 'MXN'
    assert full['fee_amount'] == Decimal('2') and full['net_cash'] == 0 and full['net_cash_cny'] == 0
    # The entire old projection remains identical, including source hash, flags and raw JSON.
    expected = {k: v for k, v in case['facts'][0].items() if k != 'trade_id'}
    assert {k: v for k, v in full.items() if k != 'fee_currency'} == expected


@pytest.mark.parametrize('stored,verified,status', [(None, True, 'missing'), ('USD', True, 'matched'),
    ('HKD', True, 'mismatch'), ('usd', True, 'invalid'), ('', True, 'invalid'),
    ('USD', False, 'unverified')])
def test_persisted_currency_never_implies_cash_settlement(stored, verified, status):
    result = fact_currency_check({'fee_currency': stored}, 'USD', source_verified=verified)
    assert result['status'] == status and result['column_available']
    assert not result['cash_settlement_verified'] and not result['additional_deduction_proposed']
    assert fact_currency_check({}, 'USD', source_verified=True)['status'] == 'column_unavailable'


@pytest.mark.parametrize('stored,status', [(None, 'missing'), ('USD', 'matched'), ('HKD', 'mismatch'), ('invalid', 'invalid')])
def test_fee_audit_separates_scalar_currency_from_source_unit_and_cash(stored, status):
    case = complete_fee_case()
    baseline = review_fees(**case)
    case['facts'][0]['fee_currency'] = stored
    result = review_fees(**case)
    item = result['records'][0]
    unit = item['unit_review']
    assert unit['status'] == 'source_unit_verified'  # Source evidence is unchanged, even if stored unit conflicts.
    assert unit['fact_currency_check']['status'] == status
    assert unit['fact_fee_currency_persisted'] == (stored is not None)
    assert unit['individual_cash_settlement'] == 'unverified' and not unit['additional_deduction_proposed']
    assert result['status'] == 'review' and result['existing_fee_and_lineage_blockers_preserved']
    assert item['checks'] == baseline['records'][0]['checks']
    assert item['impact'] == baseline['records'][0]['impact']
    assert result['summary']['signed_official_fees_by_currency'] == baseline['summary']['signed_official_fees_by_currency']
    assert result['summary']['fact_fee_currency_matched_rows'] == (status == 'matched')
    if status == 'matched':
        assert item['stored_fee_numeric_currency_is_explicit']
        assert 'individual_cash_settlement_unverified' in item['issues']


def migrate(conn):
    with conn, conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding='utf-8'))


def test_real_migration_is_nullable_additive_idempotent_and_does_not_apply_070(sync_case, database):
    before = business_state(database)
    conn = database()
    try:
        migrate(conn)
        migrate(conn)
        after = business_state(database)
        for row in after['pams_investment_trade_fact']:
            assert row.pop('fee_currency') is None
        assert after == before
        with conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pams_ibkr_source_link_repair_audit')")
            assert cur.fetchone()[0] is None
        for bad in ('usd', '', 'US', '12D'):
            with pytest.raises(psycopg2.errors.CheckViolation):
                with conn, conn.cursor() as cur:
                    cur.execute('UPDATE pams_investment_trade_fact SET fee_currency=%s', (bad,))
        assert business_state(database) == {**before, 'pams_investment_trade_fact':
            [{**row, 'fee_currency': None} for row in before['pams_investment_trade_fact']]}
    finally:
        conn.close()


@pytest.mark.parametrize('migrated', [False, True])
def test_real_new_sync_persists_unit_only_when_schema_exists_and_replay_never_backfills(
    sync_case, database, migrated, caplog,
):
    client, _, report = sync_case
    conn = database()
    try:
        if migrated: migrate(conn)
        before = rows(database, 'pams_investment_trade_fact')
        report['xml'] = report['xml'].replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibCommissionCurrency="USD"')
        assert client.post('/api/ibkr/flex/sync', json={}).json()['success']
        after = rows(database, 'pams_investment_trade_fact')
        added = [row for row in after if row['trade_id'] not in {r['trade_id'] for r in before}]
        trade = next(row for row in added if row['source_table'] == 'pams_ibkr_trades')
        assert trade.get('fee_currency') == ('USD' if migrated else None)
        assert (trade['fee_amount'], trade['net_cash'], trade['net_cash_cny']) == (1.25, -341.25, -2388.75)
        for row in before:
            assert row in after  # Neither old metadata nor updated_at changes.
        if not migrated:
            assert 'fee_currency column unavailable' in caplog.text
            migrate(conn)
            after = rows(database, 'pams_investment_trade_fact')
        assert all(row['fee_currency'] is None for row in after if row['trade_id'] in {r['trade_id'] for r in before})
        assert client.post('/api/ibkr/flex/sync', json={}).json()['success']
        assert rows(database, 'pams_investment_trade_fact') == after
    finally:
        conn.close()


@pytest.mark.parametrize('raw_change', ['missing_currency', 'missing_commission', 'zero', 'cross_currency'])
def test_real_insert_covers_unknown_zero_cross_currency_and_does_not_update_existing(sync_case, database, raw_change):
    case = make_case()
    row = deepcopy(case['facts'][0]['raw_json']['source_row'])
    row['source_id'] = 'new-fee-test'
    row['source_key'] = 'ibkr:new-fee-test'
    if raw_change == 'missing_currency': row['raw_json'].pop('ibCommissionCurrency')
    elif raw_change == 'missing_commission': row['raw_json'].pop('ibCommission')
    elif raw_change == 'zero': row['raw_json']['ibCommission'] = '0'; row['fee_amount'] = 0
    expected = None if raw_change.startswith('missing') else 'USD'
    fact = build_investment_trade_fact(row)
    conn = database()
    try:
        migrate(conn)
        with conn, conn.cursor() as cur:
            assert insert_investment_trade_facts(cur, [fact]) == 1
        inserted = rows(database, 'pams_investment_trade_fact')
        found = next(r for r in inserted if r['source_id'] == 'new-fee-test')
        assert found['fee_currency'] == expected and found['currency'] == 'MXN'
        assert found['net_cash'] == 0 and found['net_cash_cny'] == 0
        with conn, conn.cursor() as cur:
            assert insert_investment_trade_facts(cur, [{**fact, 'fee_currency': 'JPY'}]) == 0
        assert rows(database, 'pams_investment_trade_fact') == inserted
    finally:
        conn.close()


def test_real_late_failure_rolls_back_entire_metadata_insert(sync_case, database):
    case = make_case()
    row = {**case['facts'][0]['raw_json']['source_row'], 'source_id': 'failed', 'source_key': 'ibkr:failed'}
    fact = build_investment_trade_fact(row)
    conn = database()
    try:
        migrate(conn)
        before = business_state(database)
        with pytest.raises(psycopg2.errors.DivisionByZero):
            with conn, conn.cursor() as cur:
                assert insert_investment_trade_facts(cur, [fact]) == 1
                cur.execute('SELECT 1/0')
        assert business_state(database) == before
    finally:
        conn.close()


def test_real_currency_audit_is_server_readonly_after_migration(sync_case, database):
    from domain.brokers.ibkr.ibkr_reconciliation import read_ibkr_reconciliation
    client, _, report = sync_case
    report['xml'] = XML
    conn = database()
    try:
        migrate(conn)
    finally:
        conn.close()
    assert client.post('/api/ibkr/flex/sync', json={}).json()['success']
    before = business_state(database)

    class ReadOnlyConnection(connection):
        def set_session(self, *args, **kwargs):
            assert kwargs.get('readonly') and kwargs.get('isolation_level') == 'REPEATABLE READ'
            return super().set_session(*args, **kwargs)

    result = read_ibkr_reconciliation(connection_factory=lambda: database(connection_factory=ReadOnlyConnection))
    assert result['fee_review']['summary']['fact_fee_currency_matched_rows'] == 1
    assert result['fee_review']['records'][0]['unit_review']['individual_cash_settlement'] == 'unverified'
    assert business_state(database) == before
