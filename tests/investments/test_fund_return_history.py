from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))

from domain.investments.fund_return_history import reconstruct_fund_series  # noqa: E402

MON, TUE, WED, THU, FRI = (date(2026, 11, day) for day in range(9, 14))
OPEN = date(2026, 11, 6)
ASSET = {"asset": {"primary_symbol": "123456", "canonical_name": "Synthetic fund"}}


def trade(day, kind, quantity, cash, **fields):
    return {
        "account_code": "synthetic", "asset_id": "asset", "symbol": "123456",
        "currency": "CNY", "trade_date": day, "trade_type": kind, "quantity": quantity,
        "net_cash": cash, "source_evidence_status": "verified", "fund_execution_confirmed": True,
        **fields,
    }


def preview(trades, marks, snapshots=(), *, owner_ratio=lambda _: Decimal(1)):
    return reconstruct_fund_series(
        assets=ASSET, trades=trades, snapshots=snapshots,
        marks={("123456", day): Decimal(price) for day, price in marks.items()},
        start=MON, end=FRI, interval="daily", owner_ratio=owner_ratio,
    )


def test_opening_and_closed_positions_are_replayed_without_counting_subscriptions_as_profit():
    trades = [
        trade(OPEN, "fund_subscription", 100, -100),
        trade(MON, "fund_subscription", 20, -22, fund_pricing_date=TUE.isoformat()),
        trade(THU, "fund_redemption", 120, 144),
    ]
    owner_ratio = Mock(return_value=Decimal(1))
    rows, gaps, anchor, covered = preview(trades, {OPEN: "1", MON: "1", TUE: "1.1", WED: "1.2"},
                                          owner_ratio=owner_ratio)
    owner_ratio.assert_called_once_with("synthetic")
    assert (anchor, covered, gaps) == (OPEN, 1, [])
    assert [Decimal(row["net_flow"]) for row in rows] == [0, 22, 0, -144, 0]
    assert [Decimal(row["market_value"]) for row in rows] == [100, 132, 144, 0, 0]
    assert [Decimal(row["display_return_rate"]) for row in rows][:2] == [0, Decimal("0.1")]
    assert rows[2]["display_return_rate"] == rows[3]["display_return_rate"] == rows[4]["display_return_rate"]


def test_exact_nav_gap_with_flow_cannot_be_bridged():
    trades = [trade(OPEN, "fund_subscription", 100, -100), trade(TUE, "fund_subscription", 20, -22)]
    snapshots = [{"account_code": "synthetic", "asset_id": "asset", "symbol": "123456", "as_of": FRI, "quantity": 120}]
    rows, gaps, _, covered = preview(trades, {OPEN: "1", MON: "1", WED: "1.2", THU: "1.3", FRI: "1.3"}, snapshots)
    assert covered == 1 and gaps[0]["dates"] == [TUE.isoformat()]
    assert [row["display_return_rate"] is None for row in rows] == [False, True, True, True, True]
    assert rows[1]["market_value"] is None
    assert Decimal(rows[2]["market_value"]) == 144


def test_evidence_or_snapshot_mismatch_excludes_entire_position_from_the_preview():
    snapshots = [{"account_code": "synthetic", "asset_id": "asset", "symbol": "123456", "as_of": FRI, "quantity": 101}]
    rows, gaps, anchor, covered = preview([trade(OPEN, "fund_subscription", 100, -100)], {MON: "1"}, snapshots)
    assert not covered and anchor is None
    assert all(row["display_return_rate"] is None for row in rows)
    assert gaps[0]["reason"] == "snapshot_quantity_mismatch"
