from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates


def inputs():
    day = date(2026, 10, 30)
    return {'events': [{'trade_id': 1, 'account_code': 'test', 'symbol': '600001', 'currency': 'CNY',
                        'trade_date': day, 'trade_time': '2026-10-30T10:00:00', 'side': 'buy',
                        'quantity': '100', 'net_cash': '-1004', 'source_verified': True}],
            'identities': {'600001': {'asset_id': 'test-id', 'name': 'test stock', 'symbol': '600001'}},
            'quotes': {'600001': {'price': Decimal('12'), 'price_date': day,
                                  'price_time': datetime(2026, 10, 30, tzinfo=timezone.utc), 'source': 'tencent', 'symbol': 'sh600001'}},
            'as_of': day}


def test_previews_known_security_without_claiming_full_account():
    row = build_trade_snapshot_candidates(**inputs())[0]
    assert row['market_value'] == Decimal('1200')
    assert row['diluted_cost'] == Decimal('1004')
    assert row['remaining_cost'] == Decimal('1004')
    assert row['unrealized_pnl'] == row['total_pnl'] == Decimal('196')
    assert row['total_pnl_status'] == 'coverage_unverified'
    assert row['scope'] == 'securities_only'
    assert row['trade_ids'] == [1]


def test_refuses_unverified_trade_or_mismatched_quote_date():
    params = inputs()
    params['events'][0]['source_verified'] = False
    with pytest.raises(ValueError, match='source evidence'):
        build_trade_snapshot_candidates(**params)
    params = inputs()
    params['quotes']['600001']['price_date'] = date(2026, 10, 29)
    with pytest.raises(ValueError, match='same-date price'):
        build_trade_snapshot_candidates(**params)
    params = inputs()
    params['quotes']['600001']['symbol'] = 'sh600002'
    with pytest.raises(ValueError, match='same-date price'):
        build_trade_snapshot_candidates(**params)
