from datetime import date, datetime
from contextlib import nullcontext
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend/api"))
from domain.brokers.ibkr import ibkr_daily_close as pure
from domain.brokers.ibkr import ibkr_daily_close_read as reader
from domain.brokers.ibkr import ibkr_performance as performance

D = Decimal
DAY = date(2024, 1, 2)
ACCOUNT = "SYNTHETIC_ACCOUNT"


def xml():
    root = ET.Element("FlexQueryResponse")
    statement = ET.SubElement(root, "FlexStatement", accountId=ACCOUNT, fromDate="20240101", toDate="20240102")
    ET.SubElement(statement, "AccountInformation", accountId=ACCOUNT, currency="USD")
    equity = ET.SubElement(statement, "EquitySummaryInBase")
    for day, cash, interest, dividend in (("20231229", "50", "0", "0"), ("20240102", "100", "-2", "1")):
        values = {key: "0" for key in pure.COMPONENT_FIELDS}
        values.update(cash=cash, interestAccruals=interest, dividendAccruals=dividend)
        values["total"] = str(sum(map(D, values.values())))
        for key, value in list(values.items()):
            values[key + "Long"] = str(max(D(value), D(0)))
            values[key + "Short"] = str(min(D(value), D(0)))
        ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId=ACCOUNT,
            currency="USD", reportDate=day, **values)
    ET.SubElement(statement, "OpenPositions")
    flow = {key: "0" for key in pure.FLOW_FIELDS}
    flow.update(startingValue="50", endingValue="99", depositsWithdrawals="20")
    ET.SubElement(statement, "ChangeInNAV", accountId=ACCOUNT, currency="USD",
                  fromDate="20240101", toDate="20240102", **flow)
    return ET.tostring(root, encoding="utf-8")


