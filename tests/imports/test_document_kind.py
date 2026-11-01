"""Category detection only uses synthetic source bytes and never writes data."""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports import document_kind
from endpoints.account_import import router
from tests.imports.test_account_import_ths import event, export


def test_native_trade_requires_matching_account_and_preserves_unknown():
    source = export(event())
    assert document_kind.detect(source, 'table.xls', 'tonghuashun') == {'kind': 'trade', 'status': 'detected'}
    assert document_kind.detect(source, 'table.xls', 'tencent_licaitong')['status'] == 'mismatch'
    assert document_kind.detect(b'\xd0\xcf\x11\xe0synthetic', 'table.xls', 'tonghuashun')['status'] == 'mismatch'
    assert document_kind.detect('代码,名称\n000001,合成\n'.encode(), 'unknown.csv', 'tonghuashun')['status'] == 'unknown'


def test_unambiguous_trade_and_holding_and_mixed_are_distinct():
    trade = '成交日期,证券代码,操作,成交金额\n20261001,000001,买入,10\n'.encode()
    holding = '持仓日期,证券代码,持仓数量,持仓市值\n20261001,000001,1,10\n'.encode()
    mixed = '成交日期,证券代码,操作,成交金额,持仓数量,持仓市值\n20261001,000001,买入,10,1,10\n'.encode()
    assert document_kind.detect(trade, 'trade.csv', 'tonghuashun')['kind'] == 'trade'
    assert document_kind.detect(holding, 'holding.csv', 'tonghuashun')['kind'] == 'holding'
    assert document_kind.detect(mixed, 'mixed.csv', 'tonghuashun') == {'kind': None, 'status': 'unknown'}


def test_image_needs_unambiguous_ocr_and_bank_uses_account_scope(monkeypatch):
    from domain.imports import account_import_ocr
    monkeypatch.setattr(document_kind.statement, 'is_statement', lambda account: account == 'bank')
    monkeypatch.setattr(account_import_ocr, 'parse_image', lambda *_: {'ocr_status': 'ok', 'text': '证券买入'})
    assert document_kind.detect(b'synthetic', 'image.png', 'tonghuashun')['kind'] == 'trade'
    monkeypatch.setattr(account_import_ocr, 'parse_image', lambda *_: {'ocr_status': 'ok', 'text': '证券买入 持仓市值'})
    assert document_kind.detect(b'synthetic', 'image.png', 'tonghuashun')['status'] == 'unknown'
    monkeypatch.setattr(account_import_ocr, 'parse_image', lambda *_: {'ocr_status': 'unrecognized', 'text': '持仓明细'})
    assert document_kind.detect(b'synthetic', 'image.png', 'tonghuashun')['kind'] == 'holding'
    monkeypatch.setattr(document_kind.statement, 'parse', lambda *_: {'rows': [{'fields': {'action': 'income'}}]})
    assert document_kind.detect(b'synthetic', 'statement.pdf', 'bank')['kind'] == 'cash'
    monkeypatch.setattr(document_kind.statement, 'parse', lambda *_: {'rows': [{'fields': {'action': 'buy'}}]})
    assert document_kind.detect(b'synthetic', 'statement.csv', 'bank')['status'] == 'mismatch'


def test_detect_endpoint_is_same_origin_and_returns_only_classification():
    app = FastAPI()
    app.include_router(router, prefix='/api')
    with TestClient(app) as web:
        url = '/api/account-import/detect?account_code=tonghuashun&filename=table.xls'
        assert web.post(url, content=export(event()), headers={'Origin': 'http://other.invalid'}).status_code == 403
        response = web.post(url, content=export(event()), headers={'Origin': 'http://testserver'})
        assert response.status_code == 200 and response.json() == {'kind': 'trade', 'status': 'detected'}
