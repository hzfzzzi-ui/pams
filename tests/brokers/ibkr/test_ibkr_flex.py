from datetime import date

import pytest

from backend.api.collectors.ibkr_flex import (
    IBKRFlexError,
    parse_flex_report,
    parse_flex_send_response,
)


def test_parse_flex_send_response_success():
    xml = """
    <FlexStatementResponse timestamp="2026-05-30">
      <Status>Success</Status>
      <ReferenceCode>1234567890</ReferenceCode>
      <url>https://example.test/GetStatement</url>
    </FlexStatementResponse>
    """

    result = parse_flex_send_response(xml)

    assert result["reference_code"] == "1234567890"
    assert result["url"] == "https://example.test/GetStatement"


def test_parse_flex_send_response_error():
    xml = """
    <FlexStatementResponse>
      <Status>Fail</Status>
      <ErrorCode>1012</ErrorCode>
      <ErrorMessage>Token has expired.</ErrorMessage>
    </FlexStatementResponse>
    """

    with pytest.raises(IBKRFlexError, match="Token has expired"):
        parse_flex_send_response(xml)


def test_parse_flex_report_positions_and_cash():
    xml = """
    <FlexQueryResponse>
      <FlexStatements count="1">
        <FlexStatement accountId="U1234567" fromDate="20260529" toDate="20260530">
          <OpenPositions>
            <OpenPosition accountId="U1234567" symbol="AAPL" description="APPLE INC"
              currency="USD" reportDate="20260530" position="10" markPrice="190.5"
              marketValue="1905" costBasisMoney="1700" costBasisPrice="170" assetCategory="STK" />
            <OpenPosition accountId="U1234567" symbol="SPY" description="SPDR S&amp;P 500 ETF TRUST"
              currency="USD" reportDate="20260530" position="2" markPrice="520"
              marketValue="1040" costBasisMoney="900" costBasisPrice="450" assetCategory="STK" />
          </OpenPositions>
          <CashReport>
            <CashReportCurrency accountId="U1234567" currency="USD" endingCash="1234.56" />
          </CashReport>
          <Trades>
            <Trade accountId="U1234567" symbol="AAPL" description="APPLE INC"
              currency="USD" tradeID="T1" transactionID="TX1" tradeDate="20260530"
              buySell="BUY" quantity="10" tradePrice="170" tradeMoney="1700"
              ibCommission="-1.25" netCash="-1701.25" fifoPnlRealized="0" />
          </Trades>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """

    result = parse_flex_report(xml)

    assert result.statement_as_of == date(2026, 5, 30)
    assert result.source_account_ids == ["U1234567"]
    assert [row["symbol"] for row in result.rows] == ["AAPL", "SPY", "CASH_USD"]
    assert [row["symbol"] for row in result.history_rows] == ["AAPL", "SPY", "CASH_USD"]
    assert result.rows[0]["market_value"] == 1905
    assert result.rows[0]["major_category"] == "美股"
    assert result.rows[2]["major_category"] == "现金"
    assert result.rows[2]["minor_category"] == "现金/活期"
    assert len(result.trades) == 1
    assert result.trades[0]["symbol"] == "AAPL"
    assert result.trades[0]["trade_date"] == date(2026, 5, 30)
    assert result.trades[0]["side"] == "BUY"
    assert result.trades[0]["net_cash"] == -1701.25


def test_parse_flex_report_current_rows_use_latest_statement_only():
    xml = """
    <FlexQueryResponse>
      <FlexStatements count="2">
        <FlexStatement accountId="U1234567" fromDate="20260528" toDate="20260528">
          <OpenPositions>
            <OpenPosition accountId="U1234567" symbol="AAPL" description="APPLE INC"
              currency="USD" reportDate="20260528" position="10" markPrice="190"
              marketValue="1900" costBasisMoney="1700" costBasisPrice="170" assetCategory="STK" />
          </OpenPositions>
        </FlexStatement>
        <FlexStatement accountId="U1234567" fromDate="20260529" toDate="20260529">
          <OpenPositions>
            <OpenPosition accountId="U1234567" symbol="MSFT" description="MICROSOFT CORP"
              currency="USD" reportDate="20260529" position="5" markPrice="420"
              marketValue="2100" costBasisMoney="2000" costBasisPrice="400" assetCategory="STK" />
          </OpenPositions>
          <CashReport>
            <CashReportCurrency accountId="U1234567" currency="USD" endingCash="250" />
          </CashReport>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """

    result = parse_flex_report(xml)

    assert result.statement_as_of == date(2026, 5, 29)
    assert [row["as_of"] for row in result.history_rows] == [
        date(2026, 5, 28),
        date(2026, 5, 29),
        date(2026, 5, 29),
    ]
    assert [row["symbol"] for row in result.rows] == ["MSFT", "CASH_USD"]


def test_parse_flex_report_negative_cash_as_liability():
    xml = """
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U1234567" toDate="20260530">
          <CashReport>
            <CashReportCurrency accountId="U1234567" currency="USD" endingCash="-50.25" />
          </CashReport>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """

    result = parse_flex_report(xml)

    assert result.rows[0]["symbol"] == "CASH_USD"
    assert result.rows[0]["asset_liability"] == "负债"
    assert result.rows[0]["major_category"] == "借款"
    assert result.rows[0]["market_value"] == -50.25


def test_parse_flex_report_skips_base_summary_cash_row():
    xml = """
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U1234567" toDate="20260530">
          <CashReport>
            <CashReportCurrency currency="BASE_SUMMARY" endingCash="999999" />
            <CashReportCurrency currency="USD" endingCash="100" />
          </CashReport>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """

    result = parse_flex_report(xml)

    assert [row["currency"] for row in result.rows] == ["USD"]
    assert result.rows[0]["market_value"] == 100


def test_parse_flex_report_extracts_currency_cash_deposits_for_confirmation():
    xml = """
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U1234567" toDate="20260619">
          <CashReport>
            <CashReportCurrency currency="BASE_SUMMARY" toDate="20260619"
              deposits="1673.01" depositWithdrawals="1673.01" endingCash="100" />
            <CashReportCurrency accountId="U1234567" currency="MXN" toDate="20260619"
              deposits="29000" depositWithdrawals="29000" endingCash="108999.99" />
          </CashReport>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """

    result = parse_flex_report(xml)

    assert len(result.cash_movements) == 1
    movement = result.cash_movements[0]
    assert movement["movement_type"] == "deposit"
    assert movement["movement_date"] == date(2026, 6, 19)
    assert movement["currency"] == "MXN"
    assert movement["amount"] == 29000
