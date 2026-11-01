from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.accounts import account_balance  # noqa: E402

from domain.investments import portfolio_state  # noqa: E402
from domain.investments.investment_returns import build_account_performance_payload  # noqa: E402


@pytest.fixture(autouse=True)
def proven_scope(monkeypatch):
    monkeypatch.setattr(account_balance, 'load_account_snapshot_coverage',
        lambda cur, pairs: {pair: {'status':'complete'} for pair in pairs})
    monkeypatch.setattr(account_balance, '_cash_source_schema_available', lambda cur: True)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        assert sql.lstrip().split()[0] in {"WITH", "SELECT"}
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)

    def fetchall(self):
        return self.rows.pop(0)


def values(cash_delta, *, previous=True):
    rows = [
        {"as_of": date(2026, 10, 22)},
        [{"market_value_cny": Decimal("900"), "holding_pnl_cny": Decimal("50")}],
        {"market_value": Decimal("800"), "snapshot_as_of": date(2026,10,20)},
        {"cash_delta": cash_delta},
        {"previous_as_of": date(2026, 10, 21) if previous else None},
    ]
    if previous:
        rows += [{"market_value": Decimal("850")}]
    return rows


@pytest.mark.parametrize("delta", ["0", "-100.25", "200.10", "1000", "-500", "12.30", "-1.01"])
@pytest.mark.parametrize("ratio", ["1", "0.625", "0"])
def test_balance_and_performance_share_exact_rollforward(delta, ratio):
    delta, ratio = Decimal(delta), Decimal(ratio)
    kwargs = dict(account_code="tonghuashun", owner_ratio=ratio,
                  net_liquidation_gross=Decimal("1000"),
                  net_liquidation=Decimal("99999"),
                  net_liquidation_as_of=date(2026, 10, 20))
    cur = Cursor(values(delta))
    result = account_balance.load_account_balance_basis(cur, **kwargs)
    expected_gross = Decimal("1000") + (Decimal("900") - Decimal("800"))
    assert result["net_gross"] == expected_gross
    assert result["net_personal"] == expected_gross * ratio
    assert result["previous_net_gross"] == Decimal("1050")
    assert result["net_liquidation_source"] == "rolled_forward_manual_balance"
    assert result["net_liquidation_rollforward"]["cash_delta_gross_cny"] == str(delta)
    assert len(cur.executed) == 6
    sql, params = cur.executed[3]
    assert "affects_cash" in sql and "f.net_cash * fx.rate" in sql and "r.as_of=f.trade_date" in sql
    assert params == ("tonghuashun", date(2026, 10, 20), date(2026, 10, 22))
    assert cur.executed[4][1] == (date(2026, 10, 22), "tonghuashun", date(2026, 10, 22))

    other = Cursor(values(delta) + [{"cash_delta": Decimal("-5"), "fact_ids": []}])
    pnl = build_account_performance_payload(other, initial_capital=Decimal("2000"), **kwargs)
    assert pnl["net_liquidation_cny"] == str(expected_gross * ratio)
    assert pnl["total_pnl_cny"] is None and pnl["return_ratio"] is None
    assert pnl["raw_daily_pnl_cny"] is None and pnl["daily_pnl_cny"] is None
    assert pnl["external_flow_coverage"] == "unknown"
    assert Decimal(pnl["observed_external_net_flow_cny"]) == Decimal("-5") * ratio
    assert pnl["net_liquidation_rollforward"] == result["net_liquidation_rollforward"]
    assert cur.executed == other.executed[:-1]
    assert "f.trade_type IN ('deposit','withdraw')" in other.executed[-1][0]


@pytest.mark.parametrize("anchor", [None, date(2026, 10, 22), date(2026, 10, 23), "2026-10-22"])
def test_same_or_future_anchor_keeps_personal_override(anchor):
    cur = Cursor(values(Decimal("0"))[:2])
    result = account_balance.load_account_balance_basis(
        cur, account_code="tonghuashun", owner_ratio=Decimal("0.625"),
        net_liquidation_gross=Decimal("1000"), net_liquidation=Decimal("701.25"),
        net_liquidation_as_of=anchor,
    )
    assert result["net_gross"] == Decimal("1000")
    assert result["net_personal"] == Decimal("701.25")
    assert result["net_liquidation_source"] == "current_snapshot"
    assert result["previous_net_gross"] is None
    assert result["net_liquidation_rollforward"] is None
    assert len(cur.executed) == 2


