"""Staging, private evidence and access checks; no production writes."""
from io import BytesIO
from pathlib import Path
import sys
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/api"))
from endpoints import account_import as api  # noqa: E402
from domain.imports import account_import_store as store  # noqa: E402
from tests.support.account_import_support import scratch_database  # noqa: E402

BASE = "/api/account-import"


def test_import_context_groups_supported_subaccounts_without_changing_codes(monkeypatch):
    from domain.imports import account_import_statement

    cur = MagicMock()
    cur.fetchall.return_value = [
        {"account_code": code, "display_name": label, "base_currency": currency}
        for code, label, currency in (
            ("ccb_7332", "7332", "CNY"), ("ccb_credit_0453", "0453", "CNY"),
            ("santander", "Santander", "MXN"), ("wechat", "WeChat", "CNY"),
            ("tencent_licaitong", "Tencent Licaitong", "CNY"),
            ("wechat_balance", "微信零钱", "CNY"),
        )
    ]
    monkeypatch.setattr(account_import_statement, "profiles",
                        lambda: dict.fromkeys(("ccb_7332", "ccb_credit_0453", "santander", "wechat")))

    payload = store.context(cur)
    choices = {row["account_code"]: row for row in payload["accounts"]}
    assert set(choices) == {"ccb_7332", "ccb_credit_0453", "santander", "wechat", "tencent_licaitong"}
    assert choices["ccb_7332"]["group_code"] == choices["ccb_credit_0453"]["group_code"] == "ccb"
    assert choices["ccb_7332"]["group_label"] == "建设银行"
    assert choices["santander"]["selection_label"] == "Santander 4209 储蓄卡"
    assert choices["santander"]["currency"] == "MXN"
    assert choices["tencent_licaitong"]["group_code"] == "tencent"
    assert choices["tencent_licaitong"]["import_modes"] == ("ocr",)
    assert choices["ccb_7332"]["import_modes"] == ("upload", "ocr", "manual")
    assert choices["wechat"]["selection_label"] == "微信支付账单（全渠道）"
    assert payload["counter_accounts"] == cur.fetchall.return_value


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("PAMS_IMPORT_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("PAMS_IMPORT_PUBLISH_ENABLED", "false")
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    with TestClient(app, headers={"Origin": "http://testserver"}) as value:
        yield value


@pytest.fixture
def db_client(client, monkeypatch):
    with scratch_database() as params:
        monkeypatch.setenv("PAMS_UNIFIED_SOURCE_ISOLATED", "true")
        monkeypatch.setenv("PAMS_SOURCE_TEST_TOKEN_KEY", "a1"*32)
        monkeypatch.setattr(store, "get_conn", lambda: psycopg2.connect(**params))
        yield client, params


def test_missing_schema_stays_unavailable_without_a_passphrase(client, monkeypatch):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    monkeypatch.setattr(store, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "_schema_ready", lambda cur: False)
    result = client.get(BASE + "/capabilities")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["authentication_required"] is False
    assert result.json()["schema_ready"] is False
    assert result.json()["publish_enabled"] is False
    assert client.get(BASE + "/context").status_code == 503
    conn.set_session.assert_called_with(readonly=True)


def test_database_failure_is_unavailable_not_an_unlock_prompt(client, monkeypatch):
    def unavailable():
        raise psycopg2.OperationalError("private connection details")
    monkeypatch.setattr(store, "get_conn", unavailable)
    caps = client.get(BASE + "/capabilities")
    assert caps.json()["schema_ready"] is False
    assert caps.json()["publish_enabled"] is False
    result = client.get(BASE + "/context")
    assert result.status_code == 503
    assert "private connection details" not in result.text


@pytest.mark.parametrize("legacy_token", [None, "short", "unused-legacy-token-at-least-24-characters"])
def test_no_cookie_or_legacy_secret_is_required(client, monkeypatch, legacy_token):
    if legacy_token is not None:
        monkeypatch.setenv("PAMS_IMPORT_ACCESS_TOKEN", legacy_token)
    conn = MagicMock()
    conn.__enter__.return_value = conn
    monkeypatch.setattr(store, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "_schema_ready", lambda cur: True)
    monkeypatch.setattr(store, "context", lambda cur: {"accounts": []})
    for cookies in [{}, {"pams_import_session": "0.bad.signature"}]:
        client.cookies.clear()
        client.cookies.update(cookies)
        caps = client.get(BASE + "/capabilities")
        assert caps.json()["authentication_required"] is False
        assert caps.json()["configured"] and caps.json()["authenticated"]
        assert caps.json()["schema_ready"] and not caps.json()["publish_enabled"]
        result = client.get(BASE + "/context")
        assert result.status_code == 200 and result.json() == {"accounts": []}
        assert result.headers["cache-control"] == "no-store"
        assert "set-cookie" not in result.headers and "set-cookie" not in caps.headers
        assert legacy_token is None or legacy_token not in result.text + caps.text
    assert client.post(BASE + "/unlock", json={"token": "unused"}).status_code == 404


@pytest.mark.parametrize("origin", [None, "http://attacker.invalid"])
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/batches", {"account_code": "tonghuashun", "kind": "trade", "mode": "manual"}),
    ("PUT", "/batches/{id}", {"revision": 1, "rows": []}),
    ("POST", "/batches/{id}/files?filename=test.csv", None),
    ("POST", "/batches/{id}/files/{id}/parse", {"mapping": {}}),
    ("POST", "/batches/{id}/files/{id}/review", {"revision": 1}),
    ("POST", "/batches/{id}/preview", {"revision": 1}),
    ("POST", "/batches/{id}/publish", {"revision": 1, "preview_token": "a" * 64}),
])
def test_all_mutations_still_require_same_origin_before_database_access(client, monkeypatch, origin, method, path, body):
    monkeypatch.setattr(store, "get_conn", lambda: pytest.fail("Rejected origin must not touch DB"))
    client.headers.pop("Origin", None)
    headers = {"Origin": origin} if origin is not None else {}
    result = client.request(method, BASE + path.format(id="00000000-0000-0000-0000-000000000000"),
                            json=body, headers=headers)
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "same_origin_required"


