from datetime import date
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from domain.ledger.confirmed_transfers_v2 import _verified_leg


class Original:
    def verify(self, data):
        return True


def row(fields):
    raw = {'source': 'synthetic'}
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {'id': 1, 'account_code': 'fund', 'source_account': 'fund', 'currency': 'CNY',
            'amount': Decimal('-100'), 'day': date(2026, 1, 1), 'fact_type': 'fund_subscription',
            'symbol': '123456', 'source_item_id': 'item', 'original_source_id': 'source',
            'sha256': 'original', 'status': 'published', 'review_status': 'confirmed',
            'acquisition_method': 'screenshot', 'raw_fields': raw, 'record_hash': sha256(encoded).hexdigest(),
            'parsed_fields': {'business_date': '2026-01-01', 'net_cash': '-100', 'currency': 'CNY',
                              'symbol': '123456', **fields}}


@pytest.mark.parametrize('fields', [{'action': 'subscribe'}, {'trade_type': 'fund_subscription'}])
def test_subscription_source_alias_is_normalized(fields):
    leg = _verified_leg(row(fields), 'trade', Original(), hash_only=True)
    assert leg['purpose'] == 'fund_subscription_funding'
    assert Decimal(leg['amount']) == 100


def test_conflicting_action_is_not_accepted():
    with pytest.raises(ValueError):
        _verified_leg(row({'action': 'sell'}), 'trade', Original(), hash_only=True)
