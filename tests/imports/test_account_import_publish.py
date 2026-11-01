"""Real isolated PG16: no financial writes until explicit opt-in publish."""
from datetime import date, timedelta
from decimal import Decimal
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from tests.support.account_import_support import scratch_database
from endpoints.account_import import router
from domain.imports import account_import_store as store
from domain.imports import native_source_http as publisher

DAY = (date.today() - timedelta(days=2)).isoformat()


@pytest.fixture
def client(monkeypatch):
    with scratch_database() as params:
        monkeypatch.delenv("PAMS_IMPORT_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("PAMS_IMPORT_PUBLISH_ENABLED", "true")
        monkeypatch.setenv("PAMS_UNIFIED_SOURCE_ISOLATED", "true")
        monkeypatch.setenv("PAMS_SOURCE_TEST_TOKEN_KEY", "a1"*32)
        monkeypatch.setattr(store, "get_conn", lambda: psycopg2.connect(**params))
        app = FastAPI()
        app.include_router(router, prefix="/api")
        with TestClient(app) as value:
            value.headers["Origin"] = "http://testserver"
            yield value, params


def sql(params, query, values=None):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, values)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def trade(**changes):
    return {"kind": "trade", "business_date": DAY, "status": "confirmed", "action": "buy",
            "asset_type": "stock", "symbol": "000001", "currency": "CNY", "quantity": "100",
            "price": "10", "gross_amount": "1000", "fee_scope": "total_charges", "fee_amount": "5",
            "net_cash": "-1005", **changes}


def holding(**changes):
    return {"kind": "holding", "business_date": DAY, "asset_type": "stock", "symbol": "000001",
            "currency": "CNY", "quantity": "100", "price": "10", "market_value": "1000",
            "cost": "900", "cost_basis_type": "holding_cost", **changes}


def draft(client, fields, *, account="tonghuashun", completeness="partial", acknowledgements=None):
    response = client.post("/api/account-import/batches", json={"account_code": account, "mode": "manual", "kind": fields[0]["kind"], "completeness": completeness})
    assert response.status_code == 200, response.text
    batch = response.json()
    rows = [{"fields": f, "acknowledged": bool(acknowledgements)} for f in fields]
    response = client.put("/api/account-import/batches/" + batch["batch_id"], json={"revision": batch["revision"], "rows": rows, "completeness": completeness})
    assert response.status_code == 200, response.text
    return response.json()


