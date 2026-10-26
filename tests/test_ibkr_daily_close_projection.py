from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from services import ibkr_daily_close_read as daily
from services import portfolio_valuation_state as projection
from services import valuation_nav as nav
from services.portfolio_summary import apply_published_valuation_state_to_summary

DAY = date(2024, 1, 2)
SHA = "a" * 64


def proof():
    return {"status": "ok", "nav_basis": "official_base_nav_at_internal_historical_fx",
        "source_sha256": SHA, "source_account_id": "SYNTHETIC_ACCOUNT", "as_of": DAY.isoformat(),
        "assets_cny": "100", "liabilities_cny": "10", "owner_ratio": "1", "fx_rate": "7",
        "source_base_currency": "USD", "valuation_currency": "CNY",
        "fx_evidence": {"status": "ok", "rate": "7", "as_of": DAY.isoformat(), "requested_as_of": DAY.isoformat(),
                        "source": "synthetic_internal_fx", "currency": "USD", "base_currency": "CNY", "age_days": 0}}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(daily.MANIFEST_ENV, "synthetic-not-read.json")
    monkeypatch.setattr(daily, "load_evidence", lambda: {"status": "ok", "sha256": SHA,
        "account_id": "SYNTHETIC_ACCOUNT", "as_of": DAY})


def test_default_projection_eligibility_is_unchanged(monkeypatch):
    monkeypatch.delenv(daily.MANIFEST_ENV, raising=False)
    assert projection.published_daily_close_compatible(None)
    assert projection.published_daily_close_compatible({"legacy": True})


def test_old_projection_cannot_overwrite_confirmed_nav_or_pnl(enabled):
    payload = {"totals": {"market_value": "90", "holding_pnl": "7", "daily_pnl": "3"}}
    old = {"valuation_date": DAY, "net_liquidation_value": "99999"}
    result = apply_published_valuation_state_to_summary(payload, old, account_balances=parts()[0])
    assert result["totals"] == payload["totals"]
    assert result["valuation_state"]["overlay_applied"] is False
    assert result["valuation_state"]["overlay_reason"] == "daily_close_evidence_basis_mismatch"


@pytest.mark.parametrize("change", [{"source_sha256": "b" * 64}, {"source_account_id": "OTHER"},
                                   {"as_of": "2024-01-01"}, {"status": "unconfirmed"},
                                   {"nav_basis": "cash_plus_positions"}])
def test_wrong_published_evidence_is_ineligible(enabled, change):
    row = {"raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90", **change}}}
    assert not projection.published_daily_close_compatible(row, account_balances=parts()[0])


