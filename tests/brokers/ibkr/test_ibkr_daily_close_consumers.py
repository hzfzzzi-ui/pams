"""Daily-close consumers: generalized synthetic reports and disposable PG only."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import psycopg2.extras
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_daily_close as pure
from domain.brokers.ibkr import ibkr_daily_close_read as reader
from domain.investments import portfolio_state as portfolio
from domain.investments import valuation_nav as nav
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database

D = Decimal
DAY = date(2024, 2, 6)
ACCOUNT = "SYNTHETIC_CONSUMER_ACCOUNT"


def report(day=DAY, *, base="USD", gross_proof=True):
    root = ET.Element("FlexQueryResponse")
    statement = ET.SubElement(root, "FlexStatement", accountId=ACCOUNT,
                             fromDate=day.strftime("%Y%m%d"), toDate=day.strftime("%Y%m%d"))
    ET.SubElement(statement, "AccountInformation", accountId=ACCOUNT, currency=base)
    equity = ET.SubElement(statement, "EquitySummaryInBase")
    previous_day = day - timedelta(days=1)
    while previous_day.weekday() >= 5:
        previous_day -= timedelta(days=1)
    opening = {field: "0" for field in pure.COMPONENT_FIELDS + ("total",)}
    ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId=ACCOUNT,
                  currency=base, reportDate=previous_day.strftime("%Y%m%d"), **opening)
    values = {field: "0" for field in pure.COMPONENT_FIELDS}
    values.update(cash="-20", stock="100", interestAccruals="-3", dividendAccruals="2")
    values["total"] = str(sum(map(D, values.values())))
    if gross_proof:
        for field, value in list(values.items()):
            if field != "total":
                values[field + "Long"] = str(max(D(value), D(0)))
                values[field + "Short"] = str(min(D(value), D(0)))
        values.update(totalLong="102", totalShort="-23")
    ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId=ACCOUNT,
                  currency=base, reportDate=day.strftime("%Y%m%d"), **values)
    positions = ET.SubElement(statement, "OpenPositions")
    ET.SubElement(positions, "OpenPosition", accountId=ACCOUNT, currency="EUR",
                  conid="SYNTHETIC_CONTRACT", reportDate=day.strftime("%Y%m%d"))
    flow = {field: "0" for field in pure.FLOW_FIELDS}
    flow.update(startingValue="0", endingValue="79")
    ET.SubElement(statement, "ChangeInNAV", accountId=ACCOUNT, currency=base,
                  fromDate=day.strftime("%Y%m%d"), toDate=day.strftime("%Y%m%d"), **flow)
    return ET.tostring(root, encoding="utf-8")


def publish(tmp_path, monkeypatch, *, day=DAY, gross_proof=True):
    content = report(day, gross_proof=gross_proof)
    name = hashlib.sha256(content).hexdigest() + ".xml"
    (tmp_path / name).write_bytes(content)
    manifest = {"version": 1, "purpose": "ibkr_daily_close", "account_code": "ibkr",
                "source_file": name, "source_sha256": hashlib.sha256(content).hexdigest(),
                "source_account_id": ACCOUNT, "as_of": day.isoformat(), "period_start": day.isoformat()}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv(reader.MANIFEST_ENV, str(path))
    return reader.read_close(as_of=day)


def balance(day=DAY, *, account="ibkr"):
    return {"account_code": account, "amount": D(760), "currency": "CNY", "amount_cny": D(760),
            "as_of": day, "include_in_nav": True, "review_status": "ready_for_review", "truth_source": "ibkr_flex"}


def position(day=DAY, *, account="ibkr", value="760"):
    return {"account_code": account, "as_of": day, "market_value_cny": D(value)}


class RateCursor:
    def __init__(self, rate="7", day=DAY, identities=None):
        self.rate = rate
        self.day = day
        self.identities = [ACCOUNT] if identities is None else identities
        self.mode = None
        self.calls = []

    def execute(self, query, params=None):
        if "to_regclass" in query:
            self.mode = "exists"
            return
        if "SELECT DISTINCT source_account_id" in query:
            assert params == ("ibkr", self.day)
            self.mode = "identities"
            return
        self.mode = "fx"
        assert "as_of <= %s" in query
        assert params == ("CNY", "USD", self.day)
        self.calls.append((query, params))

    def fetchone(self):
        if self.mode == "exists":
            return {"exists": True}
        return None if self.rate is None else {"rate": self.rate, "as_of": self.day, "source": "synthetic_internal_fx"}

    def fetchall(self):
        assert self.mode == "identities"
        return [{"source_account_id": value} for value in self.identities]


@pytest.fixture(autouse=True)
def simulate_reviewed_identity_for_pure_cursor(monkeypatch):
    """Only the small pure cursor substitutes the separately tested source verifier."""
    real = nav.daily_close_account_identity
    def resolve(cur, *, source_account_id, as_of):
        if not isinstance(cur, RateCursor):
            return real(cur, source_account_id=source_account_id, as_of=as_of)
        values=cur.identities
        if not values or any(not isinstance(v,str) or not v.strip() for v in values):
            return {'status':'unconfirmed','reasons':['missing_native_account_identity']}
        if len(set(values))!=1:
            return {'status':'unconfirmed','reasons':['ambiguous_native_account_identity']}
        if values[0]!=source_account_id:
            return {'status':'unconfirmed','reasons':['native_account_identity_mismatch']}
        return {'status':'ok','basis':'synthetic_review','as_of':as_of.isoformat(),'authentication':'unknown'}
    monkeypatch.setattr(nav,'daily_close_account_identity',resolve)


def test_unset_preserves_legacy_without_evidence_reads(monkeypatch):
    monkeypatch.delenv(reader.MANIFEST_ENV, raising=False)
    monkeypatch.setattr(reader, "load_evidence", lambda: pytest.fail("must not read supplemental evidence"))
    before = balance()
    row = deepcopy(before)
    portfolio._apply_daily_close_balance(object(), row, base_currency="CNY", owner_ratio=D(1))
    assert row == before
    legacy = {DAY: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}}
    assert nav._overlay_ibkr_daily_closes(object(), values=legacy, base_currency="CNY", account_owner_ratio=None) is legacy
    assert nav.current_nav_bucket_from_parts(account_balances=[row], investment_positions=[position()]) == {
        "assets": D(760), "liabilities": D(0), "net_worth": D(760)}


@pytest.mark.parametrize("truth_source", ["manual", "bank_statement", "platform_snapshot", None])
def test_configured_manifest_does_not_override_non_flex_account_truth(tmp_path, monkeypatch, truth_source):
    publish(tmp_path, monkeypatch)
    row = {**balance(), "truth_source": truth_source}
    before = deepcopy(row)
    monkeypatch.setattr(reader, "read_close", lambda **kwargs: pytest.fail("manual truth must not read daily close"))
    portfolio._apply_daily_close_balance(object(), row, base_currency="CNY", owner_ratio=D(1))
    assert row == before


def test_selected_manual_balance_is_not_overridden_even_under_flex_account_config(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    row = balance()
    before = deepcopy(row)
    monkeypatch.setattr(reader, "read_close", lambda **kwargs: pytest.fail("selected manual balance is not ours"))
    portfolio._apply_daily_close_balance(object(), row, base_currency="CNY", owner_ratio=D(1), selected_source="manual")
    assert row == before


@pytest.mark.parametrize("ratio", ["0", "0.25", "1"])
def test_account_nav_uses_internal_base_fx_owner_config_and_gross_proof(tmp_path, monkeypatch, ratio):
    publish(tmp_path, monkeypatch)
    row = balance()
    row["amount"] *= D(ratio)
    row["amount_cny"] *= D(ratio)
    cur = RateCursor()
    portfolio._apply_daily_close_balance(cur, row, base_currency="CNY", owner_ratio=D(ratio))
    factor = D(ratio)
    assert row["amount_cny"] == D(553) * factor
    proof = row["daily_close"]
    assert proof["status"] == "ok" and proof["owner_ratio"] == ratio
    assert D(proof["assets_cny"]) == D(714) * factor
    assert D(proof["liabilities_cny"]) == D(161) * factor
    assert D(proof["native_fx_vs_base_fx_bridge_cny"]) == D(200) * factor
    assert D(proof["accrual_and_other_components_cny"]) == D(-7) * factor
    assert D(proof["nav_adjustment_cny"]) == D(-207) * factor
    positions = [position(value=str(D(760) * factor)), position(account="other", value="50")]
    old = deepcopy(positions)
    bucket = nav.current_nav_bucket_from_parts(account_balances=[row], investment_positions=positions)
    assert bucket == {"assets": D(714) * factor + 50, "liabilities": D(161) * factor,
                      "net_worth": D(553) * factor + 50,
                      "ibkr_daily_close": {**proof, "net_worth_cny": str(row["amount_cny"])}}
    assert D(proof["owned_nav_base"]) == D(79) * factor
    assert nav.current_nav_account_values_from_parts(account_balances=[row], investment_positions=positions) == {
        "ibkr": D(553) * factor, "other": D(50)}
    assert positions == old and len(cur.calls) == 1


@pytest.mark.parametrize("kind", ["missing_file", "missing_fx", "future_fx", "gross", "date", "account"])
def test_configured_unavailable_preserves_legacy_and_marks_unconfirmed(tmp_path, monkeypatch, kind):
    publish(tmp_path, monkeypatch, gross_proof=kind != "gross")
    row = balance(DAY + timedelta(days=1) if kind == "date" else DAY,
                  account="other" if kind == "account" else "ibkr")
    before = deepcopy(row)
    if kind == "missing_file":
        for path in tmp_path.glob("*.xml"):
            path.unlink()
    cur = RateCursor(None if kind in {"missing_fx", "future_fx"} else "7")
    portfolio._apply_daily_close_balance(cur, row, base_currency="CNY", owner_ratio=D(1))
    assert {key: row[key] for key in before} == before
    if kind == "account":
        assert row == before
    else:
        assert row["daily_close"]["status"] == "unconfirmed"
        assert row["daily_close"]["reasons"]


@pytest.mark.parametrize("field,value", [("long", "-1"), ("short", "1"), ("short", "-30"), ("long", "NaN")])
def test_malformed_gross_proof_is_never_replaced_with_net_split(tmp_path, monkeypatch, field, value):
    close = publish(tmp_path, monkeypatch)
    close["component_proof"]["subcomponent_checks"]["total_long_short"][field] = D(value)
    assert nav.daily_close_nav_bucket(close, rate=D(7), owner_ratio=D(1)) is None


def test_account_correction_is_not_synthetic_cash_or_carried_to_newer_position_dates(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    row = balance()
    portfolio._apply_daily_close_balance(RateCursor(), row, base_currency="CNY", owner_ratio=D(1))
    cfg = {"accounts": {"ibkr": {"account_type": "brokerage", "base_currency": "USD"}}}
    assert portfolio._synthetic_broker_cash_positions(
        base_currency="CNY", account_cfg=cfg, fx_rates={"USD": D(7)},
        account_position_value_cny={"ibkr": D(760)}, account_position_as_of={"ibkr": DAY},
        account_balances=[row]) == []
    newer = [position(DAY + timedelta(days=1), value="800")]
    assert nav.current_nav_account_values_from_parts(account_balances=[row], investment_positions=newer)["ibkr"] == 800
    assert nav.current_nav_bucket_from_parts(account_balances=[row], investment_positions=newer)["net_worth"] == 800


def test_series_uses_cached_bulk_reader_and_explicit_last_valid_metadata(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    calls = []
    original = reader.read_series
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(reader, "read_series", observed)
    dates = [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)]
    legacy = {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)} for day in dates}
    result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                          account_owner_ratio=lambda account: D("0.5"))
    assert calls == [1]
    assert result[DAY]["net_worth"] == D("276.5")
    assert result[DAY]["assets"] == 357 and result[DAY]["liabilities"] == D("80.5")
    assert result[dates[0]]["net_worth"] == 760 and result[dates[0]]["daily_close"]["status"] == "unconfirmed"
    assert result[dates[2]]["net_worth"] == D("276.5")
    assert result[dates[2]]["daily_close"]["status"] == "last_valid"
    assert result[dates[2]]["daily_close"]["requested_as_of"] == dates[2].isoformat()
    assert result[dates[2]]["daily_close"]["as_of"] == DAY.isoformat()
    rows = nav.build_weekly_nav_rows(dates=dates, bank_series={}, snapshot_series={}, a_share_series={}, ibkr_values=result)
    payload = nav.format_weekly_nav_payload(start_date=dates[0], terminal_date=dates[-1], base_currency="CNY", rows=rows)
    assert payload["rows"][1]["ibkr_daily_close"]["status"] == "ok"
    assert payload["rows"][2]["ibkr_daily_close"]["status"] == "last_valid"
    json.dumps(payload)


@pytest.mark.parametrize("ratio", ["0", "0.25", "1"])
def test_series_resolves_owner_ratio_once_without_changing_dates_or_ownership(tmp_path, monkeypatch, ratio):
    publish(tmp_path, monkeypatch)
    calls = []
    def resolve(account):
        calls.append(account)
        return D(ratio)
    dates = [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    legacy = {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)} for day in dates}
    before = deepcopy(legacy)
    result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                          account_owner_ratio=resolve)
    assert calls == ["ibkr"] and legacy == before
    assert result[dates[0]]["net_worth"] == 760
    assert result[dates[0]]["daily_close"]["status"] == "unconfirmed"
    for day in dates[1:]:
        assert result[day]["assets"] == D(714) * D(ratio)
        assert result[day]["liabilities"] == D(161) * D(ratio)
        assert result[day]["net_worth"] == D(553) * D(ratio)
        proof = result[day]["daily_close"]
        assert proof["owner_ratio"] == ratio
        assert proof["status"] == ("ok" if day == DAY else "last_valid")
        assert proof["requested_as_of"] == day.isoformat() and proof["as_of"] == DAY.isoformat()
        assert proof["age_days"] == (day - DAY).days


@pytest.mark.parametrize("failure", [ValueError, TypeError, InvalidOperation])
def test_series_invalid_owner_ratio_is_resolved_once_and_preserves_legacy(tmp_path, monkeypatch, failure):
    publish(tmp_path, monkeypatch)
    calls = []
    def resolve(account):
        calls.append(account)
        raise failure("synthetic invalid owner config")
    dates = [DAY, DAY + timedelta(days=1)]
    legacy = {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)} for day in dates}
    result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                          account_owner_ratio=resolve)
    assert calls == ["ibkr"]
    for day in dates:
        assert result[day] == {**legacy[day], "daily_close": {
            "status": "unconfirmed", "as_of": day.isoformat(), "requested_as_of": day.isoformat(),
            "carried": False, "reasons": ["invalid_owner_ratio"]}}


@pytest.mark.parametrize("ratio", [None, "not-a-number", "NaN", "Infinity", "-1"])
def test_series_keeps_existing_invalid_value_reason_and_never_substitutes_zero(tmp_path, monkeypatch, ratio):
    publish(tmp_path, monkeypatch)
    calls = []
    def resolve(account):
        calls.append(account)
        return ratio
    dates = [DAY, DAY + timedelta(days=1)]
    legacy = {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)} for day in dates}
    result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                          account_owner_ratio=resolve)
    assert calls == ["ibkr"]
    reason = "invalid_owner_ratio" if ratio in (None, "not-a-number") else "missing_gross_nav_proof"
    for day in dates:
        assert all(result[day][key] == value for key, value in legacy[day].items())
        assert result[day]["daily_close"]["status"] == "unconfirmed"
        assert result[day]["daily_close"]["reasons"] == [reason]


def test_series_owner_ratio_result_and_failure_do_not_leak_into_next_invocation(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    configured = {"ratio": "0.25"}
    calls = []
    def resolve(account):
        calls.append(account)
        return configured["ratio"]
    legacy = {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}
              for day in (DAY, DAY + timedelta(days=1))}
    for ratio in ("0.25", "bad", "1", "0"):
        configured["ratio"] = ratio
        result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                              account_owner_ratio=resolve)
        if ratio == "bad":
            assert result[DAY]["net_worth"] == 760
            assert result[DAY]["daily_close"]["reasons"] == ["invalid_owner_ratio"]
        else:
            assert result[DAY]["net_worth"] == D(553) * D(ratio)
            assert result[DAY]["daily_close"]["status"] == "ok"
    assert calls == ["ibkr"] * 4


@pytest.mark.parametrize("case", ["disabled", "empty", "before_evidence", "identity", "evidence"])
def test_series_does_not_resolve_owner_ratio_without_eligible_evidence(tmp_path, monkeypatch, case):
    publish(tmp_path, monkeypatch)
    day = DAY - timedelta(days=1) if case == "before_evidence" else DAY
    legacy = {} if case == "empty" else {day: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}}
    if case == "disabled":
        monkeypatch.delenv(reader.MANIFEST_ENV)
    elif case == "evidence":
        monkeypatch.setattr(reader, "read_series", lambda: {"daily_closes": {}, "reasons": ["source_sha256_mismatch"]})
    cur = RateCursor(identities=[] if case == "identity" else None)
    result = nav._overlay_ibkr_daily_closes(cur, values=legacy, base_currency="CNY",
        account_owner_ratio=lambda account: pytest.fail("ineligible dates must not read owner config"))
    assert all(all(result[key][field] == value for field, value in row.items()) for key, row in legacy.items())


def test_series_missing_resolver_and_unexpected_exception_keep_existing_behavior(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    legacy = {DAY: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}}
    result = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY", account_owner_ratio=None)
    assert result[DAY]["net_worth"] == 760
    assert result[DAY]["daily_close"]["reasons"] == ["missing_owner_ratio"]
    def unavailable(account):
        raise RuntimeError("synthetic unexpected config failure")
    with pytest.raises(RuntimeError, match="synthetic unexpected config failure"):
        nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY", account_owner_ratio=unavailable)


@pytest.mark.parametrize("offset", [1, 2, 3])
def test_weekend_and_missing_weekday_use_source_day_fx_and_are_not_green(tmp_path, monkeypatch, offset):
    source_day = date(2024, 2, 9)
    publish(tmp_path, monkeypatch, day=source_day)
    requested = source_day + timedelta(days=offset)
    cur = RateCursor(day=source_day)
    legacy = {requested: {"assets": D(9999), "liabilities": D(100), "net_worth": D(9899)}}
    values = nav._overlay_ibkr_daily_closes(cur, values=legacy, base_currency="CNY",
                                          account_owner_ratio=lambda account: D(1))
    value = values[requested]
    assert value["net_worth"] == 553
    metadata = value["daily_close"]
    assert metadata["status"] == "last_valid" and metadata["carried"] is True
    assert metadata["as_of"] == source_day.isoformat() and metadata["requested_as_of"] == requested.isoformat()
    assert metadata["age_days"] == offset and metadata["reasons"] == ["missing_closing_date"]
    assert metadata["fx_evidence"]["as_of"] == source_day.isoformat()
    assert metadata["fx_evidence"]["source"] == "synthetic_internal_fx"
    assert len(cur.calls) == 1


def test_rejected_weekday_carries_prior_proven_close_with_original_reason(tmp_path, monkeypatch):
    close = publish(tmp_path, monkeypatch)
    requested = DAY + timedelta(days=1)
    monkeypatch.setattr(reader, "read_series", lambda: {"daily_closes": {
        DAY.isoformat(): close, requested.isoformat(): {"status": "unavailable", "reasons": ["missing_amount:cash"]}}})
    legacy = {requested: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}}
    value = nav._overlay_ibkr_daily_closes(RateCursor(), values=legacy, base_currency="CNY",
                                         account_owner_ratio=lambda account: D(1))[requested]
    assert value["net_worth"] == 553 and value["daily_close"]["status"] == "last_valid"
    assert value["daily_close"]["reasons"] == ["missing_amount:cash"]


def test_expired_fx_metadata_survives_and_does_not_reconfirm_legacy_value(tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    class ExpiredCursor(RateCursor):
        def fetchone(self):
            if self.mode == "exists":
                return super().fetchone()
            return {"rate": "7", "as_of": DAY - timedelta(days=1), "source": "synthetic_old_rate"}
    row = balance()
    portfolio._apply_daily_close_balance(ExpiredCursor(), row, base_currency="CNY", owner_ratio=D(1))
    assert row["amount_cny"] == 760
    assert row["daily_close"]["status"] == "unconfirmed"
    assert row["daily_close"]["reasons"] == ["expired_historical_fx"]
    assert row["daily_close"]["fx_evidence"]["as_of"] == (DAY - timedelta(days=1)).isoformat()
    assert row["daily_close"]["fx_evidence"]["source"] == "synthetic_old_rate"


@pytest.mark.parametrize("identities,reason", [
    ([], "missing_native_account_identity"), ([None], "missing_native_account_identity"),
    (["OTHER_NATIVE_ACCOUNT"], "native_account_identity_mismatch"),
    ([ACCOUNT, "OTHER_NATIVE_ACCOUNT"], "ambiguous_native_account_identity"),
])
def test_manifest_label_cannot_substitute_native_account_mapping(tmp_path, monkeypatch, identities, reason):
    publish(tmp_path, monkeypatch)
    row = balance()
    portfolio._apply_daily_close_balance(RateCursor(identities=identities), row,
                                         base_currency="CNY", owner_ratio=D(1))
    assert row["amount_cny"] == 760 and row["daily_close"]["status"] == "unconfirmed"
    assert row["daily_close"]["reasons"] == [reason]
    legacy = {DAY: {"assets": D(900), "liabilities": D(140), "net_worth": D(760)}}
    value = nav._overlay_ibkr_daily_closes(RateCursor(identities=identities), values=legacy,
        base_currency="CNY", account_owner_ratio=lambda account: D(1))[DAY]
    assert value["net_worth"] == 760 and value["daily_close"]["status"] == "unconfirmed"
    assert value["daily_close"]["reasons"] == [reason]


@pytest.fixture
def consumer_database(database, monkeypatch):
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    conn = database()
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED','true')
    initialize_isolated_sources(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,rate,as_of,source) VALUES ('CNY','USD',7,%s,'synthetic'),('CNY','EUR',9,%s,'synthetic'),('CNY','USD',99,%s,'synthetic')",
                    (DAY, DAY, DAY + timedelta(days=1)))
        cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,as_of,currency,market_value,cost,
            asset_liability,major_category,minor_category,symbol,name,quantity) VALUES
            ('ibkr',%s,'USD',-20,-20,'负债','现金','现金/活期','CASH_USD','Cash USD',-20),
            ('ibkr',%s,'EUR',100,80,'资产','股票','股票','SYNTHETIC','Synthetic',2)""", (DAY, DAY))
    conn.commit()
    yield conn
    conn.close()