def preview(client, batch):
    response = client.post(f"/api/account-import/batches/{batch['batch_id']}/preview", json={"revision": batch["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, batch, check):
    return client.post(f"/api/account-import/batches/{batch['batch_id']}/publish", json={"revision": batch["revision"], "preview_token": check["preview_token"]})


def issues(result):
    return {i["code"] for r in result["rows"] for i in r["issues"]}


def test_publish_default_off_preview_readonly(client, monkeypatch):
    web, params = client
    monkeypatch.delenv("PAMS_IMPORT_PUBLISH_ENABLED")
    batch = draft(web, [trade()])
    check = preview(web, batch)
    assert check["can_publish"] and check["impact"]["nav_change"] is None
    assert not web.get("/api/account-import/capabilities").json()["publish_enabled"]
    assert sql(params, "SELECT count(*) n FROM pams_investment_trade_fact")[0]["n"] == 0
    assert publish(web, batch, check).status_code == 403
    assert sql(params, "SELECT count(*) n FROM pams_investment_trade_fact")[0]["n"] == 0


def native_batch(web):
    from tests.imports.test_account_import_ths import event, export
    rows = [event(), event("证券卖出", **{"成交均价": "11", "成交金额": "1100", "发生金额": "1096.350", "印花税": "0.550", "定位串": "20251020!line2"}),
            event("红利入账", **{"成交金额": "30.000", "发生金额": "30.000", "定位串": "20251020!line3"}),
            event("红利税补扣", **{"发生金额": "-2.100", "定位串": "20251020!line4"}),
            event("利息归本", **{"发生金额": "2.200", "定位串": "20251020!line5"}),
            event("银行转证券", **{"发生金额": "500.000", "定位串": "20251020!line6"}),
            event("证券转银行", **{"发生金额": "-100.000", "定位串": "20251020!line7"})]
    batch = web.post("/api/account-import/batches", json={"account_code": "tonghuashun", "mode": "upload", "kind": "trade", "completeness": "partial"}).json()
    content = export(*rows)
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls", content=content, headers={"Content-Type": "application/vnd.ms-excel"})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert len(batch["rows"]) == 7 and batch["files"][0]["content_type"] == "text/plain"
    return batch, content


def test_native_upload_initializes_missing_asset_from_source_evidence(client):
    from tests.imports.test_account_import_ths import event, export
    web, params = client
    batch = web.post('/api/account-import/batches', json={
        'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade', 'completeness': 'partial'}).json()
    content = export(event(**{'证券代码': '000002', '证券名称': 'Synthetic stock', '定位串': 'synthetic-one'}))
    response = web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls", content=content,
                        headers={'Content-Type': 'application/vnd.ms-excel'})
    assert response.status_code == 200, response.text
    staged = response.json()
    assert staged['asset_initialization']['created'] == ['000002']
    asset = sql(params, "SELECT * FROM pams_asset_master WHERE primary_symbol='000002'")[0]
    assert asset['source'] == 'ths_import' and asset['confidence'] == 'low'
    assert asset['raw_json']['source_file_sha256'] == staged['files'][0]['sha256']
    assert asset['raw_json']['pending_fields'] == ['asset_type', 'currency']
    checked = preview(web, staged)
    assert checked['counts']['blocked'] == 0
    assert any('待核对默认值' in warning for warning in checked['warnings'])
    assert not sql(params, 'SELECT * FROM pams_investment_trade_fact')


def test_import_identity_review_requires_fresh_user_confirmation(client):
    from tests.imports.test_account_import_ths import event, export
    from domain.assets import import_identity_review
    web, params = client
    batch = web.post('/api/account-import/batches', json={
        'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade', 'completeness': 'partial'}).json()
    content = export(event(**{'证券代码': '000002', '证券名称': 'Synthetic stock', '定位串': 'synthetic-one'}))
    assert web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls", content=content).status_code == 200
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        pending = import_identity_review.pending(cur)
        assert len(pending) == 1 and pending[0]['symbol'] == '000002'
        with pytest.raises(ValueError):
            import_identity_review.confirm(cur, asset_id=pending[0]['asset_id'], expected_updated_at='stale',
                                           asset_type='ETF / 基金', currency='CNY')
        import_identity_review.confirm(cur, asset_id=pending[0]['asset_id'], expected_updated_at=pending[0]['updated_at'],
                                       asset_type='ETF / 基金', currency='CNY')
        assert not import_identity_review.pending(cur)
    row = sql(params, "SELECT * FROM pams_asset_master WHERE primary_symbol='000002'")[0]
    assert row['asset_type'] == 'ETF / 基金'
    assert row['raw_json']['import_identity_review']['previous_asset_type'] == 'A股个股'
    assert row['raw_json']['source_file_sha256']


def test_asset_review_endpoint_requires_same_origin_and_shows_pending(client, monkeypatch):
    from endpoints import analysis
    from tests.imports.test_account_import_ths import event, export
    web, params = client
    monkeypatch.setattr(analysis, 'get_conn', lambda: psycopg2.connect(**params))
    web.app.include_router(analysis.router, prefix='/api')
    batch = web.post('/api/account-import/batches', json={
        'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade', 'completeness': 'partial'}).json()
    content = export(event(**{'证券代码': '000002', '证券名称': 'Synthetic stock', '定位串': 'synthetic-one'}))
    assert web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls", content=content).status_code == 200
    review = web.get('/api/analysis/asset-import-review').json()['rows'][0]
    payload = {'expected_updated_at': review['updated_at'], 'asset_type': 'A股个股', 'currency': 'CNY'}
    route = '/api/analysis/asset-import-review/' + review['asset_id']
    assert web.post(route, json=payload, headers={'Origin': 'http://other.invalid'}).status_code == 403
    assert web.post(route, json=payload).status_code == 200
    assert web.get('/api/analysis/asset-import-review').json()['rows'] == []


def test_repeated_native_upload_preserves_existing_asset_and_provenance(client):
    from tests.imports.test_account_import_ths import event, export
    web, params = client
    for index, name in enumerate(['Synthetic one', 'Synthetic renamed']):
        batch = web.post('/api/account-import/batches', json={
            'account_code': 'tonghuashun', 'mode': 'upload', 'kind': 'trade', 'completeness': 'partial'}).json()
        content = export(event(**{'证券代码': '000002', '证券名称': name, '定位串': f'synthetic-{index}'}))
        result = web.post(f"/api/account-import/batches/{batch['batch_id']}/files?filename=table.xls", content=content)
        assert result.status_code == 200, result.text
        assert result.json()['asset_initialization']['created'] == (['000002'] if index == 0 else [])
    rows = sql(params, "SELECT * FROM pams_asset_master WHERE primary_symbol='000002'")
    assert len(rows) == 1 and rows[0]['canonical_name'] == 'Synthetic one'
    assert rows[0]['raw_json']['source_records'][0]['name'] == 'Synthetic one'


def test_native_source_market_conflict_does_not_match_existing_identity(client):
    web, params = client
    sql(params, "UPDATE pams_asset_master SET exchange='SSE' WHERE primary_symbol='000001'")
    batch, _ = native_batch(web)
    check = preview(web, batch)
    assert any(issue['code'] == 'asset_market_mismatch' for issue in check['rows'][0]['issues'])
    assert check['rows'][0]['status'] == 'blocked'


def test_explicit_asset_sync_preserves_reviewed_import_identity(client):
    from domain.assets import asset_master
    _, params = client
    schema = (Path(__file__).resolve().parents[2] / 'database/postgres/schema/023_asset_master.sql').read_text(encoding='utf-8')
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('CREATE SCHEMA synthetic_asset_review')
        cur.execute('SET LOCAL search_path TO synthetic_asset_review, pg_catalog')
        cur.execute(schema)
        cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency,source,confidence,raw_json)
                    VALUES ('A_000002','000002','Reviewed fund','ETF / 基金','CNY','manual','low',
                            '{"import_identity_reviewed":true}'::jsonb)""")
        assert asset_master._upsert_asset(cur, {'symbol': '000002', 'name': 'Automatic name',
                                               'asset_type': 'ETF / 基金', 'currency': 'CNY'}, source='system') == 'A_000002'
        asset_id = asset_master._upsert_asset(cur, {'symbol': '000002', 'name': 'Automatic name',
                                                   'asset_type': 'A股个股', 'currency': 'CNY'}, source='system')
        cur.execute("SELECT asset_id,canonical_name,asset_type,source,raw_json FROM pams_asset_master WHERE primary_symbol='000002'")
        rows = cur.fetchall()
        assert asset_id == 'A_000002' and len(rows) == 1
        assert rows[0]['canonical_name'] == 'Reviewed fund' and rows[0]['asset_type'] == 'ETF / 基金'
        assert rows[0]['source'] == 'manual' and rows[0]['raw_json']['import_identity_reviewed'] is True
        cur.execute("SELECT classification_scope FROM asset_classifications WHERE asset_id='A_000002' AND classification_type='industry'")
        assert cur.fetchone()['classification_scope'] == 'fund_lookthrough'


def test_native_mixed_file_preview_never_writes_financial_facts(client, monkeypatch):
    web, params = client
    monkeypatch.delenv("PAMS_IMPORT_PUBLISH_ENABLED")
    batch, content = native_batch(web)
    assert batch["rows"][0]["source"]["trade_actions"]["cash"] == ["deposit", "withdraw", "interest"]
    context = web.get("/api/account-import/context").json()
    ordinary = next(a for a in context["accounts"] if a["account_code"] == "tonghuashun")["trade_actions"]
    assert "cash" not in ordinary and "tax" not in ordinary["stock"]
    check = preview(web, batch)
    assert check["counts"] == {"new": 5, "duplicates": 0, "blocked": 0, "pending": 2, "excluded": 0}, check["rows"]
    assert check["rows"][0]["fields"]["asset_type"] == "stock"
    assert check["rows"][3]["fields"]["action"] == "tax"
    assert not check["can_publish"] and publish(web, batch, check).status_code == 403
    assert sql(params, "SELECT content FROM source_files")[0]["content"].tobytes() == content
    for table in ("pams_investment_trade_fact", "pams_cashflow_fact", "pams_holdings_snapshot"):
        assert not sql(params, "SELECT * FROM " + table)


def test_void_draft_hides_batch_but_retains_source_and_rejects_stale_revision(client):
    web, params = client
    batch, content = native_batch(web)
    endpoint = f"/api/account-import/batches/{batch['batch_id']}/void"
    assert web.post(endpoint, json={'revision': batch['revision']}, headers={'Origin': 'http://other.invalid'}).status_code == 403
    assert web.post(endpoint, json={'revision': batch['revision'] - 1}).status_code == 409
    assert sql(params, 'SELECT status FROM source_batches')[0]['status'] == 'draft'
    response = web.post(endpoint, json={'revision': batch['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'voided'
    assert web.get('/api/account-import/batches').json()['rows'] == []
    assert web.get('/api/account-import/batches/' + batch['batch_id']).json()['status'] == 'voided'
    assert sql(params, 'SELECT content FROM source_files')[0]['content'].tobytes() == content
    assert len(sql(params, 'SELECT * FROM source_items')) == 7
    assert not sql(params, 'SELECT * FROM pams_investment_trade_fact')
    assert web.post(endpoint, json={'revision': response.json()['revision']}).status_code == 409


def test_void_refuses_published_or_linked_drafts(client):
    web, params = client
    published = draft(web, [trade()])
    assert publish(web, published, preview(web, published)).status_code == 200
    path = f"/api/account-import/batches/{published['batch_id']}/void"
    assert web.post(path, json={'revision': published['revision']}).status_code == 409
    assert len(sql(params, 'SELECT * FROM pams_investment_trade_fact')) == 1
    linked = draft(web, [trade(source_ref='new-source')])
    sql(params, """INSERT INTO pams_cashflow_fact(event_date,account_code,amount,amount_cny,
        source_table,source_id,source_hash,source_item_id)
        VALUES (%s,'tonghuashun',1,1,'synthetic','linked','synthetic-hash',%s)""",
        (DAY, linked['rows'][0]['row_id']))
    result = web.post(f"/api/account-import/batches/{linked['batch_id']}/void", json={'revision': linked['revision']})
    assert result.status_code == 409 and result.json()['detail']['code'] == 'draft_has_evidence'
    assert sql(params, 'SELECT status FROM source_batches WHERE batch_id=%s', (linked['batch_id'],))[0]['status'] == 'draft'


def test_concurrent_void_and_publish_have_only_one_effect(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    web, params = client
    batch = draft(web, [trade(source_ref='synthetic-void-race')])
    check = preview(web, batch)
    barrier = Barrier(2)
    def act(action):
        try:
            with psycopg2.connect(**params) as conn:
                conn.set_session(isolation_level='SERIALIZABLE')
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    barrier.wait(timeout=10)
                    if action == 'void':
                        publisher.void_draft(cur, batch['batch_id'], batch['revision'])
                    else:
                        publisher.publish(cur, batch['batch_id'], batch['revision'], check['preview_token'])
            return action
        except (psycopg2.Error, store.ImportProblem, ValueError):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        successes = list(pool.map(act, ['void', 'publish']))
    assert len([value for value in successes if value]) == 1
    status = sql(params, 'SELECT status FROM source_batches')[0]['status']
    assert status in {'voided', 'published'}
    assert len(sql(params, 'SELECT * FROM pams_investment_trade_fact')) == (1 if status == 'published' else 0)


def test_native_funding_bank_links_idempotence_and_financial_flags_in_isolation(client):
    web, params = client
    batch, _ = native_batch(web)
    for row in batch["rows"]:
        if row["fields"]["action"] in {"deposit", "withdraw"}:
            row["fields"]["counter_account_code"] = "bank_test"
    batch = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={"revision": batch["revision"], "rows": batch["rows"]}).json()
    check = preview(web, batch)
    assert check["can_publish"], check["rows"]
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    assert publish(web, batch, check).json()["replayed"]
    facts = sql(params, "SELECT * FROM pams_investment_trade_fact ORDER BY trade_id")
    assert len(facts) == 7 and len(sql(params, "SELECT * FROM pams_cashflow_fact")) == 2
    by_side = {f["side"]: f for f in facts}
    assert by_side["buy"]["fee_amount"] == Decimal("3.100")
    assert by_side["sell"]["net_cash"] == Decimal("1096.350")
    assert by_side["tax"]["net_cash"] == Decimal("-2.100") and by_side["interest"]["net_cash"] == Decimal("2.200")
    for action in ("deposit", "withdraw", "interest", "tax", "dividend"):
        assert not by_side[action]["affects_position"] and not by_side[action]["affects_cost_basis"]
    for action in ("deposit", "withdraw"):
        assert by_side[action]["cashflow_id"] is not None and not by_side[action]["affects_realized_pnl"]
    assert not sql(params, "SELECT * FROM pams_holdings_snapshot")
    again, _ = native_batch(web)
    for row in again["rows"]:
        if row["fields"]["action"] in {"deposit", "withdraw"}:
            row["fields"]["counter_account_code"] = "bank_test"
    again = web.put(f"/api/account-import/batches/{again['batch_id']}", json={"revision": again["revision"], "rows": again["rows"]}).json()
    repeated = web.post(f"/api/account-import/batches/{again['batch_id']}/preview", json={"revision":again['revision']})
    assert repeated.status_code == 400 and "explicit association" in repeated.text
    assert len(sql(params, "SELECT * FROM pams_investment_trade_fact")) == 7
    assert len(sql(params, "SELECT * FROM pams_cashflow_fact")) == 2


def test_native_type_resolution_is_asset_master_based_not_code_prefix(client):
    web, params = client
    sql(params, "UPDATE pams_asset_master SET asset_type='ETF / 基金' WHERE primary_symbol='000001'")
    batch, _ = native_batch(web)
    check = preview(web, batch)
    assert check["counts"]["blocked"] == 0
    assert all(row["fields"]["asset_type"] == "fund" for row in check["rows"][:4])
    assert all(row["fields"]["asset_type"] == "cash" for row in check["rows"][4:])


def test_trade_canonical_amount_source_idempotence(client):
    web, params = client
    batch = draft(web, [trade(source_ref="official-001")])
    check = preview(web, batch)
    assert check["counts"]["new"] == 1
    response = publish(web, batch, check)
    assert response.status_code == 200, response.text
    assert response.json()["batch"]["status"] == "published"
    assert publish(web, batch, check).json()["replayed"]
    fact = sql(params, "SELECT * FROM pams_investment_trade_fact")[0]
    assert fact["net_cash"] == Decimal("-1005") and fact["fee_amount"] == 5 and fact["tax_amount"] is None
    assert fact["source_table"] == "source_items" and fact["review_status"] == "confirmed"
    assert fact["raw_json"]["source_row"]["fee_scope"] == "total_charges"
    other = draft(web, [trade(source_ref="official-001")])
    again = web.post(f"/api/account-import/batches/{other['batch_id']}/preview",json={"revision":other["revision"]})
    assert again.status_code == 400 and "explicit association" in again.text
    assert sql(params, "SELECT count(*) n FROM pams_investment_trade_fact")[0]["n"] == 1


@pytest.mark.parametrize("change,code", [
    ({"fee_amount": None}, "missing"), ({"net_cash": "-999"}, "net_mismatch"),
    ({"quantity": "NaN"}, "invalid_decimal"), ({"status": "pending"}, "pending_confirmation"),
    ({"currency": "USD"}, "currency_unverified"), ({"symbol": "999999"}, "asset_unresolved"),
    ({"fee_scope": "total_charges", "tax_amount": "1"}, "double_charge"),
    ({"quantity": "101"}, "gross_mismatch"), ({"business_date": ""}, "invalid_date"),
    ({"action": "sell", "net_cash": "995", "fee_amount": "-5"}, "invalid_sign"),
])
def test_financial_gate(client, change, code):
    web, params = client
    batch = draft(web, [trade(**change)])
    check = preview(web, batch)
    assert code in issues(check) and not check["can_publish"]
    assert publish(web, batch, check).status_code == 409
    assert not sql(params, "SELECT * FROM pams_investment_trade_fact")


def test_fund_confirmation_not_application_date(client):
    web, params = client
    fields = trade(asset_type="fund", symbol="011613", action="subscribe", confirmed_date=DAY)
    batch = draft(web, [fields], account="tencent_licaitong")
    check = preview(web, batch)
    assert check["can_publish"], check["rows"]
    assert publish(web, batch, check).status_code == 200
    fact = sql(params, "SELECT * FROM pams_investment_trade_fact")[0]
    assert fact["trade_type"] == "fund_subscription" and str(fact["trade_date"]) == DAY
    bad = draft(web, [{**fields, "confirmed_date": None}], account="tencent_licaitong")
    assert "invalid_confirmation_date" in issues(preview(web, bad))


def test_ambiguous_duplicates_require_review_or_stable_distinct_reference(client):
    web, _ = client
    first = draft(web, [trade(source_ref="one")])
    assert publish(web, first, preview(web, first)).status_code == 200
    uncertain = draft(web, [trade()])
    assert "possible_duplicate" in issues(preview(web, uncertain))
    distinct = draft(web, [trade(source_ref="two")])
    assert preview(web, distinct)["can_publish"]
    conflict = draft(web, [trade(source_ref="one", fee_amount="6", net_cash="-1006")])
    assert "source_conflict" in issues(preview(web, conflict))


def test_changed_state_and_revision_invalidate_preview(client):
    web, params = client
    batch = draft(web, [trade()])
    check = preview(web, batch)
    sql(params, "UPDATE pams_asset_master SET canonical_name='Changed' WHERE asset_id='TEST_STOCK'")
    assert publish(web, batch, check).json()["detail"]["code"] == "source_changed"
    check = preview(web, batch)
    saved = web.put(f"/api/account-import/batches/{batch['batch_id']}", json={"revision": batch["revision"], "rows": batch["rows"]})
    assert saved.status_code == 200
    assert publish(web, batch, check).status_code == 409


def test_atomic_failure_rolls_back_bank_leg_trade_and_audit(client, monkeypatch):
    web, params = client
    fields = {"kind": "cash", "business_date": DAY, "currency": "CNY", "action": "deposit", "net_cash": "500", "counter_account_code": "bank_test"}
    batch = draft(web, [fields])
    check = preview(web, batch)
    assert check["can_publish"]
    def fail(*args, **kwargs):
        raise store.ImportProblem("injected", "isolated failure", 409)
    from domain.imports import native_source
    monkeypatch.setattr(native_source, "insert_investment_trade_facts", fail)
    assert publish(web, batch, check).status_code == 409
    assert not sql(params, "SELECT * FROM pams_cashflow_fact") and not sql(params, "SELECT * FROM pams_investment_trade_fact")
    assert sql(params, "SELECT status FROM source_batches")[0]["status"] == "draft"
    assert sql(params, "SELECT published_result FROM source_batches")[0]["published_result"] is None


def test_cash_two_legs_linked_no_spending_or_nav_double_count(client):
    web, params = client
    fields = {"kind": "cash", "business_date": DAY, "currency": "CNY", "action": "deposit", "net_cash": "500", "counter_account_code": "bank_test"}
    batch = draft(web, [fields])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    bank = sql(params, "SELECT * FROM pams_cashflow_fact")[0]
    broker = sql(params, "SELECT * FROM pams_investment_trade_fact")[0]
    assert bank["amount"] + broker["net_cash"] == 0 and broker["cashflow_id"] == bank["cashflow_id"]
    assert not bank["affects_spending"] and not bank["affects_nav"] and bank["affects_investment_capital"]


def test_snapshot_missing_members_never_silently_closed(client):
    web, params = client
    initial = draft(web, [holding(), holding(asset_type="cash", symbol=None, action="balance", net_cash="50")], completeness="complete")
    check = preview(web, initial)
    assert check["can_publish"], check
    assert publish(web, initial, check).status_code == 200
    assert len(sql(params, "SELECT * FROM pams_holdings_snapshot")) == 2
    next_day = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    partial = draft(web, [holding(business_date=next_day)])
    assert "incomplete_snapshot" in issues(preview(web, partial))
    old = draft(web, [holding(business_date=(date.fromisoformat(DAY)-timedelta(days=1)).isoformat())])
    assert "stale_snapshot" in issues(preview(web, old))
    zero = draft(web, [holding(business_date=next_day, quantity="0", market_value="0", cost="0"),
                       holding(business_date=next_day, asset_type="cash", symbol=None, action="balance", net_cash="1050")], completeness="complete")
    scope = web.post(f"/api/account-import/batches/{zero['batch_id']}/confirm-scope", json={
        'revision':zero['revision'], 'as_of':next_day, 'member_count':2, 'cash_amount':'1050',
        'note':'核对完整持仓和现金', 'confirmed':True})
    assert scope.status_code == 200, scope.text
    zero = web.get(f"/api/account-import/batches/{zero['batch_id']}").json()
    assert publish(web, zero, preview(web, zero)).status_code == 200
    assert len(sql(params, "SELECT * FROM pams_holdings_snapshot WHERE as_of=%s", (next_day,))) == 2


def test_snapshot_precision_cost_type_and_multiple_targets(client):
    web, _ = client
    for fields, code in [(holding(cost_basis_type="diluted_cost"), "unverified_cost"),
                         (holding(price="11"), "value_mismatch"),
                         (holding(quantity="100.0000001"), "invalid_decimal")]:
        assert code in issues(preview(web, draft(web, [fields])))
    assert "duplicate_snapshot_target" in issues(preview(web, draft(web, [holding(source_ref="one"), holding(source_ref="two")])))


def test_expired_preview_and_disabled_account(client, monkeypatch):
    web, params = client
    batch = draft(web, [trade()])
    check = preview(web, batch)
    original_time = publisher.time.time()
    with monkeypatch.context() as patch:
        patch.setattr(publisher.time, 'time', lambda: original_time + 1000)
        assert publish(web, batch, check).json()["detail"]["code"] == "source_changed"
    sql(params, "UPDATE pams_accounts SET active=false WHERE account_code='tonghuashun'")
    assert "account_inactive" in issues(preview(web, batch))


def test_snapshot_repeat_is_noop_and_negative_cash_blocked(client):
    web, params = client
    batch = draft(web, [holding()])
    assert publish(web, batch, preview(web, batch)).status_code == 200
    old = sql(params, "SELECT * FROM pams_holdings_snapshot")
    again = draft(web, [holding()])
    check = preview(web, again)
    assert check["counts"]["duplicates"] == 1 and publish(web, again, check).status_code == 200
    assert old == sql(params, "SELECT * FROM pams_holdings_snapshot")
    bad = draft(web, [holding(asset_type="cash", symbol=None, net_cash="-1")])
    assert "negative_cash_unsupported" in issues(preview(web, bad))


def test_same_reference_different_time_conflicts(client):
    web, _ = client
    first = draft(web, [trade(source_ref="clock", trade_time="10:00")])
    assert publish(web, first, preview(web, first)).status_code == 200
    changed = draft(web, [trade(source_ref="clock", trade_time="10:01")])
    assert "source_conflict" in issues(preview(web, changed))


def test_concurrent_publication_has_one_effect(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    web, params = client
    batch = draft(web, [trade(source_ref="concurrent")])
    check = preview(web, batch)
    barrier = Barrier(2)
    def call():
        try:
            with psycopg2.connect(**params) as conn:
                conn.set_session(isolation_level="SERIALIZABLE")
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    barrier.wait(timeout=5)
                    publisher.publish(cur, batch["batch_id"], batch["revision"], check["preview_token"])
            return "ok"
        except psycopg2.errors.SerializationFailure:
            return "retry"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    assert "ok" in results
    assert sql(params, "SELECT count(*) n FROM pams_investment_trade_fact")[0]["n"] == 1
    assert sql(params, "SELECT count(*) n FROM source_batches WHERE status='published'")[0]["n"] == 1


@pytest.mark.parametrize("raw_type,account,action,symbol", [
    ("A股个股", "tonghuashun", "buy", "000001"),
    ("ETF / 基金", "tonghuashun", "buy", "011613"),
    ("债券 / 货币基金", "tencent_licaitong", "subscribe", "011613"),
])
def test_existing_master_vocabulary_and_venue_actions(client, raw_type, account, action, symbol):
    web, params = client
    sql(params, "UPDATE pams_asset_master SET asset_type=%s WHERE primary_symbol=%s", (raw_type, symbol))
    expected = "stock" if symbol == "000001" else "fund"
    asset = web.get("/api/account-import/assets", params={"q": symbol}).json()["rows"][0]
    assert asset["asset_type"] == expected and asset["source_asset_type"] == raw_type
    batch = draft(web, [trade(asset_type=expected, action=action, symbol=symbol, confirmed_date=DAY if action == "subscribe" else None)], account=account)
    check = preview(web, batch)
    assert check["can_publish"], check["rows"]
    assert publish(web, batch, check).status_code == 200
    wrong = "subscribe" if action == "buy" else "buy"
    rejected = draft(web, [trade(asset_type=expected, action=wrong, symbol=symbol, confirmed_date=DAY)], account=account)
    assert "action_type" in issues(preview(web, rejected))


def test_full_snapshot_counts_unchanged_members(client):
    web, _ = client
    batch = draft(web, [holding(), holding(asset_type="cash", action="balance", net_cash="50", symbol=None)], completeness="complete")
    assert publish(web, batch, preview(web, batch)).status_code == 200
    changed = draft(web, [holding(), holding(asset_type="cash", action="balance", net_cash="60", symbol=None)], completeness="complete")
    check = preview(web, changed)
    assert check["counts"]["duplicates"] == 1 and check["counts"]["new"] == 1 and check["can_publish"]
    assert publish(web, changed, check).status_code == 200


def test_display_fifty_percent_heuristic_is_not_import_membership_proof(client):
    web, params = client
    old_day = (date.fromisoformat(DAY)-timedelta(days=1)).isoformat()
    for symbol in ("000001", "000002", "000003", "000004"):
        sql(params, "INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,as_of,quantity,cost,market_value,asset_liability) VALUES('tonghuashun',%s,%s,'CNY',%s,100,900,1000,'资产')", (symbol, symbol, old_day))
    for symbol in ("000001", "000002"):
        sql(params, "INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,as_of,quantity,cost,market_value,asset_liability) VALUES('tonghuashun',%s,%s,'CNY',%s,100,900,1000,'资产')", (symbol, symbol, DAY))
    fresh = draft(web, [holding(business_date=(date.fromisoformat(DAY)+timedelta(days=1)).isoformat())])
    assert "incomplete_snapshot" in issues(preview(web, fresh))


@pytest.mark.parametrize("currency,liability", [("USD", "资产"), ("CNY", "负债")])
def test_cash_does_not_replace_foreign_cash_or_loans(client, currency, liability):
    web, params = client
    sql(params, "INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,as_of,quantity,cost,market_value,asset_liability) VALUES('tonghuashun','CASH_OLD','Old cash',%s,%s,10,10,10,%s)", (currency, DAY, liability))
    old = sql(params, "SELECT * FROM pams_holdings_snapshot")
    batch = draft(web, [holding(asset_type="cash", symbol=None, net_cash="50")])
    check = preview(web, batch)
    assert "foreign_member_unsupported" in issues(check) and check["rows"][0]["before"] is None
    assert publish(web, batch, check).status_code == 409
    assert old == sql(params, "SELECT * FROM pams_holdings_snapshot")
