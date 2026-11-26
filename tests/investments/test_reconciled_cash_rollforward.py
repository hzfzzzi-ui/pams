"""Tests for rolling forward Tonghuashun cash across verified trade facts lacking 本次金额."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from domain.investments.trades import nav_trade_source_v2 as mod


def test_read_reconciled_cash_rolls_forward_verified_history_trades():
    anchor_events = [
        {
            "trade_date": date(2026, 10, 23),
            "net_cash": Decimal("-13495.13"),
            "broker_cash": Decimal("20118.49"),
            "source_digest": "abc",
        }
    ]
    subsequent_facts = [
        {"trade_date": date(2026, 11, 18), "trade_type": "dividend", "currency": "CNY", "net_cash": Decimal("74.00"), "source_evidence_status": "verified"},
        {"trade_date": date(2026, 11, 19), "trade_type": "buy", "currency": "CNY", "net_cash": Decimal("-11865.12"), "source_evidence_status": "verified"},
        {"trade_date": date(2026, 11, 24), "trade_type": "buy", "currency": "CNY", "net_cash": Decimal("-7325.07"), "source_evidence_status": "verified"},
    ]
    with patch.object(mod, "_read_event_prefix", return_value=(anchor_events, date(2026, 11, 18))), \
         patch.object(mod, "load_trade_rows", return_value=subsequent_facts):
        day, cash = mod.read_reconciled_cash(None, as_of=date(2026, 11, 25))
    assert day == date(2026, 11, 24)
    assert cash == Decimal("1002.30")


def test_read_reconciled_cash_returns_none_if_rollforward_goes_negative():
    anchor_events = [
        {
            "trade_date": date(2026, 10, 23),
            "net_cash": Decimal("0"),
            "broker_cash": Decimal("1000.00"),
            "source_digest": "abc",
        }
    ]
    subsequent_facts = [
        {"trade_date": date(2026, 11, 19), "trade_type": "buy", "currency": "CNY", "net_cash": Decimal("-5000.00"), "source_evidence_status": "verified"},
    ]
    with patch.object(mod, "_read_event_prefix", return_value=(anchor_events, date(2026, 11, 19))), \
         patch.object(mod, "load_trade_rows", return_value=subsequent_facts):
        assert mod.read_reconciled_cash(None, as_of=date(2026, 11, 25)) is None