def publisher():
    spec = importlib.util.spec_from_file_location("daily_close_cli", ROOT / "scripts/ibkr_daily_close.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def published(tmp_path, monkeypatch):
    output = publisher().publish(xml(), directory=tmp_path, account_id=ACCOUNT,
                                  as_of=DAY, period_start=date(2024, 1, 1))
    monkeypatch.setenv(reader.MANIFEST_ENV, output["manifest"])
    return tmp_path / "daily-close.json"


def test_unconfigured_is_explicit_and_legacy_is_identical(monkeypatch):
    monkeypatch.delenv(reader.MANIFEST_ENV, raising=False)
    assert reader.load_evidence()["reasons"] == ["not_configured"]
    payload = {"legacy": 1}
    assert performance.apply_daily_close(payload, Path("must-not-read")) is payload


def test_manifest_read_proven_close_and_period_without_db(published):
    result = reader.read_close(as_of=DAY, account_id=ACCOUNT, period_start=date(2024, 1, 1))
    assert result["status"] == "ok"
    assert result["nav_base"] == 99
    assert result["period"]["pnl_base"] == 29
    result["period"]["pnl_base"] = 999
    assert reader.read_close(as_of=DAY, account_id=ACCOUNT, period_start=date(2024, 1, 1))["period"]["pnl_base"] == 29


@pytest.mark.parametrize("change,reason", [
    ({"source_file": "../source.xml"}, "invalid_source_path"),
    ({"account_code": "other"}, "manifest_scope_mismatch"),
    ({"purpose": "positions"}, "manifest_scope_mismatch"),
    ({"source_sha256": "0" * 64}, "source_sha256_mismatch"),
    ({"as_of": "2999-01-01"}, "future_evidence"),
])
def test_manifest_rejects_untrusted_bindings(published, change, reason):
    manifest = json.loads(published.read_text())
    manifest.update(change)
    published.write_text(json.dumps(manifest))
    assert reader.load_evidence()["reasons"] == [reason]


def test_raw_tampering_is_not_hidden_by_parse_cache(published):
    assert reader.read_close(as_of=DAY)["status"] == "ok"
    raw = published.parent / json.loads(published.read_text())["source_file"]
    raw.write_bytes(xml() + b" ")
    assert reader.read_close(as_of=DAY)["reasons"] == ["source_sha256_mismatch"]


def test_wrong_account_or_uncovered_day_does_not_carry_latest(published):
    assert reader.read_close(as_of=DAY, account_id="OTHER")["reasons"] == ["account_mismatch"]
    assert reader.read_close(as_of=date(2024, 1, 3))["reasons"] == ["missing_closing_date"]


def legacy():
    return {"as_of": DAY.isoformat(), "base_currency": "USD", "net_liquidation_base": 100.0,
        "net_deposit_base": 20.0, "total_pnl_base": 80.0, "cash_base": 100.0,
        "position_value_base": 0.0, "unrealized_pnl_base": 0.0, "realized_pnl_base": 1.0,
        "other_pnl_base": 79.0, "gross_exposure_base": 0.0, "negative_cash_base": 0.0,
        "net_liquidation_cny": 777.0, "total_pnl_cny": 666.0, "fx_rate_to_cny": 7.77}


def fx_proof(**change):
    return {"status": "ok", "rate": D(7), "as_of": DAY.isoformat(), "source": "synthetic_fx",
            "requested_as_of": DAY.isoformat(), "currency": "USD", "base_currency": "CNY", "age_days": 0,
            **change}


def test_overlay_accrual_does_not_double_subtract_cash_or_duplicate_capital(published):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    before = legacy()
    result = reader.overlay_performance(before, account_id=ACCOUNT, close=close, fx_rate=D("7"),
                                        fx_evidence=fx_proof())
    assert result["net_liquidation_base"] == 99
    assert result["total_pnl_base"] == 29
    assert result["return_ratio"] == pytest.approx(29 / 70)
    assert result["return_capital_base"] == 70
    assert result["return_capital_cny"] == 490
    assert result["cash_base"] == 100
    assert result["net_deposit_base"] == 20
    assert result["total_pnl_cny"] == 203
    assert result["daily_close"]["nav_basis"] == "official_nav_total"
    assert result["daily_close"]["return_ratio_basis"] == "period_pnl_over_opening_nav_plus_net_external_flows_not_twr_or_mwr"
    assert result["daily_close"]["return_ratio_status"] == "defined"
    assert before == legacy()


def test_missing_fx_never_leaves_stale_cny_or_invents_one(published):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    result = reader.overlay_performance(legacy(), account_id=ACCOUNT, close=close)
    assert not any(key.endswith("_cny") for key in result)
    assert result["daily_close"]["fx_status"] == "unavailable"
    assert result["return_capital_base"] == 70
    assert "return_capital_cny" not in result


def test_actual_performance_consumer_uses_same_date_scope_and_internal_fx(published):
    source = published.parent / "production.xml"
    source.write_bytes(xml())
    class Cursor:
        def execute(self, sql, params):
            assert "as_of <= %s" in sql and params == ("CNY", "USD", DAY)
        def fetchone(self):
            return (D(7), DAY, "synthetic_fx")
    result = performance.apply_daily_close(legacy(), source, cur=Cursor())
    assert result["net_liquidation_base"] == 99
    assert result["total_pnl_base"] == 29
    assert result["net_liquidation_cny"] == 693


def test_production_component_conflict_is_not_silently_overwritten(published):
    source = published.parent / "production.xml"
    source.write_bytes(xml())
    before = {**legacy(), "position_value_base": 200.0}
    result = performance.apply_daily_close(before, source, cur=object())
    assert result["net_liquidation_base"] == before["net_liquidation_base"]
    assert result["daily_close"]["reasons"] == ["production_close_components_mismatch"]


@pytest.mark.parametrize("row", [None, {"rate": "0"}, {"rate": "NaN"}, {"rate": "-1"}])
def test_historical_fx_missing_invalid_is_unknown(row):
    class Cursor:
        def execute(self, sql, params):
            assert "as_of <= %s" in sql and params[-1] == DAY
        def fetchone(self):
            return row
    assert reader.historical_rate(Cursor(), currency="USD", base_currency="CNY", as_of=DAY) is None


def test_publish_rejects_date_regression_and_keeps_immutable_original(published):
    before = published.read_bytes()
    with pytest.raises(ValueError):
        publisher().publish(xml(), directory=published.parent, account_id="OTHER",
                              as_of=DAY, period_start=date(2024, 1, 1))
    assert published.read_bytes() == before
    digest = hashlib.sha256(xml()).hexdigest()
    assert (published.parent / (digest + ".xml")).read_bytes() == xml()


def test_invalid_close_retains_entire_legacy_payload_and_labels_financial_basis():
    original = {**legacy(), "status": "ok", "custom": {"preserve": True}}
    result = reader.overlay_performance(original, account_id=ACCOUNT,
                                        close={"status": "unavailable", "reasons": ["missing_evidence"]})
    assert {k: v for k, v in result.items() if k != "daily_close"} == original
    assert result["daily_close"] == {"status": "unavailable", "reasons": ["missing_evidence"],
                                     "value_basis": "legacy_cash_plus_positions_unconfirmed"}


@pytest.mark.parametrize("field,value", [("cash_base", D("NaN")), ("position_value_base", None),
                                       ("unrealized_pnl_base", D("Infinity")), ("negative_cash_base", -1)])
def test_overlay_rejects_invalid_subject_without_partial_replacement(published, field, value):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    before = {**legacy(), field: value}
    result = reader.overlay_performance(before, account_id=ACCOUNT, close=close, fx_evidence=fx_proof())
    assert result["daily_close"]["status"] == "unavailable"
    assert result["net_liquidation_base"] == before["net_liquidation_base"]
    assert result["net_liquidation_cny"] == before["net_liquidation_cny"]
    assert result["total_pnl_base"] == before["total_pnl_base"]


def test_overlay_revalidates_cash_and_positions_even_if_wrapper_did_not(published):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    before = {**legacy(), "cash_base": 101}
    result = reader.overlay_performance(before, account_id=ACCOUNT, close=close)
    assert result["daily_close"]["reasons"] == ["production_close_components_mismatch"]
    assert {k: v for k, v in result.items() if k != "daily_close"} == before


@pytest.mark.parametrize("alteration", ["missing", "amount", "date", "basis"])
def test_no_old_pnl_survives_partial_or_invalid_period_as_a_confirmed_value(published, alteration):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    if alteration == "missing":
        close.pop("period")
    elif alteration == "amount":
        close["period"]["pnl_base"] = D(500)
    elif alteration == "date":
        close["period"]["to_date"] = "2024-01-01"
    else:
        close["period"]["basis"] = "ytd_shortcut"
    before = legacy()
    result = reader.overlay_performance(before, account_id=ACCOUNT, close=close)
    assert result["daily_close"]["status"] == "unavailable"
    assert {k: v for k, v in result.items() if k != "daily_close"} == before


@pytest.mark.parametrize("change", [{"rate": D("NaN")}, {"rate": D(0)}, {"rate": D(-1)},
                                    {"as_of": "2024-01-01"}, {"source": ""}, {"currency": "EUR"},
                                    {"base_currency": "USD"}, {"requested_as_of": "2024-01-01"}])
def test_invalid_fx_proof_never_generates_cny_or_hides_proven_usd(published, change):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    result = reader.overlay_performance(legacy(), account_id=ACCOUNT, close=close, fx_evidence=fx_proof(**change))
    assert result["net_liquidation_base"] == 99
    assert result["total_pnl_base"] == 29
    assert not any(key.endswith("_cny") for key in result)
    assert "fx_rate_to_cny" not in result
    assert result["daily_close"]["fx_status"] == "unavailable"


def test_rate_without_date_source_metadata_is_not_a_valid_fx_proof(published):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    result = reader.overlay_performance(legacy(), account_id=ACCOUNT, close=close, fx_rate=D(7))
    assert result["daily_close"]["fx_evidence"]["reasons"] == ["missing_fx_metadata"]
    assert "total_pnl_cny" not in result


@pytest.mark.parametrize("row,reason", [
    ((D(7), date(2024, 1, 1), "synthetic_fx"), "expired_historical_fx"),
    ((D(7), date(2024, 1, 3), "synthetic_fx"), "future_historical_fx"),
])
def test_fx_exposes_actual_date_source_and_expiry_without_rate_fallback(row, reason):
    class Cursor:
        def execute(self, sql, params):
            assert "SELECT rate, as_of, source" in sql
        def fetchone(self):
            return row
    proof = reader.historical_fx(Cursor(), currency="USD", base_currency="CNY", as_of=DAY)
    assert proof["status"] == "unavailable" and proof["reasons"] == [reason]
    assert proof["as_of"] == row[1].isoformat() and proof["source"] == "synthetic_fx"
    assert "rate" not in proof
    assert reader.historical_rate(Cursor(), currency="USD", base_currency="CNY", as_of=DAY) is None


def test_bulk_reader_parses_once_and_isolation_from_consumer_mutation(published, monkeypatch):
    evidence = reader.load_evidence()
    with reader._CACHE_LOCK:
        reader._CACHE.clear()
    calls = []
    original = reader.parse_daily_close_series
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(reader, "parse_daily_close_series", counted)
    first = reader.read_series(evidence=evidence)
    assert first["status"] == "ok"
    first["daily_closes"][DAY.isoformat()]["nav_base"] = 999
    for _ in range(5):
        assert reader.read_close(as_of=DAY, evidence=evidence)["nav_base"] == 99
        assert reader.read_series(evidence=evidence)["daily_closes"][DAY.isoformat()]["nav_base"] == 99
    assert len(calls) == 1


def test_bulk_reader_rejects_forged_text_even_after_warm_cache(published):
    evidence = reader.load_evidence()
    assert reader.read_series(evidence=evidence)["status"] == "ok"
    evidence["xml_text"] += " "
    assert reader.read_series(evidence=evidence)["status"] == "unavailable"
    assert reader.read_close(as_of=DAY, evidence=evidence)["status"] == "unavailable"


def test_cache_is_report_bounded_and_has_no_large_xml_keys(published):
    evidence = reader.load_evidence()
    with reader._CACHE_LOCK:
        reader._CACHE.clear()
    for padding in range(5):
        text = evidence["xml_text"] + " " * padding
        proof = {**evidence, "xml_text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        assert reader.read_series(evidence=proof)["status"] == "ok"
    assert len(reader._CACHE) == reader.CACHE_MAX_REPORTS
    assert all(len(key[0]) == 64 and key[1] == ACCOUNT for key in reader._CACHE)
    assert all("xml_text" not in value for value in reader._CACHE.values())


def test_manifest_declared_period_and_last_day_cannot_be_silently_ignored(published):
    manifest = json.loads(published.read_text())
    manifest["period_start"] = "2024-01-03"
    published.write_text(json.dumps(manifest))
    assert reader.load_evidence()["status"] == "unavailable"


def test_bounded_read_checks_actual_bytes_not_stat(tmp_path):
    source = tmp_path / "oversized"
    source.write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="file_size_limit"):
        reader.read_bounded(source, 10)


def test_publisher_same_date_different_source_requires_explicit_separate_approval(published):
    before = published.read_bytes()
    with pytest.raises(ValueError, match="publication_same_date_source_conflict"):
        publisher().publish(xml() + b" ", directory=published.parent, account_id=ACCOUNT,
                            as_of=DAY, period_start=date(2024, 1, 1))
    assert published.read_bytes() == before


def test_publication_rejects_actual_date_regression_under_lock(published):
    before = published.read_bytes()
    old = xml().replace(b"20240102", b"20240101")
    with pytest.raises(ValueError, match="publication_scope_or_date_regression"):
        publisher().publish(old, directory=published.parent, account_id=ACCOUNT,
                            as_of=date(2024, 1, 1), period_start=date(2024, 1, 1))
    assert published.read_bytes() == before


def test_previous_source_tampering_blocks_new_publication(published):
    manifest = json.loads(published.read_text())
    (published.parent / manifest["source_file"]).write_bytes(b"tampered")
    newer = xml().replace(b"20240102", b"20240103")
    with pytest.raises(ValueError, match="previous_source_integrity_failure"):
        publisher().publish(newer, directory=published.parent, account_id=ACCOUNT,
                            as_of=date(2024, 1, 3), period_start=date(2024, 1, 1))


def test_publication_lock_blocks_second_writer_and_releases_after_exception(tmp_path):
    module = publisher()
    module.LOCK_WAIT_SECONDS = 0
    with module.publication_lock(tmp_path):
        with pytest.raises(ValueError, match="publication_lock_timeout"):
            with module.publication_lock(tmp_path):
                raise AssertionError("second writer entered")
    with pytest.raises(RuntimeError):
        with module.publication_lock(tmp_path):
            raise RuntimeError("interrupt")
    with module.publication_lock(tmp_path):
        pass


def test_malformed_production_xml_keeps_old_values_with_unavailable_marker(published):
    source = published.parent / "production.xml"
    source.write_bytes(b"<broken>")
    before = legacy()
    result = performance.apply_daily_close(before, source, cur=object())
    assert result["daily_close"]["status"] == "unavailable"
    assert result["daily_close"]["value_basis"] == "legacy_cash_plus_positions_unconfirmed"
    assert {k: v for k, v in result.items() if k != "daily_close"} == before


@pytest.mark.parametrize("flow", [D(-50), D(-51)])
def test_nonpositive_capital_denominator_is_undefined_not_zero_return(published, flow):
    close = reader.read_close(as_of=DAY, period_start=date(2024, 1, 1))
    close["period"]["net_external_flow_base"] = flow
    close["period"]["pnl_base"] = close["nav_base"] - D(50) - flow
    result = reader.overlay_performance(legacy(), account_id=ACCOUNT, close=close, fx_evidence=fx_proof())
    assert result["daily_close"]["status"] == "ok"
    assert result["daily_close"]["return_ratio_status"] == "undefined"
    assert result["return_ratio"] is None
    assert result["return_capital_base"] == D(50) + flow
    assert result["return_capital_cny"] == (D(50) + flow) * 7
    assert result["net_deposit_base"] == flow


@pytest.mark.parametrize("phase", ["legacy_parse", "overlay"])
def test_published_source_hash_guards_both_sides_of_overlay(tmp_path, monkeypatch, phase):
    source = tmp_path / "production.xml"
    source.write_bytes(xml())
    selected = performance.report_state.PublishedReport(source, DAY, 1,
        hashlib.sha256(source.read_bytes()).hexdigest(), None)
    monkeypatch.setattr(performance, "get_conn", lambda: nullcontext(None))
    monkeypatch.setattr(performance.report_state, "select_report", lambda *args: (selected, None))
    def parse(path):
        if phase == "legacy_parse":
            source.write_bytes(xml() + b" ")
        return legacy()
    def overlay(result, path):
        if phase == "overlay":
            source.write_bytes(xml() + b" ")
        return result
    monkeypatch.setattr(performance, "_parse_flex_performance", parse)
    monkeypatch.setattr(performance, "apply_daily_close", overlay)
    with pytest.raises(performance.IBKRFlexError, match="changed during read"):
        performance.read_published_performance()


@pytest.mark.parametrize("production_env", ["IBKR_FLEX_POSITIONS_QUERY_ID", "IBKR_FLEX_OPEN_POSITIONS_QUERY_ID",
                                          "IBKR_FLEX_QUERY_ID"])
def test_cli_cannot_download_any_configured_production_query(monkeypatch, capsys, production_env):
    from collectors import ibkr_flex
    called = []
    monkeypatch.setattr(ibkr_flex, "request_flex_reference", lambda *args: called.append(args))
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "synthetic_token_never_printed")
    for name, value in (("IBKR_FLEX_POSITIONS_QUERY_ID", "11"), ("IBKR_FLEX_OPEN_POSITIONS_QUERY_ID", "22"),
                        ("IBKR_FLEX_QUERY_ID", "33")):
        monkeypatch.setenv(name, value)
    query = "000" + os.environ[production_env]
    monkeypatch.setattr(sys, "argv", ["ibkr_daily_close", "--download-query-id", query,
        "--account-id", ACCOUNT, "--as-of", DAY.isoformat(), "--period-start", "2024-01-01"])
    assert publisher().main() == 2
    assert called == []
    assert "synthetic_token_never_printed" not in capsys.readouterr().out