def test_matching_published_evidence_is_eligible(enabled):
    assert projection.published_daily_close_compatible(
        {"raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}}, account_balances=parts()[0])


def parts():
    balances = [{"account_code": "ibkr", "include_in_nav": True, "as_of": DAY,
        "amount_cny": Decimal(90), "daily_close": proof(), "review_status": "ready_for_review"}]
    positions = [{"account_code": "ibkr", "as_of": DAY, "market_value_cny": Decimal(80),
                  "asset_type": "stock", "asset_id": "SYNTHETIC_CONTRACT", "holding_pnl_cny": Decimal(7)}]
    return balances, positions


def test_new_projection_records_only_actually_applied_nav_basis(enabled):
    balances, positions = parts()
    result = projection.build_portfolio_valuation_state_from_parts(account_balances=balances,
        investment_positions=positions, as_of=DAY)
    assert result["net_liquidation_value"] == Decimal(90)
    assert result["raw_json"]["ibkr_daily_close"] == {**proof(), "net_worth_cny": "90"}
    assert projection.published_daily_close_compatible(result, account_balances=balances)
    positions[0]["as_of"] = date(2024, 1, 3)
    stale = projection.build_portfolio_valuation_state_from_parts(account_balances=balances,
        investment_positions=positions, as_of=date(2024, 1, 3))
    assert stale["raw_json"]["ibkr_daily_close"]["status"] == "unconfirmed"
    assert stale["data_completeness"] == "partial"
    assert not projection.published_daily_close_compatible(stale, account_balances=balances)


def test_nav_endpoint_rebuilds_terminal_bucket_when_projection_basis_is_old(enabled, monkeypatch):
    import main
    balances, positions = parts()
    class Connection:
        def cursor(self, **kwargs):
            return nullcontext(object())
    monkeypatch.setattr(main, "get_conn", lambda: nullcontext(Connection()))
    monkeypatch.setattr(main.accounts_endpoint, "_overview_payload", lambda: {})
    monkeypatch.setattr(main, "_investment_return_view_as_of", lambda cur: DAY)
    monkeypatch.setattr(main, "_latest_published_portfolio_valuation_state", lambda *args, **kwargs:
                        {"valuation_date": DAY, "net_liquidation_value": "99999"})
    monkeypatch.setattr(main, "_is_published_valuation_fresh_for_date", lambda *args: True)
    monkeypatch.setattr(main, "_load_portfolio_accounts_config", lambda: {})
    monkeypatch.setattr(main, "_load_account_balances", lambda *args: balances)
    monkeypatch.setattr(main, "_load_investment_positions", lambda *args, **kwargs: positions)
    captured = {}
    def build(cur, **kwargs):
        captured.update(kwargs)
        return {"rows": []}
    monkeypatch.setattr(main, "_build_nav_weekly_payload", build)
    main.nav_weekly(start=DAY.isoformat(), baseCurrency="CNY", latest=True, interval="daily")
    assert captured["terminal_nav_bucket"]["net_worth"] == Decimal(90)
    assert captured["terminal_nav_bucket"]["ibkr_daily_close"] == {**proof(), "net_worth_cny": "90"}
    assert captured["terminal_account_values_cny"]["ibkr"] == Decimal(90)


def final_assembler(monkeypatch, terminal, *, status="ok", account_values=None):
    day = DAY if status != "last_valid" else date(2024, 1, 3)
    metadata = {**proof(), "status": status, "requested_as_of": day.isoformat(), "carried": day != DAY}
    if status == "last_valid":
        metadata.update(age_days=1, reasons=["missing_closing_date"])
    ibkr = {day: {"assets": Decimal(100), "liabilities": Decimal(10), "net_worth": Decimal(90)}}
    if status != "absent":
        ibkr[day]["daily_close"] = metadata
    for name, result in (("load_fx_timelines", {}), ("_statement_rows_by_account", {}),
                         ("_bank_backcast_series", {day: {"assets": Decimal(50), "liabilities": Decimal(0),
                                                        "net_worth": Decimal(50)}}),
                         ("_investment_snapshot_series", {}), ("_tonghuashun_series", {}),
                         ("account_anchor_value_cny", Decimal(0)), ("_ibkr_nav_series", ibkr)):
        monkeypatch.setattr(nav, name, lambda *args, _result=result, **kwargs: _result)
    monkeypatch.setattr(nav, "scale_series_to_terminal_anchor", lambda series, **kwargs: series)
    before = deepcopy(terminal)
    result = nav.build_nav_weekly_payload(object(), start_date=day, terminal_date=day,
        base_currency="CNY", overview_payload={}, owner_ratio_case_sql=lambda alias: ("", []),
        account_owner_ratio=lambda account: Decimal(1), terminal_nav_bucket=terminal,
        terminal_account_values_cny=account_values, interval="daily")
    assert terminal == before
    return result["rows"][0], metadata


@pytest.mark.parametrize("status", ["ok", "last_valid"])
def test_final_assembler_preserves_proven_daily_close_over_unqualified_terminal(monkeypatch, status):
    row, metadata = final_assembler(monkeypatch,
        {"assets": Decimal(800), "liabilities": Decimal(0), "net_worth": Decimal(800)}, status=status)
    assert Decimal(row["assets"]) == 150 and Decimal(row["liabilities"]) == 10
    assert Decimal(row["netWorth"]) == 140
    assert row["ibkr_daily_close"] == metadata


@pytest.mark.parametrize("status", ["ok", "last_valid"])
def test_final_assembler_accepts_terminal_with_same_account_close_and_valuation_inputs(monkeypatch, status):
    terminal = {"assets": Decimal(170), "liabilities": Decimal(10), "net_worth": Decimal(160),
                "ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}
    row, metadata = final_assembler(monkeypatch, terminal, status=status, account_values={"ibkr": Decimal(90)})
    assert Decimal(row["netWorth"]) == 160 and Decimal(row["assets"]) == 170
    # Current series metadata owns last-valid dating; a terminal 'ok' marker
    # cannot relabel a carried account value as a newly confirmed close.
    assert row["ibkr_daily_close"] == metadata


def test_actual_current_bucket_preserves_new_other_account_anchor_in_final_assembler(monkeypatch):
    balances, positions = parts()
    balances.append({"account_code": "synthetic_bank", "include_in_nav": True,
                     "as_of": DAY, "amount_cny": Decimal(70)})
    before = deepcopy((balances, positions))
    terminal = nav.current_nav_bucket_from_parts(account_balances=balances, investment_positions=positions)
    account_values = nav.current_nav_account_values_from_parts(account_balances=balances, investment_positions=positions)
    assert {key: terminal[key] for key in ("assets", "liabilities", "net_worth")} == {
        "assets": Decimal(170), "liabilities": Decimal(10), "net_worth": Decimal(160)}
    assert terminal["ibkr_daily_close"] == {**proof(), "net_worth_cny": "90"}
    row, metadata = final_assembler(monkeypatch, terminal, account_values=account_values)
    assert Decimal(row["assets"]) == 170 and Decimal(row["liabilities"]) == 10
    assert Decimal(row["netWorth"]) == 160  # The historical bank anchor in the assembler is only 50.
    assert row["ibkr_daily_close"] == metadata
    assert (balances, positions) == before


def test_current_bucket_does_not_stamp_unapplied_old_proof_on_newer_native_positions(monkeypatch):
    balances, positions = parts()
    positions[0]["as_of"] = date(2024, 1, 3)
    balances.append({"account_code": "synthetic_bank", "include_in_nav": True,
                     "as_of": date(2024, 1, 3), "amount_cny": Decimal(70)})
    terminal = nav.current_nav_bucket_from_parts(account_balances=balances, investment_positions=positions)
    assert "ibkr_daily_close" not in terminal
    assert terminal["net_worth"] == 150
    row, metadata = final_assembler(monkeypatch, terminal, status="last_valid")
    assert Decimal(row["netWorth"]) == 140
    assert row["ibkr_daily_close"] == metadata and metadata["status"] == "last_valid"


def test_current_bucket_without_official_application_keeps_exact_legacy_three_key_shape(monkeypatch):
    monkeypatch.delenv(daily.MANIFEST_ENV, raising=False)
    balances, positions = parts()
    balances[0].pop("daily_close")
    assert nav.current_nav_bucket_from_parts(account_balances=balances, investment_positions=positions) == {
        "assets": Decimal(80), "liabilities": Decimal(0), "net_worth": Decimal(80)}


@pytest.mark.parametrize("change", [
    {"source_sha256": "b" * 64}, {"source_account_id": "OTHER_ACCOUNT"}, {"as_of": "2024-01-01"},
    {"owner_ratio": "0.5"}, {"fx_rate": "8"}, {"valuation_currency": "USD"},
    {"assets_cny": "101"}, {"liabilities_cny": "11"}, {"net_worth_cny": "45"},
    {"status": "unconfirmed"}, {"fx_evidence": {"status": "unavailable"}},
])
def test_final_assembler_rejects_mismatched_terminal_proof_without_partial_amounts(monkeypatch, change):
    terminal = {"assets": Decimal(800), "liabilities": Decimal(0), "net_worth": Decimal(800),
                "ibkr_daily_close": {**proof(), "net_worth_cny": "90", **change}}
    row, metadata = final_assembler(monkeypatch, terminal)
    assert Decimal(row["netWorth"]) == 140
    assert Decimal(row["assets"]) == 150 and Decimal(row["liabilities"]) == 10
    assert row["ibkr_daily_close"] == metadata


@pytest.mark.parametrize("field", ["source_base_currency", "valuation_currency", "fx_evidence", "owner_ratio",
                                   "fx_rate", "assets_cny", "liabilities_cny", "net_worth_cny"])
def test_final_assembler_does_not_infer_missing_terminal_proof_fields(monkeypatch, field):
    proposed = {**proof(), "net_worth_cny": "90"}
    proposed.pop(field)
    row, _ = final_assembler(monkeypatch, {"assets": Decimal(170), "liabilities": Decimal(10),
        "net_worth": Decimal(160), "ibkr_daily_close": proposed})
    assert Decimal(row["netWorth"]) == 140


@pytest.mark.parametrize("change", [{"assets": "NaN"}, {"liabilities": "-1"}, {"net_worth": "0"},
                                    {"assets": "99", "liabilities": "10", "net_worth": "89"}])
def test_final_assembler_rejects_invalid_terminal_totals_even_with_matching_marker(monkeypatch, change):
    terminal = {"assets": Decimal(170), "liabilities": Decimal(10), "net_worth": Decimal(160),
                "ibkr_daily_close": {**proof(), "net_worth_cny": "90"}, **change}
    row, _ = final_assembler(monkeypatch, terminal)
    assert Decimal(row["netWorth"]) == 140


def test_final_assembler_crosschecks_actual_terminal_account_value(monkeypatch):
    terminal = {"assets": Decimal(170), "liabilities": Decimal(10), "net_worth": Decimal(160),
                "ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}
    row, _ = final_assembler(monkeypatch, terminal, account_values={"ibkr": Decimal(800)})
    assert Decimal(row["netWorth"]) == 140


@pytest.mark.parametrize("status", ["unconfirmed", "absent"])
def test_final_assembler_preserves_legacy_terminal_path_without_confirmed_evidence(monkeypatch, status):
    row, _ = final_assembler(monkeypatch,
        {"assets": Decimal(800), "liabilities": Decimal(0), "net_worth": Decimal(800)}, status=status)
    assert Decimal(row["netWorth"]) == 800
    if status == "absent":
        assert "ibkr_daily_close" not in row
    else:
        assert row["ibkr_daily_close"]["status"] == "unconfirmed"


def test_changed_live_ownership_cannot_reuse_old_xml_matched_published_total(enabled):
    balances, _ = parts()
    persisted = {"valuation_date": DAY, "net_liquidation_value": "90",
                 "raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}}
    balances[0]["amount_cny"] = Decimal(45)
    balances[0]["daily_close"].update(owner_ratio="0.5", assets_cny="50", liabilities_cny="5")
    assert not projection.published_daily_close_compatible(persisted, account_balances=balances)
    payload = {"as_of": DAY.isoformat(), "totals": {"market_value": "45", "holding_pnl": "7"}}
    result = apply_published_valuation_state_to_summary(payload, persisted, account_balances=balances)
    assert result["totals"] == payload["totals"] and result["valuation_state"]["overlay_applied"] is False


@pytest.mark.parametrize("field,value", [("fx_rate", "8"), ("assets_cny", "101"), ("liabilities_cny", "11"),
                                       ("valuation_currency", "USD")])
def test_published_compatibility_compares_live_inputs_not_only_xml_identity(enabled, field, value):
    balances, _ = parts()
    persisted = {"raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}}
    balances[0]["daily_close"][field] = value
    assert not projection.published_daily_close_compatible(persisted, account_balances=balances)


def test_published_compatibility_compares_actual_amount_and_fx_source(enabled):
    balances, _ = parts()
    persisted = {"raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}}
    balances[0]["amount_cny"] = Decimal(45)
    assert not projection.published_daily_close_compatible(persisted, account_balances=balances)
    balances[0]["amount_cny"] = Decimal(90)
    balances[0]["daily_close"]["fx_evidence"]["source"] = "different_internal_source"
    assert not projection.published_daily_close_compatible(persisted, account_balances=balances)


@pytest.mark.parametrize("field", ["fx_evidence", "owner_ratio", "fx_rate", "valuation_currency", "net_worth_cny"])
def test_published_compatibility_rejects_missing_valuation_inputs(enabled, field):
    balances, _ = parts()
    persisted_proof = {**proof(), "net_worth_cny": "90"}
    persisted_proof.pop(field)
    assert not projection.published_daily_close_compatible({"raw_json": {"ibkr_daily_close": persisted_proof}},
                                                          account_balances=balances)


def test_published_compatibility_requires_current_confirmed_balance(enabled):
    persisted = {"raw_json": {"ibkr_daily_close": {**proof(), "net_worth_cny": "90"}}}
    assert not projection.published_daily_close_compatible(persisted)
    balances, _ = parts()
    balances[0]["daily_close"]["status"] = "unconfirmed"
    assert not projection.published_daily_close_compatible(persisted, account_balances=balances)


@pytest.mark.parametrize("ratio", ["0", "0.25", "1"])
def test_shared_value_metadata_exposes_owned_nav_in_source_base_currency(ratio):
    close = {"as_of": DAY.isoformat(), "source_sha256": SHA, "account_id": "SYNTHETIC_ACCOUNT",
             "base_currency": "USD", "nav_base": Decimal(79), "cash_base": Decimal(-20),
             "position_value_base": Decimal(100), "interest_accrual_base": Decimal(-3),
             "dividend_accrual_base": Decimal(2), "other_nav_components_base": Decimal(0)}
    before = deepcopy(close)
    owner = Decimal(ratio)
    metadata = nav.daily_close_value_metadata(close, rate=Decimal(7), owner_ratio=owner,
                                              legacy_value=Decimal(80) * owner * 7)
    assert metadata["owned_nav_base"] == str(Decimal(79) * owner)
    assert metadata["source_base_currency"] == "USD"
    assert Decimal(metadata["nav_adjustment_cny"]) == -owner * 7
    assert close == before