def test_missing_snapshot_empty_positions_and_no_previous_close():
    cur = Cursor([{"as_of": None}])
    assert account_balance.load_account_balance_basis(cur, account_code="x", owner_ratio=Decimal("1")) is None
    cur = Cursor([{"as_of": date(2026, 10, 22)}, []])
    result = account_balance.load_account_balance_basis(cur, account_code="x", owner_ratio=Decimal("1"))
    assert result is None
    cur = Cursor(values(Decimal("-2000"), previous=False))
    result = account_balance.load_account_balance_basis(
        cur, account_code="tonghuashun", owner_ratio=Decimal("0.625"),
        net_liquidation_gross=Decimal("1000"), net_liquidation_as_of="2026-10-20",
    )
    assert result["net_personal"] == Decimal("687.500")
    assert result["previous_net_gross"] is None
    assert len(cur.executed) == 5


def test_balance_no_longer_imports_return_modules_and_preserves_shape(monkeypatch):
    monkeypatch.setitem(sys.modules, "domain.investments.investment_returns", None)
    cur = Cursor(values(Decimal("-10")))
    result = portfolio_state._rolled_forward_account_balance(
        cur, account_cfg={"accounts": {"tonghuashun": {"capital_base": "2000"}}},
        account_code="tonghuashun", owner_ratio=Decimal("0.625"),
        manual_balance={"amount": "1000", "currency": "CNY", "as_of": "2026-10-20"},
    )
    assert result == {"amount": Decimal("687.500"), "currency": "CNY",
                      "amount_cny": Decimal("687.500"), "as_of": "2026-10-22",
                      "source": "rolled_forward_manual_balance"}
    for module in (account_balance, portfolio_state):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("domain.investments.investment_return", "endpoints"))


@pytest.mark.parametrize("account,manual,cfg", [
    ("other", {"amount": "1"}, {}),
    ("tonghuashun", None, {}),
    ("tonghuashun", {"amount": "1", "currency": "USD"}, {}),
    ("tonghuashun", {"amount": "bad"}, {}),
    ("tonghuashun", {"amount": "1"}, {"accounts": {"tonghuashun": {"capital_base": "bad"}}}),
    ("tonghuashun", {"amount": "1"}, {"accounts": {"tonghuashun": {"personal_capital": "bad"}}}),
])
def test_legacy_validation_fallback_does_not_query(account, manual, cfg):
    cur = Cursor([])
    assert portfolio_state._rolled_forward_account_balance(
        cur, account_cfg=cfg, account_code=account, manual_balance=manual, owner_ratio=Decimal("0.625")
    ) is None
    assert cur.executed == []


def test_balance_query_failure_still_falls_back():
    class BrokenCursor:
        def execute(self, *args):
            raise RuntimeError("unavailable")

    assert portfolio_state._rolled_forward_account_balance(
        BrokenCursor(), account_cfg={}, account_code="tonghuashun",
        manual_balance={"amount": "1000", "currency": "CNY", "as_of": "2026-10-20"},
        owner_ratio=Decimal("1"),
    ) is None


def test_rollforward_never_treats_missing_fx_cash_as_zero():
    cur=Cursor(values(None))
    result=account_balance.load_account_balance_basis(cur,account_code='tonghuashun',owner_ratio=Decimal(1),
        net_liquidation_gross=Decimal(1000),net_liquidation_as_of=date(2026,10,20))
    assert result['net_gross'] == Decimal('1100')
    assert result['net_liquidation_rollforward']['cash_delta_gross_cny'] is None
    assert 'f.net_cash * fx.rate' in cur.executed[3][0]
    assert "r.as_of=f.trade_date" in cur.executed[3][0]


def test_unverified_scope_does_not_claim_account_total(monkeypatch):
    monkeypatch.setattr(account_balance,'load_account_snapshot_coverage',
        lambda cur,pairs:{pair:{'status':'unknown'} for pair in pairs})
    cur=Cursor([{'as_of':date(2026,10,22)}])
    assert account_balance.load_account_balance_basis(cur,account_code='tonghuashun',owner_ratio=Decimal(1)) is None