def test_enabled_public_read_keeps_last_values_but_does_not_claim_unproven_close_is_ok(tmp_path, monkeypatch):
    source = tmp_path / "production.xml"
    source.write_bytes(xml())
    selected = performance.report_state.PublishedReport(source, DAY, 1,
        hashlib.sha256(source.read_bytes()).hexdigest(), None)
    monkeypatch.setenv(reader.MANIFEST_ENV, "unavailable.json")
    monkeypatch.setattr(performance, "get_conn", lambda: nullcontext(None))
    monkeypatch.setattr(performance.report_state, "select_report", lambda *args: (selected, None))
    monkeypatch.setattr(performance.report_state, "report_quality", lambda *args: {"status": "ok"})
    monkeypatch.setattr(performance, "_parse_flex_performance", lambda *args, **kwargs: legacy())
    monkeypatch.setattr(performance, "apply_daily_close", lambda value, path:
        {**value, "daily_close": {"status": "unavailable", "reasons": ["missing_evidence"]}})
    result = performance.read_published_performance()
    assert result["status"] == "unavailable"
    assert result["net_liquidation_base"] == legacy()["net_liquidation_base"]
    assert result["total_pnl_base"] == legacy()["total_pnl_base"]


def close_day_xml(day, prior):
    root = ET.fromstring(xml())
    statement = next(root.iter("FlexStatement"))
    statement.set("fromDate", day.strftime("%Y%m%d"))
    statement.set("toDate", day.strftime("%Y%m%d"))
    equity = statement.find("EquitySummaryInBase")
    equity[0].set("reportDate", prior.strftime("%Y%m%d"))
    equity[1].set("reportDate", day.strftime("%Y%m%d"))
    bridge = statement.find("ChangeInNAV")
    bridge.set("fromDate", day.strftime("%Y%m%d"))
    bridge.set("toDate", day.strftime("%Y%m%d"))
    return ET.tostring(root, encoding="utf-8")