def create(client, kind="holding"):
    result = client.post(BASE + "/batches", json={"account_code": "tonghuashun", "mode": "manual", "kind": kind})
    assert result.status_code == 200, result.text
    return result.json()


def test_draft_revision_provenance_and_account_scope(db_client):
    client, params = db_client
    b = create(client)
    body = {"revision": 1, "completeness": "partial", "rows": [
        {"fields": {"quantity": "20.010", "symbol": "000001", "kind": "holding"}, "acknowledged": True}]}
    result = client.put(BASE + "/batches/" + b["batch_id"], json=body)
    assert result.status_code == 200, result.text
    assert result.json()["revision"] > b["revision"]
    new_revision = result.json()["revision"]
    row = result.json()["rows"][0]
    assert row["fields"]["quantity"] == "20.010" and row["source"] == {"origin": "manual"}
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 409
    body["revision"] = new_revision
    body["rows"][0]["fields"]["quantity"] = 20.01
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 400
    body["rows"][0]["fields"]["quantity"] = "20.01"
    body["rows"][0]["row_id"] = "00000000-0000-0000-0000-000000000000"
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 400
    unsupported = client.post(BASE + "/batches", json={"account_code": "ibkr", "mode": "manual", "kind": "trade"})
    assert unsupported.status_code == 400
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("SELECT parsed_fields->'fields'->>'quantity' FROM source_items")
        assert cur.fetchone()[0] == '20.010'



def test_private_file_duplicate_idempotence_mapping_and_cross_batch_denial(db_client):
    client, _ = db_client
    b = create(client)
    path = BASE + "/batches/" + b["batch_id"]
    content = "证券代码,证券名称,总份额,持仓成本,市值,日期\n000001,Test,20.01,100,120,2026-10-28\n".encode()
    first = client.post(path + "/files?filename=holdings.csv", content=content)
    assert first.status_code == 200, first.text
    assert first.json()["rows"][0]["fields"]["symbol"] == "000001"
    file = first.json()["files"][0]
    second = client.post(path + "/files?filename=renamed.csv", content=content)
    assert second.json()["revision"] == first.json()["revision"]
    evidence = client.get(path + "/files/" + file["file_id"])
    assert evidence.content == content and evidence.headers["cache-control"] == "no-store"
    other = create(client)
    assert client.get(BASE + "/batches/" + other["batch_id"] + "/files/" + file["file_id"]).status_code == 404
    assert client.post(path + "/files?filename=..%2Fbad.csv", content=content).status_code == 400
    assert client.get(BASE + "/batches").json()["rows"]


def test_formula_xlsx_and_unknown_field_updates_never_publish(db_client):
    from openpyxl import Workbook
    client, _ = db_client
    b = create(client)
    wb = Workbook()
    wb.active.append(["证券代码", "持仓成本"])
    wb.active.append(["000001", "=1+2"])
    data = BytesIO()
    wb.save(data)
    path = BASE + "/batches/" + b["batch_id"]
    assert client.post(path + "/files?filename=formula.xlsx", content=data.getvalue()).status_code == 400
    result = client.get(path).json()
    assert result["rows"] == [] and result["files"] == [] and result["revision"] == 1
    bad = {"revision": 1, "rows": [{"fields": {"source_table": "evil"}}]}
    assert client.put(path, json=bad).status_code == 400
    capabilities = client.get(BASE + "/capabilities").json()
    assert capabilities["schema_ready"] and capabilities["authenticated"]
    assert capabilities["publish_enabled"] is False
    assert client.post(path + "/preview", json={"revision": 1}).json()["can_publish"] is False
