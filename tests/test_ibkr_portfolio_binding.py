"""Synthetic ownership joins and opt-in, read-only PostgreSQL boundary checks."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from decimal import Context, Decimal, Inexact, ROUND_UP, localcontext
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/api"))
from services import ibkr_performance as performance
from services import ibkr_portfolio_binding as binding

D = Decimal
DAY = "2024-02-06"
ACCOUNT = "SYNTHETIC_PORTFOLIO_ACCOUNT"
SHA = "a" * 64


def parts(owner="1"):
    fx = {"status": "ok", "rate": "7", "as_of": DAY, "requested_as_of": DAY,
          "age_days": 0, "source": "synthetic_exact_day_fx", "currency": "USD", "base_currency": "CNY"}
    full = {"status": "ok", "as_of": DAY, "base_currency": "USD", "fx_rate_to_cny": 7.0,
            "net_liquidation_cny": 553.0, "total_pnl_cny": -47.0, "return_capital_cny": 600.0,
            "cash_cny": -140.0, "position_value_cny": 700.0, "return_ratio": -47 / 600,
            "leverage_ratio": 1.27, "negative_cash_ratio": 140 / 553,
            "daily_close": {"status": "ok", "fx_status": "available", "nav_basis": "official_nav_total",
                            "as_of": DAY, "source_account_id": ACCOUNT, "source_sha256": SHA,
                            "fx_evidence": deepcopy(fx)}}
    canonical = {"account_code": "ibkr", "as_of": date.fromisoformat(DAY), "currency": "CNY",
                 "base_currency": "USD", "truth_source": "ibkr_flex", "include_in_nav": True,
                 "amount_cny": D(553) * D(owner), "daily_close": {
                     "status": "ok", "as_of": DAY, "requested_as_of": DAY, "carried": False,
                     "source_account_id": ACCOUNT, "source_sha256": SHA, "owner_ratio": owner,
                     "nav_basis": "official_base_nav_at_internal_historical_fx", "fx_rate": "7",
                     "source_base_currency": "USD", "valuation_currency": "CNY", "fx_evidence": fx,
                     "identity_evidence": {"status": "ok", "as_of": DAY}}}
    return full, [canonical]


@pytest.mark.parametrize("owner", ["0", "0.25", "1"])
def test_owned_amounts_do_not_modify_full_broker_values_or_ratios(owner):
    full, balances = parts(owner)
    before = deepcopy((full, balances))
    result = binding.bind_portfolio_account(full, balances)
    assert result["status"] == "ok"
    assert result["ownership_basis"] == "owner_adjusted" and result["currency"] == "CNY"
    assert result["account_code"] == "ibkr" and result["source_account_id"] == ACCOUNT
    assert result["source_sha256"] == SHA and result["as_of"] == DAY
    assert result["owner_ratio"] == float(owner)
    for field in ("net_liquidation_cny", "total_pnl_cny", "return_capital_cny", "cash_cny", "position_value_cny"):
        assert D(str(result[field])) == D(str(full[field])) * D(owner)
    assert result["return_ratio"] == (full["return_ratio"] if D(owner) > 0 else None)
    assert (full, balances) == before


@pytest.mark.parametrize("field", ["total_pnl_cny", "return_capital_cny", "cash_cny", "position_value_cny"])
@pytest.mark.parametrize("value", [None, "", False, "NaN", "Infinity", [], {}])
def test_missing_or_invalid_money_never_becomes_zero(field, value):
    full, balances = parts("0")
    full[field] = value
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"
    full.pop(field)
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"


@pytest.mark.parametrize("field,value", [
    ("source_account_id", "OTHER_ACCOUNT"), ("source_sha256", "b" * 64), ("source_sha256", None),
    ("as_of", "2024-02-05"), ("requested_as_of", "2024-02-07"), ("carried", True),
    ("status", "unconfirmed"), ("nav_basis", "cash_plus_positions"), ("valuation_currency", "USD"),
    ("source_base_currency", "EUR"), ("owner_ratio", None), ("owner_ratio", "-0.25"),
    ("owner_ratio", "NaN"), ("owner_ratio", True), ("owner_ratio", "Infinity"), ("fx_rate", "8"),
    ("identity_evidence", {"status": "unconfirmed", "as_of": DAY}),
    ("identity_evidence", {"status": "ok", "as_of": "2024-02-05"}),
])
def test_canonical_proof_conflicts_are_not_accepted(field, value):
    full, balances = parts()
    balances[0]["daily_close"][field] = value
    result = binding.bind_portfolio_account(full, balances)
    assert result["status"] == "unconfirmed" and "net_liquidation_cny" not in result


@pytest.mark.parametrize("field", ["source_account_id", "source_sha256", "owner_ratio", "fx_rate",
                                   "as_of", "requested_as_of", "carried", "identity_evidence", "fx_evidence"])
def test_canonical_missing_proof_is_not_inferred(field):
    full, balances = parts()
    balances[0]["daily_close"].pop(field)
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"


@pytest.mark.parametrize("field,value", [("as_of", "2024-02-05"), ("currency", "USD"),
    ("base_currency", "EUR"), ("truth_source", "manual"), ("include_in_nav", False),
    ("amount_cny", None), ("amount_cny", "138.25"), ("amount_cny", "NaN")])
def test_canonical_row_scope_and_actual_owned_nav_are_required(field, value):
    full, balances = parts()
    balances[0][field] = value
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"


def test_missing_or_duplicate_account_cannot_expand_scope():
    full, balances = parts()
    assert binding.bind_portfolio_account(full, [])["status"] == "unconfirmed"
    assert binding.bind_portfolio_account(full, balances + deepcopy(balances))["status"] == "unconfirmed"
    other = {"account_code": "unrelated", "amount_cny": None}
    assert binding.bind_portfolio_account(full, balances + [other])["status"] == "ok"


@pytest.mark.parametrize("target", ["full", "canonical", "both"])
@pytest.mark.parametrize("field,value", [("as_of", "2024-02-05"), ("requested_as_of", "2024-02-05"),
    ("status", "unavailable"), ("source", ""), ("currency", "EUR"), ("base_currency", "USD"),
    ("rate", "0"), ("rate", "NaN"), ("rate", "8")])
def test_fx_identity_rate_source_and_exact_day_are_bound(target, field, value):
    full, balances = parts()
    if target in {"full", "both"}:
        full["daily_close"]["fx_evidence"][field] = value
    if target in {"canonical", "both"}:
        balances[0]["daily_close"]["fx_evidence"][field] = value
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"


@pytest.mark.parametrize("field,value", [("source_account_id", None), ("source_sha256", "bad"),
    ("as_of", "2024-02-05"), ("fx_status", "unavailable"), ("nav_basis", "legacy"), ("status", "unavailable")])
def test_full_performance_requires_new_complete_scope(field, value):
    full, balances = parts()
    full["daily_close"][field] = value
    assert binding.bind_portfolio_account(full, balances)["status"] == "unconfirmed"


def test_explicit_zero_and_negative_cash_and_undefined_ratio_are_preserved():
    full, balances = parts("0.25")
    full.update(return_capital_cny=0, return_ratio=None)
    result = binding.bind_portfolio_account(full, balances)
    assert result["return_capital_cny"] == 0 and result["return_ratio"] is None
    assert result["cash_cny"] == -35 and result["total_pnl_cny"] == -11.75


@pytest.mark.parametrize("difference,accepted", [("1e-8", True), ("1.00001e-8", False), ("-1e-8", True)])
def test_actual_canonical_nav_is_returned_with_only_the_agreed_tolerance(difference, accepted):
    full, balances = parts("0.25")
    balances[0]["amount_cny"] += D(difference)
    result = binding.bind_portfolio_account(full, balances)
    assert (result["status"] == "ok") is accepted
    if accepted:
        assert result["net_liquidation_cny"] == float(balances[0]["amount_cny"])


def test_arithmetic_does_not_inherit_ambient_decimal_context():
    full, balances = parts("0.25")
    expected = binding.bind_portfolio_account(full, balances)
    hostile = Context(prec=2, rounding=ROUND_UP, Emin=-2, Emax=2, clamp=1)
    hostile.traps[Inexact] = True
    with localcontext(hostile) as context:
        before = context.copy()
        assert binding.bind_portfolio_account(full, balances) == expected
        assert context.prec == before.prec and context.traps == before.traps and context.flags == before.flags


class Connection:
    def __init__(self):
        self.session = None
        self.cursor_factory = None
        self.closed = False

    def set_session(self, **kwargs):
        self.session = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, *, cursor_factory):
        self.cursor_factory = cursor_factory
        return nullcontext(self)

    def close(self):
        self.closed = True


def loader_setup(monkeypatch, balances):
    cfg = {"accounts": {"ibkr": {"truth_source": "ibkr_flex", "owner_ratio": "0.25"}}}
    calls = []
    monkeypatch.setattr(binding.portfolio_state, "_load_accounts_config", lambda: cfg)
    def load(cur, base, config):
        calls.append((cur, base, config))
        return balances
    monkeypatch.setattr(binding.portfolio_state, "load_account_balances", load)
    return cfg, calls


def test_loader_calls_canonical_valuation_once_in_readonly_realdict_transaction(monkeypatch):
    full, balances = parts("0.25")
    before = deepcopy(full)
    cfg, calls = loader_setup(monkeypatch, balances)
    conn = Connection()
    result = binding.read_bound_performance(full, enabled=True, conn_factory=lambda: conn)
    assert result["portfolio_account"]["net_liquidation_cny"] == 138.25
    assert {key: value for key, value in result.items() if key != "portfolio_account"} == before
    assert full == before and calls == [(conn, "CNY", cfg)]
    assert conn.session == {"readonly": True, "isolation_level": "REPEATABLE READ"}
    assert conn.cursor_factory is RealDictCursor and conn.closed


@pytest.mark.parametrize("kind", ["default_off", "legacy", "unavailable_close"])
def test_legacy_default_never_reads_config_or_opens_an_extra_connection(monkeypatch, kind):
    full, _ = parts()
    if kind == "legacy":
        full.pop("daily_close")
    elif kind == "unavailable_close":
        full["daily_close"]["status"] = "unavailable"
    def forbidden():
        pytest.fail("default/legacy must not query the portfolio")
    monkeypatch.setattr(binding.portfolio_state, "_load_accounts_config", forbidden)
    assert binding.read_bound_performance(full, enabled=kind != "default_off", conn_factory=forbidden) is full


@pytest.mark.parametrize("cfg", [{}, {"accounts": {}}, {"accounts": {"ibkr": {}}}])
def test_missing_configuration_is_unconfirmed_without_implicit_full_ownership(monkeypatch, cfg):
    full, _ = parts()
    monkeypatch.setattr(binding.portfolio_state, "_load_accounts_config", lambda: cfg)
    result = binding.read_bound_performance(full, enabled=True, conn_factory=lambda: pytest.fail("no config"))
    assert result["status"] == "unavailable" and result["portfolio_account"]["status"] == "unconfirmed"
    assert result["net_liquidation_cny"] == full["net_liquidation_cny"]


def test_unmapped_identity_and_loader_failure_disable_only_public_availability(monkeypatch):
    full, balances = parts()
    balances[0]["daily_close"]["source_account_id"] = "OTHER_ACCOUNT"
    loader_setup(monkeypatch, balances)
    result = binding.read_bound_performance(full, enabled=True, conn_factory=Connection)
    assert result["status"] == "unavailable" and result["portfolio_account"]["status"] == "unconfirmed"
    assert {key: value for key, value in result.items() if key not in {"status", "portfolio_account"}} == {
        key: value for key, value in full.items() if key != "status"}
    def failed(*args):
        raise RuntimeError("private connection details must not leak")
    monkeypatch.setattr(binding.portfolio_state, "load_account_balances", failed)
    result = binding.read_bound_performance(full, enabled=True, conn_factory=Connection)
    assert result["portfolio_account"]["reasons"] == ["portfolio_binding_unavailable"]


@pytest.mark.parametrize("owner", ["0", "0.25", "1"])
def test_real_public_read_hook_preserves_full_fields_and_attaches_owned_contract(tmp_path, monkeypatch, owner):
    full, balances = parts(owner)
    source = tmp_path / "synthetic.xml"
    source.write_bytes(b"<FlexQueryResponse/>")
    report = SimpleNamespace(path=source, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(performance, "get_conn", lambda: nullcontext(None))
    monkeypatch.setattr(performance.report_state, "select_report", lambda *args: (report, None))
    monkeypatch.setattr(performance.report_state, "report_quality", lambda *args: {"status": "ok"})
    monkeypatch.setattr(performance, "_parse_flex_performance", lambda *args, **kwargs: deepcopy(full))
    monkeypatch.setattr(performance, "apply_daily_close", lambda value, path: value)
    monkeypatch.setattr("services.ibkr_daily_close_read.configured", lambda: True)
    loader_setup(monkeypatch, balances)
    monkeypatch.setattr(binding.portfolio_state, "get_conn", Connection)
    result = performance.read_published_performance()
    assert result["portfolio_account"]["net_liquidation_cny"] == float(D(553) * D(owner))
    assert all(result[key] == value for key, value in full.items())


@pytest.mark.parametrize("failure", [False, True])
def test_postgresql_loader_boundary_is_server_readonly_and_closes_connection(monkeypatch, failure):
    dsn = os.environ.get("PAMS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PAMS_TEST_POSTGRES_DSN must explicitly select an isolated PostgreSQL server")
    full, balances = parts("0.25")
    cfg, calls = loader_setup(monkeypatch, balances)
    observed = []
    def load(cur, base, config):
        assert isinstance(cur, RealDictCursor) and base == "CNY" and config is cfg
        cur.execute("SELECT current_setting('transaction_read_only') AS readonly")
        assert cur.fetchone()["readonly"] == "on"
        observed.append(True)
        if failure:
            cur.execute("SELECT 1 / 0")
        cur.execute("SELECT %s::jsonb AS canonical", (json.dumps(balances[0], default=str),))
        return [cur.fetchone()["canonical"]]
    monkeypatch.setattr(binding.portfolio_state, "load_account_balances", load)
    conn = psycopg2.connect(dsn)
    result = binding.read_bound_performance(full, enabled=True, conn_factory=lambda: conn)
    assert result["status"] == ("unavailable" if failure else "ok")
    assert observed == [True] and conn.closed and calls == []
    if not failure:
        assert result["portfolio_account"]["net_liquidation_cny"] == 138.25
