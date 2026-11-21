from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from domain.investments import broker_account_history as history
from domain.investments import nav_trade_source_v2 as trades


def test_official_nav_and_financing_use_same_close_across_archives(monkeypatch):
    prior, opening, latest = (date.fromisoformat(d) for d in ("2025-12-30", "2025-12-31", "2026-01-02"))

    def close(nav, cash):
        return {"status": "ok", "identity_evidence": {"status": "ok"}, "base_currency": "USD",
                "nav_base": Decimal(nav), "cash_base": Decimal(cash)}

    def archive(cur, *, through=None):
        if through is None:
            return {"as_of": latest.isoformat(), "daily_closes": {
                opening.isoformat(): close("75", "-25"), latest.isoformat(): close("80", "10")}}
        assert through == opening
        return {"as_of": opening.isoformat(), "daily_closes": {
            prior.isoformat(): close("70", "-30"), opening.isoformat(): close("74", "-26")}}

    monkeypatch.setattr(history.daily_close, "read_published_portfolio_series", archive)
    params = {"account_code": history.ACCOUNT, "start_date": prior, "end_date": latest, "currency": "USD"}
    assert history.broker_history_rows(None, **params) == [
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
