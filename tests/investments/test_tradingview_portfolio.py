from datetime import date
from decimal import Decimal

from backend.api.collectors.tradingview_portfolio import (
    MarketMark,
    calculate_portfolio,
    parse_transactions_csv,
    read_transactions,
)


def test_parse_transactions_csv_mixed_sides(tmp_path):
    path = tmp_path / "a_share.csv"
    path.write_text(
        "Symbol,Side,Qty,Fill Price,Commission,Closing Time\n"
        "$CASH,Deposit,1000,,,2026-05-01 00:00:00\n"
        "SSE:601899,Buy,10,10,1,2026-05-02 01:30:00\n"
        "SSE:601899,Dividend,2,,,2026-05-03 00:00:00\n",
        encoding="utf-8",
    )

    rows = parse_transactions_csv(path)

    assert [row.side for row in rows] == ["Deposit", "Buy", "Dividend"]
    assert rows[1].normalized_symbol == "601899"
    assert rows[1].commission == Decimal("1")


def test_read_transactions_deduplicates_repeated_export(tmp_path):
    content = (
        "Symbol,Side,Qty,Fill Price,Commission,Closing Time\n"
        "$CASH,Deposit,1000,,,2026-05-01 00:00:00\n"
    )
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")

    rows, raw_count = read_transactions([first, second])

    assert raw_count == 2
    assert len(rows) == 1


def test_calculate_portfolio_matches_cash_and_nav():
    rows = [
        # source_file, source_hash, row_hash, symbol, normalized_symbol, side,
        # quantity, fill_price, commission, closing_time, raw
    ]
    import datetime as dt
    from backend.api.collectors.tradingview_portfolio import TradingViewTransaction

    rows.extend(
        [
            TradingViewTransaction("x.csv", "h", "1", "$CASH", None, "Deposit", Decimal("1000"), None, Decimal("0"), dt.datetime(2026, 5, 1), {}),
            TradingViewTransaction("x.csv", "h", "2", "SSE:601899", "601899", "Buy", Decimal("10"), Decimal("10"), Decimal("1"), dt.datetime(2026, 5, 2), {}),
            TradingViewTransaction("x.csv", "h", "3", "SSE:601899", "601899", "Dividend", Decimal("2"), None, Decimal("0"), dt.datetime(2026, 5, 3), {}),
        ]
    )

    result = calculate_portfolio(
        rows,
        {"601899": MarketMark(price=Decimal("12"), name="紫金矿业", unrealized_pnl=Decimal("20"))},
        as_of=date(2026, 5, 31),
    )

    assert result.cash == Decimal("901")
    assert result.holdings_market_value == Decimal("120")
    assert result.total_nav == Decimal("1021")
    assert result.holding_pnl == Decimal("20")
    assert result.realized_pnl == Decimal("1")
