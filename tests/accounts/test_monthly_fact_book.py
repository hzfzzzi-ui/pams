from datetime import date
from decimal import Decimal

from domain.accounts.monthly.monthly_fact_book import cash_book
from domain.brokers.ibkr.fact_replay import replay_xml


def test_posted_opening_and_trade_date_opening_do_not_count_the_same_charge_twice():
    origin, jan, feb = date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28)

    def leg(trade_day, posted_day, amount):
        return dict(
            transaction_date=trade_day,
            posting_date=posted_day,
            amount=Decimal(amount),
            currency="CNY",
            source_evidence_status="verified",
        )

    legs = [leg(origin, jan, "-4"), leg(jan, None, "2")]
    assert cash_book(Decimal(-8), "CNY", legs, origin, [jan, feb]) == {
        jan: -10,
        feb: -10,
    }
    assert cash_book(
        Decimal(-12), "CNY", legs, origin, [jan, feb], transaction_opening=True
    ) == {jan: -10, feb: -10}
    assert cash_book(None, "CNY", [], origin, [jan]) == {jan: None}
    assert cash_book(Decimal(0), "CNY", [], origin, [jan]) == {jan: 0}


def test_broker_cash_shares_and_interest_use_movements_even_when_statement_total_differs():
    opening, close = date(2025, 12, 31), date(2026, 1, 1)
    xml = b"""<FlexQueryResponse><FlexStatement accountId="test" fromDate="20260101" toDate="20260101">
      <EquitySummaryByReportDateInBase reportDate="20251231" totalLong="0" totalShort="0"/>
      <StatementOfFundsLine levelOfDetail="Currency" activityCode="BUY" currency="USD" amount="-12"/>
      <Trade tradeID="1" conid="x" assetCategory="STK" levelOfDetail="EXECUTION" quantity="2"/>
      <OpenPosition levelOfDetail="SUMMARY" conid="x" markPrice="8" fxRateToBase="1"/>
      <InterestAccrualsCurrency currency="USD" interestAccrued="-1" accrualReversal="0"/>
      <ChangeInNAV endingValue="9999" changeInInterestAccruals="-1"/>
    </FlexStatement></FlexQueryResponse>"""
    source = {
        "account_id": "test",
        "daily_closes": {
            str(close): {"base_currency": "USD", "identity_evidence": {"status": "ok"}}
        },
    }
    assert replay_xml(xml, source, opening, [close])[close] == (3, 16, 13, close)
    assert replay_xml(xml, source, opening, [close], opening=Decimal(5)) == {}


def test_verified_transfer_only_contributes_while_between_both_accounts(monkeypatch):
    from unittest.mock import Mock
    from domain.accounts.monthly.monthly_fact_book import cash_in_transit
    from domain.ledger.transfers import confirmed_transfers_v2

    start, end = date(2026, 1, 31), date(2026, 2, 2)
    cur = Mock()
    cur.fetchall.return_value = [
        {"confirmation_id": "x", "sent": start, "received": end}
    ]
    pair = {
        "confirmation_id": "x",
        "evidence_status": "confirmed",
        "from_evidence": {"currency": "USD", "amount": "-10", "account_code": "bank"},
        "to_evidence": {"currency": "USD", "amount": "10", "account_code": "broker"},
    }
    monkeypatch.setattr(confirmed_transfers_v2, "read", lambda *a, **kw: [pair])
    members = {
        "bank": {"owner_ratio": Decimal(1)},
        "broker": {"owner_ratio": Decimal(1)},
    }
    books = {start: members, end: members}
    assert (
        cash_in_transit(cur, books, lambda u, d: Decimal(7))[start][0]["personal"] == 70
    )
    assert end not in cash_in_transit(cur, books, lambda u, d: Decimal(7))
    pair["evidence_status"] = "revoked"
    assert not cash_in_transit(cur, books, lambda u, d: Decimal(7))
