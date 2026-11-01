"""Unified source review, exact writes and statement isolation contracts."""
from copy import deepcopy
from decimal import Decimal

import pytest

from tests.imports.test_account_import_publish import client as import_client, draft, trade, preview, publish, sql, native_batch, DAY  # noqa: F401
from domain.imports import account_import_statement as statement
from domain.imports import account_import_store as store


@pytest.fixture
def client(request):
    return request.getfixturevalue("import_client")


@pytest.fixture
def bill_client(client, monkeypatch):
    monkeypatch.setattr(statement, "config", lambda: {"profiles": {"test": {"account_code": "bank_test", "profile_code": "test",
        "statement_template": "manual_statement_csv", "currency": "CNY"}}, "rules": [], "amount_rules": []})
    return client


def test_file_values_cannot_be_edited_deleted_or_spoofed(client):
    web, _ = client
    batch, _ = native_batch(web)
    path = f"/api/account-import/batches/{batch['batch_id']}"
    rows = deepcopy(batch["rows"])
    rows[0]["fields"]["gross_amount"] = "1"
    assert web.put(path, json={"revision": batch["revision"], "rows": rows}).json()["detail"]["code"] == "file_row_readonly"
    assert web.put(path, json={"revision": batch["revision"], "rows": batch["rows"][1:]}).status_code == 400
    rows = deepcopy(batch["rows"])
    rows[0]["excluded"] = True
    assert web.put(path, json={"revision": batch["revision"], "rows": rows}).status_code == 200


def test_write_plan_contains_real_fields_and_all_funding_legs(client):
    web, _ = client
    fields = {"kind": "cash", "business_date": DAY, "currency": "CNY", "action": "deposit", "net_cash": "500", "counter_account_code": "bank_test"}
    batch = draft(web, [fields])
    check = preview(web, batch)
    assert check["source_row_count"] == 1 and check["write_count"] == 2
    bank, broker = check["write_plan"]
    assert bank["table"] == "pams_cashflow_fact" and broker["table"] == "pams_investment_trade_fact"
    assert Decimal(bank["after"]["amount"]) == -Decimal(broker["after"]["net_cash"])
    assert broker["links"][0]["field"] == "cashflow_id"
    result = publish(web, batch, check).json()
    assert len(result["records"]) == check["write_count"]


def test_statement_preview_is_readonly_and_balance_is_independent(bill_client, monkeypatch):
    web, params = bill_client
    monkeypatch.delenv("PAMS_IMPORT_PUBLISH_ENABLED")
    batch = draft(web, [{"kind": "cash", "business_date": DAY, "currency": "CNY", "asset_type": "cash", "action": "expense", "net_cash": "-25.10", "note": "午餐"}], account="bank_test")
    check = preview(web, batch)
    assert check["can_publish"] and check["write_count"] == 1
    assert check["write_plan"][0]["table"] == "pams_cashflow_fact"
    assert check["write_plan"][0]["after"]["amount"] == "-25.10"
    assert publish(web, batch, check).status_code == 403
    for table in ("pams_cashflow_fact", "pams_investment_trade_fact", "pams_holdings_snapshot"):
        assert not sql(params, "SELECT * FROM " + table)


def test_statement_transaction_and_balance_only_selected_rows_publish(bill_client):
    web, params = bill_client
    batch = web.post('/api/account-import/batches', json={"account_code":"bank_test", "mode":"upload", "kind":"cash"}).json()
    content = f"transaction_date,amount,currency,balance,summary\n{DAY},100.05,CNY,200.05,工资\n".encode()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    batch = web.post(path+'/files?filename=bank.csv', content=content).json()
    assert len(batch['rows']) == 2 and batch['rows'][1]['excluded']
    assert not preview(web,batch)['can_publish']
    reviewed = web.put(path, json={'revision':batch['revision'], 'rows':batch['rows']})
    assert reviewed.status_code == 200, reviewed.text
    batch = reviewed.json()
    check = preview(web,batch)
    assert check['can_publish'] and check['write_count'] == 1
    assert publish(web,batch,check).status_code == 200
    assert len(sql(params,'SELECT * FROM pams_cashflow_fact')) == 1
    assert not sql(params,'SELECT * FROM pams_holdings_snapshot')


def test_image_review_is_per_file_and_invalidated_by_edits(client, monkeypatch):
    web, params = client
    monkeypatch.setattr(store, "parse", lambda *args: {"origin":"ocr", "rows":[{"fields":trade(), "source":{"row":1}, "issues":[]}]})
    batch = web.post('/api/account-import/batches',json={"account_code":"tonghuashun","mode":"upload","kind":"trade"}).json()
    path = f"/api/account-import/batches/{batch['batch_id']}"
    batch = web.post(path+'/files?filename=one.png',content=b'test image one').json()
    file = batch['files'][0]['file_id']
    assert not preview(web,batch)['can_publish']
    batch = web.post(path+f'/files/{file}/review',json={"revision":batch['revision']}).json()
    assert preview(web,batch)['can_publish']
    rows=deepcopy(batch['rows']); rows[0]['fields']['note']='corrected'; rows[0]['acknowledged']=True
    batch=web.put(path,json={"revision":batch['revision'],"rows":rows}).json()
    assert not preview(web,batch)['can_publish']
    assert not sql(params,'SELECT * FROM pams_investment_trade_fact')


def test_liability_balance_is_never_recast_as_positive_asset(bill_client, monkeypatch):
    web, params = bill_client
    monkeypatch.setattr(statement, "config", lambda: {"profiles":{"card":{"account_code":"bank_test","account_kind":"credit_card"}}})
    batch=draft(web,[{"kind":"cash","business_date":DAY,"currency":"CNY","action":"balance","net_cash":"100"}],account="bank_test")
    check=preview(web,batch)
    assert not check['can_publish'] and check['write_count']==0
    assert any(i['code']=='liability_balance_unsupported' for row in check['rows'] for i in row['issues'])
    assert not sql(params,'SELECT * FROM pams_holdings_snapshot')


def test_statement_config_is_loaded_once_per_preflight(bill_client, monkeypatch):
    web, _ = bill_client
    batch=draft(web,[{"kind":"cash","business_date":DAY,"currency":"CNY","action":"expense","net_cash":"-1"}],account="bank_test")
    original=statement.config
    calls=[]
    def load():
        calls.append(True)
        return original()
    monkeypatch.setattr(statement,"config",load)
    assert preview(web,batch)['can_publish']
    assert len(calls)==1


def test_missing_ocr_rows_can_be_added_only_under_owned_image(client, monkeypatch):
    web, params=client
    monkeypatch.setattr(store,'parse',lambda *args:{'origin':'ocr','rows':[]})
    batch=web.post('/api/account-import/batches',json={'account_code':'tonghuashun','mode':'upload','kind':'trade'}).json()
    path=f"/api/account-import/batches/{batch['batch_id']}"
    batch=web.post(path+'/files?filename=missing.png',content=b'image').json()
    file_id=batch['files'][0]['file_id']
    assert web.post(path+f'/files/{file_id}/review',json={'revision':batch['revision']}).status_code==400
    batch=web.put(path,json={'revision':batch['revision'],'rows':[{'fields':trade(),'image_file_id':file_id}]}).json()
    assert batch['rows'][0]['source']['origin']=='ocr' and batch['rows'][0]['source']['human_added']
    assert not preview(web,batch)['can_publish']
    batch=web.post(path+f'/files/{file_id}/review',json={'revision':batch['revision']}).json()
    assert preview(web,batch)['can_publish']
    assert not sql(params,'SELECT * FROM pams_investment_trade_fact')
