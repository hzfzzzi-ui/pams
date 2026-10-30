"""Legacy entry points cannot bypass the shared import approval."""
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/api'))
from endpoints import bank_reconcile, investment_transactions, holdings_trade


@pytest.mark.parametrize('module,name',[(bank_reconcile,'import_bank_reconcile'),
    (investment_transactions,'create_investment_transaction'),(holdings_trade,'upsert_trade')])
def test_legacy_import_never_connects_or_writes(module,name,monkeypatch):
    monkeypatch.setattr(module,'get_conn',lambda:pytest.fail('Legacy entry must not connect'))
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    with pytest.raises(HTTPException) as error:
        getattr(module,name)(None)
    assert error.value.status_code==409
    assert error.value.detail['code']=='import_review_required'


def test_existing_investment_and_bank_entry_only_open_the_shared_flow():
    trade=(ROOT/'frontend/public/assets/js/transactions.js').read_text(encoding='utf-8')
    bank=(ROOT/'frontend/public/assets/js/bank_reconcile.js').read_text(encoding='utf-8')
    assert "window.location.assign('account_import.html?kind=trade&mode=manual')" in trade
    assert "jpost('/api/investment-transactions', payload)" not in trade
    assert '"/api/bank-reconcile/import"' not in bank
    assert 'filePaths' not in bank
