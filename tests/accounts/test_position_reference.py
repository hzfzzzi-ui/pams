from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[2] / 'backend' / 'api'
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.accounts.position_reference import read_historical_position_references


def test_only_verified_account_reference_populates_holdings_display():
    day = date(2026, 11, 15)
    cfg = {
        'account_type': 'fund_platform', 'truth_source': 'manual', 'reconcile_method': 'position_nav',
        'manual_balance': {'amount': '123.45', 'as_of': '2026-06-02'}, 'display_name': '基金账户',
    }
    raw = {'snapshot_date': '2026-06-02', 'account_code': 'fund', 'currency': 'CNY', 'known_historical_amount': '123.45'}
    fields = {'business_date': '2026-06-02', 'symbol': '110020', 'currency': 'CNY', 'market_value': '123.45'}
    row = {
        'raw_fields': raw, 'parsed_fields': fields, 'account_code': 'fund',
        'record_hash': sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'symbol': '110020', 'name': '基金名称', 'asset_type': 'fund',
    }

    class Cursor:
        def execute(self, *args):
            pass

        def fetchone(self):
            return {'enabled': True}

        def fetchall(self):
            return self.rows

    cur = Cursor()

    def read(rows):
        cur.rows = rows
        return read_historical_position_references(
            cur, account_config={'accounts': {'fund': cfg}}, as_of=day,
            excluded_accounts=set(), owner_ratio=lambda code: Decimal('1'),
        )

    assert read([row]) == [{
        'account_code': 'fund', 'account_name': '基金账户', 'symbol': '110020', 'name': '基金名称',
        'category': 'A股基金', 'currency': 'CNY', 'value': '123.45', 'as_of': '2026-06-02',
    }]
    assert read([dict(row, parsed_fields={**fields, 'market_value': '999.00'})]) == []
    assert read([dict(row, record_hash='modified')]) == []
    assert read([row, row]) == []
