"""Staging, private evidence and access checks; no production writes."""
from io import BytesIO
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/api"))
from endpoints import account_import as api  # noqa: E402
from services import account_import_access as access  # noqa: E402
from services import account_import_store as store  # noqa: E402
from account_import_support import scratch_database  # noqa: E402

TOKEN = "isolated-test-only-import-access-token"
BASE = "/api/account-import"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAMS_IMPORT_ACCESS_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    with TestClient(app, headers={"Origin": "http://testserver"}) as value:
        yield value


@pytest.fixture
def db_client(client, monkeypatch):
    with scratch_database() as params:
        monkeypatch.setattr(store, "get_conn", lambda: psycopg2.connect(**params))
        assert client.post(BASE + "/unlock", json={"token": TOKEN}).status_code == 200
        yield client, params


def test_default_capabilities_do_not_connect_or_create_schema(client, monkeypatch):
    monkeypatch.delenv("PAMS_IMPORT_ACCESS_TOKEN")
    monkeypatch.setattr(store, "get_conn", lambda: pytest.fail("Unconfigured import must not touch DB"))
    result = client.get(BASE + "/capabilities")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["configured"] is False
    assert result.json()["publish_enabled"] is False
    assert client.get(BASE + "/context").status_code == 401


def test_unlock_is_same_origin_cookie_private_and_never_reveals_token(client):
    assert client.post(BASE + "/unlock", json={"token": TOKEN}, headers={"Origin": "http://attacker.invalid"}).status_code == 403
    assert client.post(BASE + "/unlock", json={"token": "wrong"}).status_code == 401
    result = client.post(BASE + "/unlock", json={"token": TOKEN})
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert TOKEN not in result.text and TOKEN not in result.headers["set-cookie"]
    assert access.COOKIE in client.cookies


def test_tampered_and_expired_sessions_are_rejected(client, monkeypatch):
    client.cookies.set(access.COOKIE, "0.bad.signature")
    assert client.get(BASE + "/context").status_code == 401
    monkeypatch.setattr(access.time, "time", lambda: 100)
    token = access.signed_session()
    client.cookies.set(access.COOKIE, token)
    monkeypatch.setattr(access.time, "time", lambda: 2000)
    assert client.get(BASE + "/context").status_code == 401


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
    assert result.json()["revision"] == 2
    row = result.json()["rows"][0]
    assert row["fields"]["quantity"] == "20.010" and row["source"] == {"origin": "manual"}
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 409
    body["revision"] = 2
    body["rows"][0]["fields"]["quantity"] = 20.01
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 400
    body["rows"][0]["fields"]["quantity"] = "20.01"
    body["rows"][0]["row_id"] = "00000000-0000-0000-0000-000000000000"
    assert client.put(BASE + "/batches/" + b["batch_id"], json=body).status_code == 400
    unsupported = client.post(BASE + "/batches", json={"account_code": "ibkr", "mode": "manual", "kind": "trade"})
    assert unsupported.status_code == 400
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("SELECT event,details FROM pams_account_import_audit ORDER BY created_at")
        audits = cur.fetchall()
        assert [r[0] for r in audits] == ["created", "draft_saved"]
        assert audits[-1][1]["changes"][0]["after"]["fields"]["quantity"] == "20.010"


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
