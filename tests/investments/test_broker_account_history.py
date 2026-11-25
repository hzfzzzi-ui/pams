from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from domain.investments.valuation import broker_account_history as history
from domain.investments.trades import nav_trade_source_v2 as trades


def test_official_nav_and_financing_use_same_close_across_archives(monkeypatch):
    prior, opening, latest = (date.fromisoformat(d) for d in ("2025-12-30", "2025-12-31", "2026-01-02"))

    def close(nav, cash):
        return {"status": "ok", "identity_evidence": {"status": "ok"}, "base_currency": "USD",
                "nav_base": Decimal(nav), "cash_base": Decimal(cash), "source_sha256": "a"*64}

    def archive(cur, *, through=None):
        if through is None:
            return {"as_of": latest.isoformat(), "daily_closes": {
                opening.isoformat(): close("75", "-25"), latest.isoformat(): close("80", "10")}}
        assert through == opening
        return {"as_of": opening.isoformat(), "daily_closes": {
            prior.isoformat(): close("70", "-30"), opening.isoformat(): close("74", "-26")}}

    monkeypatch.setattr(history.daily_close, "read_published_portfolio_series", archive)
    params = {"account_code": history.ACCOUNT, "start_date": prior, "end_date": latest, "currency": "USD"}
    rows = history.broker_history_rows(None, **params)
    assert all(r["daily_close"]["source_sha256"] == "a"*64 and r["daily_close"]["as_of"] == r["date"].isoformat() for r in rows)
    assert [{k:r[k] for k in ("date","net_value","liabilities")} for r in rows] == [
        {"date": prior, "net_value": 70, "liabilities": 30},
        {"date": opening, "net_value": 75, "liabilities": 25},
        {"date": latest, "net_value": 80, "liabilities": 0},
    ]
    assert history.broker_history_rows(None, **{**params, "account_code": "other"}) == []
    monkeypatch.setattr(history.daily_close, "historical_rate", lambda *args, **kwargs: None)
    assert history.broker_history_rows(None, **{**params, "currency": "CNY"}) == []


def test_reconstruction_scopes_both_trades_and_originals_to_selected_account(monkeypatch):
    day = date(2026, 1, 2)
    facts = Mock(return_value=[{"source_item_id": "other-fact", "trade_date": day}])
    monkeypatch.setattr(trades, "load_trade_rows", facts)
    cur = Mock()
    cur.fetchall.return_value = []
    points = history.reconstructed_history_rows(cur, account_code="selected", start_date=day, end_date=day,
                                               currency="CNY")
    assert facts.call_args.kwargs["account_code"] == "selected"
    query, params = cur.execute.call_args.args
    assert "r.account_code=%s" in query and params == ("selected",)
    assert points == []


def test_carried_broker_cash_requires_fresh_prices_for_every_open_position(monkeypatch):
    trade_day, priced, missing, weekend = [date(2026, 1, day) for day in (7, 8, 9, 10)]
    events = [
        dict(trade_date=trade_day, trade_type="buy", source_digest="sheet", net_cash=-10, broker_cash=20,
             source_market="深圳Ａ股", symbol="000001", quantity=Decimal(1)),
        dict(trade_date=trade_day, trade_type="buy", source_digest="sheet", net_cash=-10, broker_cash=10,
             source_market="深圳Ａ股", symbol="000002", quantity=Decimal(1)),
    ]
    monkeypatch.setattr(trades, "_read_event_prefix", lambda cur, account_code: (events, None))
    cur = Mock()
    cur.fetchall.return_value = [
        {"symbol": symbol, "as_of": day, "close_price": Decimal(amount)}
        for symbol, day, amount in (("sz000001", trade_day, 10), ("sz000002", trade_day, 10),
                                    ("sz000001", priced, 12), ("sz000002", priced, 10),
                                    ("sz000001", missing, 13))
    ]
    def rows(first, last):
        return history.reconstructed_history_rows(cur, account_code="broker", start_date=first, end_date=last,
                                                  currency="CNY")
    assert rows(trade_day, weekend) == [
        {"date": trade_day, "net_value": 30, "net_value_status": "estimated"},
        {"date": priced, "net_value": 32, "net_value_status": "estimated"},
    ]
    # A fully cash position has no new market valuation after the final entry.
    events[:] = [dict(trade_date=trade_day, trade_type="deposit", source_digest="sheet",
                      net_cash=10, broker_cash=10)]
    assert rows(priced, weekend) == []
