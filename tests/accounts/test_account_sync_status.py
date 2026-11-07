from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.accounts import sync_status


def test_verified_cashflow_does_not_initialize_balance():
    imported = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    day = date(2026, 11, 1)
    cashflow = sync_status._activity([
        {'source_evidence_status': 'verified', 'transaction_date': day, 'created_at': imported},
        {'source_evidence_status': 'unknown', 'transaction_date': date(2026, 11, 2)},
    ], 'transaction_date')
    balance = sync_status._balance(None, [], {'manual_balance': {'as_of': '2026-06-21'}})
    assert cashflow == {'status': 'recorded', 'count': 1, 'as_of': day, 'synced_at': imported,
                        'has_unverified': True}
    assert balance == {'status': 'uninitialized', 'as_of': None, 'synced_at': None,
                       'historical_as_of': '2026-06-21'}


def test_balance_requires_one_verified_member(monkeypatch):
    day = date(2026, 11, 1)
    published = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    row = {'id': 1, 'source_item_id': 'source-item', 'as_of': day, 'published_at': published,
           'market_value': Decimal('18971.58')}
    monkeypatch.setattr(sync_status, '_complete_member_proven', lambda *args: False)
    assert sync_status._balance(None, [row], {})['status'] == 'review'
    monkeypatch.setattr(sync_status, '_complete_member_proven', lambda *args: True)
    assert sync_status._balance(None, [row], {}) == {
        'status': 'recorded', 'as_of': day, 'amount': '18971.58', 'synced_at': published, 'historical_as_of': None,
    }
    assert sync_status._balance(None, [row, dict(row, id=2)], {})['status'] == 'review'