def publish_account_binding(conn, account=ACCOUNT):
    from uuid import uuid4
    from psycopg2.extras import RealDictCursor
    from domain.imports.ibkr_holding_source import stage_report
    from domain.brokers.ibkr.ibkr_holding_review import review, review_account_binding, publish as publish_source
    from tests.database.test_ibkr_holding_identity_review import XML
    content=XML.replace(b'U_TEST',account.encode()).replace(b'20261022',DAY.strftime('%Y%m%d').encode())
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        source=stage_report(cur,batch_id=uuid4(),account_code='ibkr',source_account_id=account,business_date=DAY,content=content)
        cur.execute('SELECT revision FROM source_batches WHERE batch_id=%s',(source['batch_id'],))
        revision=cur.fetchone()['revision']
        revision=review(cur,batch_id=source['batch_id'],item_id=source['position_item_ids'][0],expected_revision=revision,note='synthetic row')['revision']
        revision=review(cur,batch_id=source['batch_id'],item_id=source['scope_item_id'],expected_revision=revision,note='synthetic scope',scope_complete=True)['revision']
        revision=review_account_binding(cur,batch_id=source['batch_id'],source_account_id=account,expected_revision=revision,note='synthetic account')['revision']
        publish_source(cur,batch_id=source['batch_id'],expected_revision=revision)
    conn.commit()


