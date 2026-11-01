"""Synthetic source-only identity checks; no production data or database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports.ths_asset_candidates import collect_candidates, initialize


def source(symbol='000001', name='合成证券', market='深圳Ａ股', day='2026-01-02', row=2):
    return {'fields': {'symbol': symbol, 'name': name, 'business_date': day},
            'source': {'profile': 'ths_broker_ledger_v1', 'market': market, 'row': row}}


def test_same_identity_keeps_original_names_dates_and_rows():
    parsed = {'rows': [source(name='XD合成', day='2026-01-03'), source(row=3),
                       source(symbol='CASH_CNY'), source(symbol='000002', market='未知')]}
    assets, conflicts = collect_candidates(parsed)
    assert conflicts == [{'symbol': '000002', 'reason': 'source_incomplete'}] and len(assets) == 1
    assert assets[0]['name'] == '合成证券' and assets[0]['exchange'] == 'SZSE'
    assert [r['name'] for r in assets[0]['source_records']] == ['XD合成', '合成证券']


def test_same_code_with_two_markets_or_conflicting_names_is_not_auto_initialized():
    assets, conflicts = collect_candidates({'rows': [source(), source(market='上海Ａ股')]})
    assert not assets and conflicts == [{'symbol': '000001', 'reason': 'identity_ambiguous'}]
    assets, conflicts = collect_candidates({'rows': [source(), source(name='另一标的', row=3)]})
    assert not assets and len(conflicts) == 1


def test_initialization_does_not_overwrite_existing_and_keeps_source_evidence():
    class Cursor:
        def __init__(self): self.queries = []; self.matches = []
        def execute(self, sql, params=None):
            self.queries.append((sql, params))
            if 'SELECT DISTINCT m.asset_id' in sql:
                self.matches = [{'asset_id': 'old'}] if params[1][0] == '000001' else []
        def fetchall(self): return self.matches

    cur = Cursor()
    outcome = initialize(cur, {'rows': [source(), source(symbol='000002')]},
                         batch_id='batch', file_id='file', file_sha256='a' * 64)
    assert outcome['created'] == ['000002']
    writes = [params for sql, params in cur.queries if 'INSERT INTO pams_asset_master' in sql]
    assert len(writes) == 1 and writes[0][1] == '000002'
    evidence = writes[0][-1].adapted
    assert evidence['source_file_sha256'] == 'a' * 64
    assert evidence['source_records'][0]['date'] == '2026-01-02'
    assert evidence['pending_fields'] == ['asset_type', 'currency']
