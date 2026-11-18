from backend.api.domain.investments.holdings_account_cards import build_holdings_account_cards


def test_card_uses_its_displayed_account_positions_not_stale_account_total():
    cards = build_holdings_account_cards(
        positions={"as_of": "2026-11-18", "by_account": [
            {"account_code": "tonghuashun", "known_value_base": "152942.79"}]},
        overview={}, stock_performance=None,
        stock={"as_of": "2026-11-18", "rows": [
            {"account": "tonghuashun", "market_value_cny": "100000"},
            {"account": "tonghuashun", "market_value_cny": "51381.40"},
            {"account": "other", "market_value_cny": "20000"},
            {"account": "tonghuashun", "market_value_cny": "100", "is_closed": True},
        ]}, funds={}, overseas={}, closed_returns={}, summary={},
    )
    assert cards["tonghuashun"]["market_value"]["value"] == "151381.40"


def test_card_does_not_invent_total_for_unknown_active_position():
    cards = build_holdings_account_cards(
        positions={"as_of": "2026-11-18", "by_account": [
            {"account_code": "tonghuashun", "known_value_base": "999"}]},
        overview={}, stock_performance=None,
        stock={"as_of": "2026-11-18", "rows": [
            {"account": "tonghuashun", "market_value_cny": "100"},
            {"account": "tonghuashun", "market_value_cny": None},
        ]}, funds={}, overseas={}, closed_returns={}, summary={},
    )
    assert cards["tonghuashun"]["market_value"]["value"] is None