def test_real_pg_consumers_are_readonly_ownerratio_exact_and_native_positions_unchanged(consumer_database, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    conn = consumer_database
    publish_account_binding(conn)
    cfg = {"accounts": {"ibkr": {"account_type": "brokerage", "truth_source": "ibkr_flex",
                                "owner_ratio": "0.4", "base_currency": "USD"}}}
    monkeypatch.setattr(portfolio, "_view_as_of", lambda: DAY)
    with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        before = portfolio.load_investment_positions(cur, "CNY", cfg)
        balances = portfolio.load_account_balances(cur, "CNY", cfg)
        after = portfolio.load_investment_positions(cur, "CNY", cfg, account_balances=balances)
        assert before == after
        row=balance()
        portfolio._apply_daily_close_balance(cur,row,base_currency="CNY",owner_ratio=D("0.4"))
        assert row["amount_cny"] == D("221.2")
        assert D(row["daily_close"]["fx_rate"]) == D("7")
        assert balances[0]["amount_cny"] == D("221.2")
        assert balances[0]["snapshot_scope_status"] == "unknown"
        values = nav._ibkr_nav_series(cur, dates=[DAY], base_currency="CNY",
            owner_ratio_case_sql=lambda alias: ("%s", [D("0.4")]), account_owner_ratio=lambda account: D("0.4"))
        assert values[DAY]["net_worth"] == D("221.2")
        assert values[DAY]["assets"] == D("285.6") and values[DAY]["liabilities"] == D("64.4")
        cur.execute('SELECT count(*) AS n FROM pams_holdings_snapshot')
        assert cur.fetchone()["n"] == 2


def test_real_pg_future_only_fx_is_not_used_for_official_close(consumer_database, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    conn = consumer_database
    publish_account_binding(conn)
    with conn, conn.cursor() as cur:
        cur.execute('DELETE FROM pams_fx_rates_daily WHERE as_of <= %s', (DAY,))
    with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        row = balance()
        portfolio._apply_daily_close_balance(cur, row, base_currency="CNY", owner_ratio=D(1))
        assert row["amount_cny"] == 760
        assert row["daily_close"]["status"] == "unconfirmed"
        assert row["daily_close"]["reasons"] == ["missing_historical_fx"]


@pytest.mark.parametrize("native", [None, "OTHER_NATIVE_ACCOUNT"])
def test_real_pg_missing_or_cross_account_native_identity_blocks_override(consumer_database, tmp_path, monkeypatch, native):
    publish(tmp_path, monkeypatch)
    conn = consumer_database
    if native is not None:
        publish_account_binding(conn,native)
    with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        row = balance()
        portfolio._apply_daily_close_balance(cur, row, base_currency="CNY", owner_ratio=D(1))
        assert row["amount_cny"] == 760 and row["daily_close"]["status"] == "unconfirmed"
        expected = "missing_native_account_identity" if native is None else "native_account_identity_mismatch"
        assert row["daily_close"]["reasons"] == [expected]
