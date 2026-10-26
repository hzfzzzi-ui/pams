"""Exercise publication against the same disposable PostgreSQL fixture as atomic sync."""
from datetime import date, datetime, timezone
from threading import Event, Thread
import time

import pytest

from test_ibkr_sync_transaction import (
    XML, business_state, collector, database as database, ibkr, rows, sync_case as sync_case,
)
from services import ibkr_report_state as state


@pytest.mark.parametrize("kind,reason", [
    ("stale", "stale_statement_date"),
    ("future", "future_or_unclosed_statement_date"),
    ("incomplete", "missing_base_cash"),
])
def test_candidate_rejected_before_any_business_write(sync_case, database, monkeypatch, kind, reason):
    client, _, report = sync_case
    before = business_state(database)
    if kind == "stale":
        report["xml"] = XML.replace("20261022", "20261021")
    elif kind == "future":
        report["xml"] = XML.replace("20261022", "20261023")
    else:
        report["xml"] = XML.replace('currency="BASE_SUMMARY"', 'currency="IGNORED"')
    monkeypatch.setattr(collector, "_write_holdings", lambda *a, **k: pytest.fail("Gate was too late"))
    result = client.post("/api/ibkr/flex/sync", json={}).json()
    assert result["status"] == "blocked" and result["reason"] == reason
    assert business_state(database) == before
    assert sorted(row["status"] for row in rows(database, "pams_data_sync_runs")) == ["blocked", "success"]


def test_duplicate_keeps_publication_and_all_business_rows(sync_case, database, tmp_path):
    client, conn, _ = sync_case
    assert client.post("/api/ibkr/flex/sync", json={}).json()["success"]
    active, _ = state.select_report(conn, tmp_path / "raw/ibkr")
    conn.rollback()
    before = business_state(database)
    result = client.post("/api/ibkr/flex/sync", json={}).json()
    assert result["sync_run"]["publication"]["state"] == "unchanged"
    assert business_state(database) == before
    selected, latest = state.select_report(conn, tmp_path / "raw/ibkr")
    assert selected == active
    assert state.report_quality(selected, latest, expected=date(2026, 10, 22))["status"] == "ok"


def test_failed_report_cannot_be_selected_by_performance_or_risk(sync_case, database, monkeypatch, tmp_path):
    from services import ibkr_performance, risk_state
    client, conn, _ = sync_case
    before, _ = state.select_report(conn, tmp_path / "raw/ibkr")
    conn.rollback()
    def fail(cur):
        raise RuntimeError("fact failure")
    monkeypatch.setattr(ibkr, "backfill_investment_trade_facts", fail)
    assert client.post("/api/ibkr/flex/sync", json={}).status_code == 500
    monkeypatch.setattr(ibkr_performance, "get_conn", lambda: conn)
    monkeypatch.setattr(ibkr_performance, "_latest_usd_cny_rate", lambda: None)
    http = client.get("/api/ibkr/performance")
    assert http.status_code == 200
    result = http.json()
    assert result["source_file"] == before.path.name
    assert result["report_quality"]["status"] == "review"
    assert risk_state._latest_ibkr_performance() == result
    assert result["net_liquidation_base"] == -119


def test_explicit_date_override_does_not_publish_a_fake_current_report(sync_case, tmp_path):
    client, conn, _ = sync_case
    active, _ = state.select_report(conn, tmp_path / "raw/ibkr")
    conn.rollback()
    response = client.post("/api/ibkr/flex/sync", json={"as_of": "2026-10-20"})
    assert response.status_code == 502
    assert "does not match IBKR report date" in response.json()["detail"]
    selected, _ = state.select_report(conn, tmp_path / "raw/ibkr")
    assert selected == active


def test_waiting_older_publisher_rechecks_after_newer_commit(sync_case, database, monkeypatch, tmp_path):
    _, conn, _ = sync_case
    monkeypatch.setattr(state, "utc_now", lambda: datetime(2026, 10, 23, 22, tzinfo=timezone.utc))
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(7362198401)")
    attempted = Event()
    result = {}
    old_conn = database()
    monkeypatch.setattr(collector, "fetch_flex_statement", lambda *a, **k: XML)
    original_save = collector.save_raw_flex_xml
    def saved(*args, **kwargs):
        path = original_save(*args, **kwargs)
        attempted.set()
        return path
    monkeypatch.setattr(collector, "save_raw_flex_xml", saved)
    def old_worker():
        try:
            result.update(collector.sync_ibkr_flex_to_db(
                old_conn, token="test", query_id="q", expected_statement_reference=date(2026, 10, 22),
            ))
        finally:
            old_conn.close()
    worker = Thread(target=old_worker)
    worker.start()
    try:
        assert attempted.wait(5)
        # Verify the second connection actually waits on the transaction advisory lock.
        waiting = False
        for _ in range(100):
            with conn.cursor() as cur:
                cur.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)", (old_conn.get_backend_pid(),))
                waiting = cur.fetchone()[0]
            if waiting:
                break
            time.sleep(.02)
        assert waiting
        monkeypatch.setattr(collector, "fetch_flex_statement", lambda *a, **k: XML.replace("20261022", "20261023"))
        newer = collector.sync_ibkr_flex_to_db(conn, token="test", query_id="q")
        assert newer["success"]
        worker.join(10)
        assert not worker.is_alive()
        assert result["reason"] == "report_date_regression"
        selected, _ = state.select_report(conn, tmp_path / "raw/ibkr")
        assert selected.as_of == date(2026, 10, 23)
    finally:
        conn.rollback()
        worker.join(10)
