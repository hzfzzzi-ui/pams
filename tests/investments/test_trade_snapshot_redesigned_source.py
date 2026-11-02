"""The production snapshot loader can consume the redesigned source reader."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.investments import trade_read_v2, trade_snapshot_source


class Cursor:
    class Connection:
        autocommit = False
    connection = Connection()

    def execute(self, sql, args=None):
        self.sql = sql

    def fetchone(self):
        if 'transaction_read_only' in self.sql:
            return {'ro': 'on', 'level': 'repeatable read'}
        if 'to_regclass' in self.sql:
            return {'redesigned': True}
        if 'pams_asset_master' in self.sql:
            return {'asset_id': 'synthetic', 'primary_symbol': '000001', 'canonical_name': 'Synthetic',
                    'asset_type': 'stock', 'status': 'active', 'currency': 'CNY'}
        raise AssertionError(self.sql)


def test_snapshot_loader_uses_verified_redesigned_trade(monkeypatch):
    monkeypatch.setattr(trade_snapshot_source, 'source_enabled', lambda cur: True)
    trade = {'trade_id': 1, 'account_code': 'tonghuashun', 'symbol': '000001', 'currency': 'CNY',
             'trade_type': 'buy', 'side': 'buy', 'asset_id': 'synthetic', 'source_evidence_status': 'verified'}
    monkeypatch.setattr(trade_read_v2, 'load_trade_rows', lambda *args, **kwargs: [dict(trade)])
    events, identities = trade_snapshot_source.load_verified_equity_events(Cursor(), 'tonghuashun')
    assert events[0]['source_verified'] is True and identities['000001']['asset_id'] == 'synthetic'
    trade['source_evidence_status'] = 'unknown'
    with pytest.raises(ValueError, match='verification failed'):
        trade_snapshot_source.load_verified_equity_events(Cursor(), 'tonghuashun')
