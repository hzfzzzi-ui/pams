"""Pending suggestions are evidence-aware and never automatic confirmations."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.ledger import bank_source_read, confirmed_transfers, confirmed_transfers_v2, ledger_view  # noqa: E402
from domain.ledger.ledger_view import _load_pending_transfer_cases  # noqa: E402


def test_pending_candidates_show_unmatched_unique_ambiguity_and_missing_evidence(monkeypatch):
    monkeypatch.setattr(ledger_view, '_table_exists', lambda cur, name: False)
    day = date(2026, 10, 22)
    rows = []
    monkeypatch.setattr(confirmed_transfers, 'read', lambda cur, limit=None: [])
    monkeypatch.setattr(bank_source_read, 'rows', lambda cur, since, limit: rows)
    def fact(ident, account, amount, status='verified'):
        return {'cashflow_id':ident, 'account_code':account, 'amount':Decimal(amount),
                'currency':'CNY', 'event_date':day, 'cashflow_type':'internal_transfer',
                'counter_account_code':None, 'source_evidence_status':status}
    def pending():
        return _load_pending_transfer_cases(None, start=day, end=day)
    assert pending() == []
    rows.append(fact(1,'a','-10'))
    assert pending()[0]['status'] == 'unmatched'
    rows.append(fact(2,'b','10'))
    assert {item['status'] for item in pending()} == {'unique_candidate'}
    rows.append(fact(3,'c','10'))
    assert {item['status'] for item in pending()} == {'ambiguous'}
    assert pending()[0]['candidate_count'] == 2
    rows.append(fact(4,'d','-10','unknown'))
    unknown=next(item for item in pending() if item['case_id']=='cashflow:4')
    assert unknown['status']=='source_unknown' and unknown['amount'] is None and unknown['candidate_count'] is None
    assert all(item['total_count']==4 for item in pending())


def test_new_source_schema_keeps_missing_evidence_unmatched(monkeypatch):
    day = date(2026, 10, 22)
    monkeypatch.setattr(ledger_view, '_table_exists', lambda cur, name: True)
    monkeypatch.setattr(ledger_view, '_confirmed_transfer_records', lambda *args, **kwargs: [])
    monkeypatch.setattr(confirmed_transfers_v2, 'occupied_cashflow_ids', lambda cur: set())
    rows = [{'id': 1, 'account_code': 'a', 'amount': Decimal('-10'), 'currency': 'CNY',
             'transaction_date': day, 'cashflow_type': 'internal_transfer',
             'classification_status': 'confirmed', 'source_evidence_status': 'verified'},
            {'id': 2, 'account_code': 'b', 'amount': Decimal('10'), 'currency': 'CNY',
             'transaction_date': day, 'cashflow_type': 'internal_transfer',
             'classification_status': 'confirmed', 'source_evidence_status': 'unknown'}]
    monkeypatch.setattr(ledger_view, '_load_cash_rows', lambda *args, **kwargs: rows)
    cases = _load_pending_transfer_cases(None, start=day, end=day)
    assert cases[0]['status'] == 'unmatched' and cases[0]['amount'] == -10
    assert cases[1]['status'] == 'source_unknown' and cases[1]['amount'] is None
    assert cases[0]['candidate_count'] == 0 and cases[0]['candidate_ids'] == []
    assert cases[1]['candidate_count'] is None and cases[1]['candidate_ids'] == []


def test_new_source_schema_suggests_only_verified_unique_or_ambiguous_pairs(monkeypatch):
    day = date(2026, 10, 22)
    monkeypatch.setattr(ledger_view, '_table_exists', lambda cur, name: True)
    monkeypatch.setattr(ledger_view, '_confirmed_transfer_records', lambda *args, **kwargs: [])
    monkeypatch.setattr(confirmed_transfers_v2, 'occupied_cashflow_ids', lambda cur: set())
    def fact(ident, account, amount):
        evidence = {'account_code': account, 'amount': Decimal(amount), 'currency': 'CNY',
                    'source_item_id': ident, 'source_id': ident, 'source_hash': None}
        return {'id': ident, **evidence, 'pair_evidence': evidence,
                'transaction_date': day, 'cashflow_type': 'internal_transfer',
                'classification_status': 'confirmed', 'source_evidence_status': 'verified'}
    rows = [fact(1, 'a', '-10'), fact(2, 'b', '10')]
    monkeypatch.setattr(ledger_view, '_load_cash_rows', lambda *args, **kwargs: rows)
    cases = _load_pending_transfer_cases(None, start=day, end=day)
    assert [case['status'] for case in cases] == ['unique_candidate', 'unique_candidate']
    assert cases[0]['candidate_ids'] == [2] and cases[1]['candidate_ids'] == [1]
    rows.append(fact(3, 'c', '10'))
    cases = _load_pending_transfer_cases(None, start=day, end=day)
    assert [case['status'] for case in cases] == ['ambiguous', 'ambiguous', 'ambiguous']
    assert cases[0]['candidate_count'] == 2
    rows[2]['source_evidence_status'] = 'unknown'
    cases = _load_pending_transfer_cases(None, start=day, end=day)
    assert [case['status'] for case in cases] == ['unique_candidate', 'unique_candidate', 'source_unknown']
    assert cases[2]['candidate_count'] is None and cases[2]['amount'] is None