@pytest.mark.parametrize("day,prior,instant,confirmed", [
    (date(2026, 10, 21), date(2026, 10, 20), "2026-10-21T19:59:00+00:00", False),
    (date(2026, 10, 21), date(2026, 10, 20), "2026-10-21T20:00:00+00:00", True),
    (date(2026, 6, 19), date(2026, 6, 18), "2026-06-19T20:29:00+00:00", False),
    (date(2026, 6, 19), date(2026, 6, 18), "2026-06-19T20:30:00+00:00", True),
])
def test_supplemental_publish_and_read_share_existing_day_confirmation_gate(
    tmp_path, monkeypatch, day, prior, instant, confirmed,
):
    state = reader.report_state
    monkeypatch.setenv("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30")
    monkeypatch.setattr(state, "utc_now", lambda: datetime.fromisoformat(instant))
    original = state.validate_candidate
    calls = []
    def observed(observed_day, expected_day, previous):
        assert (observed_day, expected_day, previous) == (day, day, None)
        calls.append(True)
        return original(observed_day, expected_day, previous)
    monkeypatch.setattr(state, "validate_candidate", observed)
    content = close_day_xml(day, prior)
    publication_dir = tmp_path / "publication"
    if confirmed:
        assert publisher().publish(content, directory=publication_dir, account_id=ACCOUNT,
            as_of=day, period_start=day)["status"] == "published"
    else:
        with pytest.raises(state.ReportRejected, match="future_or_unclosed_statement_date"):
            publisher().publish(content, directory=publication_dir, account_id=ACCOUNT, as_of=day, period_start=day)
        assert not publication_dir.exists()
    # A full but unapproved XML/manifest cannot bypass the read gate, including
    # explicitly supplied evidence that never went through publication.
    digest = hashlib.sha256(content).hexdigest()
    name = digest + ".xml"
    (tmp_path / name).write_bytes(content)
    manifest = {"version": 1, "purpose": "ibkr_daily_close", "account_code": "ibkr",
                "source_file": name, "source_sha256": digest, "source_account_id": ACCOUNT,
                "as_of": day.isoformat(), "period_start": day.isoformat()}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv(reader.MANIFEST_ENV, str(manifest_path))
    supplied = {"status": "ok", "xml_text": content.decode(), "account_id": ACCOUNT,
                "sha256": digest, "as_of": day, "period_start": day}
    results = [reader.load_evidence(), reader.read_close(as_of=day, evidence=supplied),
               reader.read_series(evidence=supplied)]
    assert all(result["status"] == ("ok" if confirmed else "unavailable") for result in results)
    if not confirmed:
        assert all(result["reasons"] == ["future_or_unclosed_statement_date"] for result in results)
    assert len(calls) == 4


def test_day_confirmation_gate_does_not_require_historical_evidence_to_be_latest(published, monkeypatch):
    monkeypatch.setattr(reader.report_state, "utc_now", lambda: datetime.fromisoformat("2026-10-21T14:00:00+00:00"))
    assert reader.load_evidence()["status"] == "ok"
    assert reader.read_close(as_of=DAY)["status"] == "ok"


def test_cached_close_is_rechecked_against_day_confirmation(tmp_path, monkeypatch):
    day, prior = date(2026, 10, 21), date(2026, 10, 20)
    content = close_day_xml(day, prior)
    evidence = {"status": "ok", "xml_text": content.decode(), "account_id": ACCOUNT, "as_of": day,
                "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(reader.report_state, "utc_now", lambda: datetime.fromisoformat("2026-10-21T20:01:00+00:00"))
    assert reader.read_close(as_of=day, evidence=evidence)["status"] == "ok"
    monkeypatch.setattr(reader.report_state, "utc_now", lambda: datetime.fromisoformat("2026-10-21T19:59:00+00:00"))
    result = reader.read_close(as_of=day, evidence=evidence)
    assert result["status"] == "unavailable" and result["reasons"] == ["future_or_unclosed_statement_date"]
