"""A dated historical observation must not become a later balance."""

from datetime import date
from decimal import Decimal
import os
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor
import pytest

from domain.imports.isolation import source_enabled
from domain.investments.position_nav_projection import load_position_nav_estimates


def test_point_in_time_partial_is_not_carried_forward():
    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    day, later = date(2025, 12, 31), date(2026, 1, 1)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert source_enabled(cur), 'database isolation marker is required'
                accounts = ['example_historical', 'example_ordinary']
                historical_id = None
                for code in accounts:
                    cur.execute('INSERT INTO pams_accounts(account_code,display_name) VALUES (%s,%s)', (code, code))
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + code,))
                    cur.execute('''INSERT INTO pams_holdings_snapshot
                        (account_code,as_of,symbol,name,currency,asset_liability,major_category,minor_category,market_value)
                        VALUES (%s,%s,%s,%s,'CNY','资产','其他','其他',%s) RETURNING id''',
                        (code, day, code, code, Decimal('100') if code == accounts[0] else Decimal('25')))
                    ident = cur.fetchone()['id']
                    if code == accounts[0]:
                        historical_id = ident
                        version = str(uuid4())
                        cur.execute('''INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash)
                            SELECT %s,id,md5(to_jsonb(h)::text) FROM pams_holdings_snapshot h WHERE id=%s''', (version, ident))
                        cur.execute('''INSERT INTO account_snapshot_version
                            (version_id,account_code,as_of,coverage_status,cash_status,scope_attestation,calculation_version,reason)
                            VALUES (%s,%s,%s,'partial','unknown',%s,'test-point-v1','synthetic evidence')''',
                            (version, code, day, Json({'point_in_time_only': True})))
                        later_version = str(uuid4())
                        cur.execute('''INSERT INTO account_snapshot_member(version_id,holding_id,holding_row_hash)
                            SELECT %s,id,md5(to_jsonb(h)::text) FROM pams_holdings_snapshot h WHERE id=%s''', (later_version, ident))
                        cur.execute('''INSERT INTO account_snapshot_version
                            (version_id,account_code,as_of,coverage_status,cash_status,calculation_version,reason)
                            VALUES (%s,%s,%s,'partial','unknown','test-later-v1','later review')''',
                            (later_version, code, day))
                estimates = load_position_nav_estimates(
                    cur, accounts={code: {} for code in accounts}, dates=[day, later],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []),
                    account_owner_ratio=lambda _: Decimal('1'),
                    rate_at=lambda currency, _: Decimal('1') if currency == 'CNY' else None,
                )
                assert estimates[day][accounts[0]][0] == Decimal('100')
                assert accounts[0] not in estimates.get(later, {})
                assert estimates[day][accounts[1]][0] == Decimal('25')
                assert estimates[later][accounts[1]][0] == Decimal('25')
                # A modified row invalidates its immutable membership proof;
                # it must not leak into either the source day or later days.
                cur.execute('UPDATE pams_holdings_snapshot SET market_value=999 WHERE id=%s', (historical_id,))
                invalid = load_position_nav_estimates(
                    cur, accounts={code: {} for code in accounts}, dates=[day, later],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []),
                    account_owner_ratio=lambda _: Decimal('1'),
                    rate_at=lambda currency, _: Decimal('1') if currency == 'CNY' else None,
                )
                assert all(accounts[0] not in invalid.get(d, {}) for d in (day, later))
                assert invalid[later][accounts[1]][0] == Decimal('25')
                # Zero-valued, closed positions can be priced without FX, but
                # they cannot replace the other holdings in the same account.
                cur.execute("""INSERT INTO pams_holdings_snapshot
                    (account_code,as_of,symbol,name,currency,asset_liability,major_category,minor_category,market_value)
                    VALUES (%s,%s,'FOREIGN','Foreign','USD','资产','其他','其他',15)""",(accounts[1],day))
                cur.execute("UPDATE pams_holdings_snapshot SET market_value=0 WHERE account_code=%s AND symbol=%s",
                            (accounts[1],accounts[1]))
                def foreign(rate):
                    return load_position_nav_estimates(cur, accounts={accounts[1]: {}}, dates=[day],
                        owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: Decimal(1),
                        rate_at=lambda currency, _: Decimal(1) if currency=='CNY' else rate)
                assert accounts[1] not in foreign(None).get(day,{})
                assert foreign(Decimal(7))[day][accounts[1]][0]==105
        finally:
            conn.rollback()

def test_conflicting_backcast_stops_until_next_reconciled_point_or_anchor():
    from decimal import Decimal as D
    from datetime import date, timedelta
    from domain.investments.valuation_nav import _apply_historical_cash_points
    start = date(2025, 12, 31)
    dates = [start + timedelta(days=n) for n in range(5)]
    account = {'account_code':'wallet', 'base_currency':'CNY', 'primary_balance':{'as_of':dates[-1]}}
    def point(value):
        return {'currency':'CNY', 'asset_liability':'资产', 'market_value':D(value)}
    points = {('wallet', dates[0]): point(0), ('wallet', dates[2]):point(300)}
    buckets = {day: {'net_worth':D(310), 'assets':D(310), 'liabilities':D(0),
                     'account_values':{'wallet':D(300),'other':D(10)},
                     'available_accounts':['wallet','other'], 'estimated_accounts':['wallet']} for day in dates}
    _apply_historical_cash_points(None, bank_series=buckets, accounts=[account], dates=dates,
                                 rate_at=lambda *_: D(1), points=points, periods={})
    assert [buckets[d]['net_worth'] for d in dates] == [10,10,310,310,310]
    assert 'wallet' not in buckets[dates[1]]['available_accounts']
    assert 'wallet' in buckets[dates[1]]['unavailable_accounts']
    assert buckets[dates[0]]['account_values']['wallet'] == 0
