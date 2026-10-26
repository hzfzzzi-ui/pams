"""Account overview binds proven shared values; it never revalues the account."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sys

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from endpoints import accounts
from services import ibkr_daily_close_read as reader, portfolio_state
from test_ibkr_daily_close_consumers import ACCOUNT, DAY, publish
from test_ibkr_sync_transaction import database as database

D = Decimal


def config(ratio="1"):
    return {"accounts": {"ibkr": {"account_type": "brokerage", "truth_source": "ibkr_flex",
            "base_currency": "USD", "owner_ratio": ratio}}}


def canonical(ratio="1"):
    return {"account_code": "ibkr", "truth_source": "ibkr_flex", "base_currency": "USD",
        "currency": "CNY", "amount": D(553) * D(ratio), "amount_cny": D(553) * D(ratio),
        "as_of": DAY, "review_status": "ready_for_review", "daily_close": {
            "status": "ok", "as_of": DAY.isoformat(), "requested_as_of": DAY.isoformat(),
            "carried": False, "source_sha256": "a" * 64, "source_account_id": ACCOUNT,
            "source_base_currency": "USD", "valuation_currency": "CNY", "owner_ratio": ratio,
            "owned_nav_base": str(D(79) * D(ratio)), "fx_rate": "7",
            "identity_evidence": {"status": "ok", "as_of": DAY.isoformat()},
            "fx_evidence": {"status": "ok", "as_of": DAY.isoformat(), "requested_as_of": DAY.isoformat(),
                            "currency": "USD", "base_currency": "CNY", "rate": "7", "source": "synthetic_fx"}}}


def build(*, cfg=None, shared=None, reviews=None, holding_day=DAY, holding_currency="USD"):
    return accounts._build_accounts_overview(cfg or config(), {}, {}, {}, {"ibkr": [
        {"currency": holding_currency, "market_value": D(100), "as_of": holding_day},
        {"currency": holding_currency, "market_value": D(-20), "as_of": holding_day},
    ]}, {}, reviews or {}, {}, canonical_balances=shared)[0]


def test_default_without_metadata_keeps_legacy_values_shape_and_review_fingerprint():
    row = build()
    assert "daily_close" not in row
    assert row["primary_balance"] is row["platform_balance"]
    assert row["primary_balance"] == {"amount": D(80), "currency": "USD", "as_of": DAY,
                                      "source": "platform_snapshot"}
    assert accounts._review_fingerprint(row) == f"ready_for_review|80|USD|{DAY}|platform_snapshot"
    approved = build(reviews={"ibkr": {"review_action": "approve",
                                     "review_fingerprint": accounts._review_fingerprint(row)}})
    assert approved["review_status"] == "approved"


@pytest.mark.parametrize("ratio", ["0", "0.25", "1", "1.5"])
def test_primary_uses_shared_owned_native_and_cny_amounts_once_preserving_raw_comparison(ratio):
    shared = {"ibkr": canonical(ratio)}
    before = deepcopy(shared)
    row = build(cfg=config(ratio), shared=shared)
    assert row["daily_close"]["status"] == "ok"
    assert row["primary_balance"] == {"amount": D(79) * D(ratio), "currency": "USD",
        "as_of": DAY.isoformat(), "source": "ibkr_flex", "amount_cny": D(553) * D(ratio)}
    assert row["platform_balance"]["amount"] == D(80) * D(ratio)
    assert row["platform_balance_gross"]["amount"] == 80
    assert [r["market_value"] for r in row["platform_balances"]] == [100, -20]
    assert shared == before


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("source", ["manual", "user_confirmation"])
def test_selected_manual_is_kept_even_if_shared_row_claims_a_valid_close(offset, source):
    cfg = config()
    cfg["accounts"]["ibkr"]["manual_balance"] = {"amount": "90", "currency": "USD",
        "as_of": (DAY + timedelta(days=offset)).isoformat(), "source": source}
    row = build(cfg=cfg, shared={"ibkr": canonical()})
    assert row["primary_balance"] is row["manual_balance"]
    assert row["primary_balance"]["amount"] == 90
    assert row["daily_close"]["status"] == "unconfirmed"


@pytest.mark.parametrize("truth", ["manual", "platform_snapshot", "official_statement"])
def test_non_flex_truth_stays_identical(truth):
    cfg = config()
    cfg["accounts"]["ibkr"]["truth_source"] = truth
    assert build(cfg=cfg, shared={"ibkr": canonical()}) == build(cfg=cfg)


@pytest.mark.parametrize("path,value", [
    (("account_code",), "other"), (("truth_source",), "manual"),
    (("as_of",), DAY + timedelta(days=1)), (("base_currency",), "EUR"),
    (("currency",), "USD"), (("review_status",), "missing_data"),
    (("amount_cny",), None), (("amount_cny",), "NaN"), (("amount_cny",), False),
    (("daily_close", "status"), "unconfirmed"), (("daily_close", "status"), "last_valid"),
    (("daily_close", "as_of"), "2024-02-07"), (("daily_close", "requested_as_of"), "2024-02-07"),
    (("daily_close", "carried"), True), (("daily_close", "source_sha256"), "bad"),
    (("daily_close", "source_account_id"), ""), (("daily_close", "source_base_currency"), "EUR"),
    (("daily_close", "valuation_currency"), "USD"), (("daily_close", "owner_ratio"), "0.5"),
    (("daily_close", "owned_nav_base"), None), (("daily_close", "owned_nav_base"), "Infinity"),
    (("daily_close", "identity_evidence", "status"), "unconfirmed"),
    (("daily_close", "identity_evidence"), ["ok"]),
    (("daily_close", "identity_evidence", "as_of"), "2024-02-07"),
    (("daily_close", "fx_rate"), "8"), (("daily_close", "fx_evidence", "status"), "unavailable"),
    (("daily_close", "fx_evidence", "as_of"), "2024-02-05"),
    (("daily_close", "fx_evidence", "requested_as_of"), "2024-02-07"),
    (("daily_close", "fx_evidence", "currency"), "EUR"),
    (("daily_close", "fx_evidence", "base_currency"), "USD"),
    (("daily_close", "fx_evidence", "source"), ""),
    (("daily_close", "fx_evidence", "rate"), "0"),
    (("daily_close", "fx_evidence"), ["ok"]), (("daily_close",), "invalid"),
])
def test_unknown_or_mismatched_proof_preserves_legacy_not_zero(path, value):
    shared = canonical()
    node = shared
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    row = build(shared={"ibkr": shared})
    assert row["primary_balance"] == build()["primary_balance"]
    assert row["primary_balance"]["amount"] == 80
    assert row["daily_close"]["status"] == "unconfirmed"
    assert row["daily_close"]["reasons"]


@pytest.mark.parametrize("shared", [{}, {"ibkr": {}}])
def test_missing_shared_balance_is_unconfirmed(shared):
    row = build(shared=shared)
    assert row["primary_balance"]["amount"] == 80
    assert row["daily_close"]["status"] == "unconfirmed"


@pytest.mark.parametrize("holding_day,holding_currency", [(DAY + timedelta(days=1), "USD"), (DAY, "EUR")])
def test_primary_date_and_currency_must_match(holding_day, holding_currency):
    row = build(shared={"ibkr": canonical()}, holding_day=holding_day, holding_currency=holding_currency)
    assert row["primary_balance"]["amount"] == 80
    assert row["daily_close"]["status"] == "unconfirmed"


def test_old_approval_is_stale_even_when_amount_is_unchanged_and_new_proof_is_reviewable():
    legacy = build()
    shared = canonical()
    shared["daily_close"]["owned_nav_base"] = "80"
    old_review = {"review_action": "approve", "review_fingerprint": accounts._review_fingerprint(legacy)}
    row = build(shared={"ibkr": shared}, reviews={"ibkr": old_review})
    assert row["primary_balance"]["amount"] == legacy["primary_balance"]["amount"]
    assert row["review_status"] == "stale"
    current = build(shared={"ibkr": shared})
    new_review = {"review_action": "approve", "review_fingerprint": accounts._review_fingerprint(current)}
    assert build(shared={"ibkr": shared}, reviews={"ibkr": new_review})["review_status"] == "approved"


@pytest.mark.parametrize("path,value", [(("source_sha256",), "b" * 64), (("source_account_id",), "OTHER_SYNTHETIC"),
    (("fx_evidence", "source"), "new_internal_fx"), (("fx_evidence", "rate"), "8"),
    (("owner_ratio",), "0.5"), (("status",), "unconfirmed")])
def test_review_fingerprint_binds_source_fx_owner_and_status_even_with_same_balance(path, value):
    row = build(shared={"ibkr": canonical()})
    fingerprint = accounts._review_fingerprint(row)
    changed = deepcopy(row)
    node = changed["daily_close"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    accounts._apply_review(changed, {"review_action": "approve", "review_fingerprint": fingerprint})
    assert changed["review_status"] == "stale"


def setup_payload(monkeypatch, *, configured=True, rows=None):
    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self, **kwargs):
            return self

    cur = Context()
    cfg = config()
    monkeypatch.setattr(accounts, "get_conn", lambda: cur)
    monkeypatch.setattr(accounts, "_load_accounts_config", lambda: cfg)
    monkeypatch.setattr(accounts, "_load_bank_config", lambda: {})
    for name in ("_fetch_db_accounts", "_fetch_statement_balances", "_fetch_latest_fx_rates_by_base",
                 "_fetch_transfer_rollups", "_fetch_latest_reviews"):
        monkeypatch.setattr(accounts, name, lambda _: {})
    monkeypatch.setattr(accounts, "_fetch_holding_balances", lambda _: {"ibkr": [
        {"currency": "USD", "market_value": D(80), "as_of": DAY}]})
    monkeypatch.setattr(reader, "configured", lambda: configured)
    calls = []

    def load(cursor, base, account_cfg):
        assert configured
        assert cursor is cur and base == "CNY" and account_cfg is cfg
        calls.append(1)
        return rows if rows is not None else [canonical()]

    monkeypatch.setattr(portfolio_state, "load_account_balances", load)
    return calls


@pytest.mark.parametrize("configured", [False, True])
def test_payload_calls_shared_loader_once_in_existing_connection_only_when_configured(monkeypatch, configured):
    calls = setup_payload(monkeypatch, configured=configured)
    row = accounts._overview_payload()["accounts"][0]
    assert len(calls) == int(configured)
    assert ("daily_close" in row) is configured
    assert row["primary_balance"]["amount"] == (79 if configured else 80)


def test_duplicate_canonical_accounts_are_not_silently_last_wins(monkeypatch):
    setup_payload(monkeypatch, rows=[canonical(), canonical(), canonical()])
    row = accounts._overview_payload()["accounts"][0]
    assert row["primary_balance"]["amount"] == 80
    assert row["daily_close"]["status"] == "unconfirmed"
    assert "duplicate_canonical_account" in row["daily_close"]["reasons"]


@pytest.mark.parametrize("ratio,missing_fx,manual_offset", [
    ("0", False, None), ("0.25", False, None), ("1", False, None), ("1", True, None),
    ("1", False, 0), ("1", False, 1),
])
def test_postgres_actual_readonly_overview_uses_shared_metadata_without_writes(
    database, tmp_path, monkeypatch, ratio, missing_fx, manual_offset,
):
    publish(tmp_path, monkeypatch)
    cfg = config(ratio)
    if manual_offset is not None:
        cfg["accounts"]["ibkr"]["manual_balance"] = {"amount": "90", "currency": "USD",
            "as_of": (DAY + timedelta(days=manual_offset)).isoformat()}
    with database() as conn, conn.cursor() as cur:
        cur.execute("""
            DROP SCHEMA public CASCADE; CREATE SCHEMA public;
            CREATE TABLE pams_accounts (account_code text, display_name text, base_currency text,
                                       is_asset boolean, active boolean, account_type text);
            INSERT INTO pams_accounts VALUES ('ibkr','Synthetic broker','USD',true,true,'brokerage');
            CREATE TABLE pams_source_row (source_table text, account_code text, source_system text,
                                          event_date date, imported_at timestamptz);
            CREATE TABLE pams_cashflow_fact (cashflow_id bigint, account_code text, event_date date,
                                             updated_at timestamptz, raw_json jsonb, review_status text);
            CREATE TABLE pams_holdings_snapshot (account_code text, currency text, asset_liability text,
                                                 as_of date, market_value numeric, cost numeric, holding_pnl numeric);
            CREATE TABLE pams_fx_rates_daily (base_currency text, quote_currency text, rate numeric,
                                              as_of date, source text);
            CREATE TABLE pams_ibkr_position_history (account_code text, as_of date, source_account_id text);
            CREATE TABLE pams_reconciliation_case (case_id bigint, raw_json jsonb, relation_code text,
                review_action text, status text, amount numeric, currency text, case_date date, note text,
                reviewed_by text, reviewed_at timestamptz, from_account_code text, to_account_code text, case_type text);
        """)
        cur.execute("INSERT INTO pams_holdings_snapshot VALUES ('ibkr','USD','asset',%s,100,90,10),"
                    "('ibkr','USD','liability',%s,-20,0,0)", (DAY, DAY))
        cur.execute("INSERT INTO pams_ibkr_position_history VALUES ('ibkr',%s,%s)", (DAY, ACCOUNT))
        if not missing_fx:
            cur.execute("INSERT INTO pams_fx_rates_daily VALUES ('CNY','USD',7,%s,'synthetic_internal_fx')", (DAY,))
        legacy_fingerprint = accounts._review_fingerprint(build(cfg=cfg))
        cur.execute("""INSERT INTO pams_reconciliation_case (case_id,case_type,review_action,status,raw_json,reviewed_at)
            VALUES (1,'account_review','approve','ready_for_review',%s,now())""",
            (psycopg2.extras.Json({"account_code": "ibkr", "review_fingerprint": legacy_fingerprint}),))
    monkeypatch.setattr(accounts, "_load_accounts_config", lambda: cfg)
    monkeypatch.setattr(accounts, "_load_bank_config", lambda: {})
    connections = []

    def readonly_connection():
        conn = database()
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        connections.append(conn)
        return conn

    monkeypatch.setattr(accounts, "get_conn", readonly_connection)
    original = portfolio_state.load_account_balances
    seen = []

    def observed(cur, base, cfg):
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()["transaction_read_only"] == "on"
        rows = original(cur, base, cfg)
        seen.extend(rows)
        return rows

    monkeypatch.setattr(portfolio_state, "load_account_balances", observed)
    try:
        row = accounts._overview_payload()["accounts"][0]
        assert len(seen) == 1 and len(connections) == 1
        if manual_offset is not None:
            assert row["primary_balance"] is row["manual_balance"]
            assert row["primary_balance"]["amount"] == D(90) * D(ratio)
            assert row["daily_close"]["status"] == "unconfirmed"
        elif missing_fx:
            assert row["primary_balance"]["amount"] == 80
            assert row["daily_close"]["status"] == "unconfirmed"
        else:
            assert row["primary_balance"]["amount"] == D(79) * D(ratio)
            assert row["primary_balance"]["amount_cny"] == seen[0]["amount_cny"] == D(553) * D(ratio)
            assert row["daily_close"]["owned_nav_base"] == str(D(79) * D(ratio))
            assert row["daily_close"]["status"] == "ok"
        assert row["platform_balance"]["amount"] == D(80) * D(ratio)
        assert accounts._norm(row)["primary_balance"]["currency"] == "USD"
        assert row["review_status"] == "stale"
    finally:
        for connection in connections:
            connection.close()
