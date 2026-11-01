"""Pending suggestions are evidence-aware and never automatic confirmations."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.ledger import bank_source_read, confirmed_transfers  # noqa: E402
from domain.ledger.ledger_view import _load_pending_transfer_cases  # noqa: E402


def test_pending_candidates_show_unmatched_unique_ambiguity_and_missing_evidence(monkeypatch):
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
