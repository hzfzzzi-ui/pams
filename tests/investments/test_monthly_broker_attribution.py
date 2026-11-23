from datetime import date
from decimal import Decimal as D
from xml.etree import ElementTree as ET

import pytest

from domain.brokers.ibkr.ibkr_daily_close import FLOW_FIELDS, _Evidence
from domain.brokers.ibkr.ibkr_monthly_attribution import _convert, _movements
from domain.ledger.ledger_view import _monthly_close


def test_multi_currency_statement_matches_nav_less_dated_funding_and_counts_cash_once():
    start, end = date(2030, 1, 31), date(2030, 2, 1)
    root = ET.Element('FlexStatements')
    account = 'TEST'
    closes = {}
    for day, nav, position in [(start, '100', '500'), (end, '147', '900')]:
        stamp = day.strftime('%Y%m%d')
        st = ET.SubElement(root, 'FlexStatement', accountId=account, fromDate=stamp, toDate=stamp)
        positions = ET.SubElement(st, 'OpenPositions')
        ET.SubElement(positions, 'OpenPosition', currency='HKD', symbol='X', conid='1',
                      positionValue=position, fxRateToBase='0.2', levelOfDetail='SUMMARY')
        closes[day.isoformat()] = dict(nav_base=D(nav), position_value_base=D(position)*D('.2'),
                                       base_currency='USD', status='ok', identity_evidence={'status': 'ok'})
    ET.SubElement(st, 'ChangeInNAV', accountId=account, currency='USD', fromDate=stamp, toDate=stamp,
                  startingValue='100', endingValue='147', mtm='30', interest='-2', commissions='-1',
                  **{key: '20' if key == 'depositsWithdrawals' else '0' for key in FLOW_FIELDS})
    trades = ET.SubElement(st, 'Trades')
    attrs = dict(accountId=account, currency='HKD', fxRateToBase='0.2', tradeDate=stamp, levelOfDetail='EXECUTION')
    ET.SubElement(trades, 'Trade', **attrs, assetCategory='STK', tradeID='1', conid='1', symbol='X', proceeds='-300')
    ET.SubElement(trades, 'Trade', **attrs, assetCategory='CASH', tradeID='2', symbol='USD.HKD',
                  proceeds='-100', quantity='22')
    ET.SubElement(st, 'CashReportCurrency', accountId=account, currency='BASE_SUMMARY', fxTranslationGainLoss='8')
    cash = ET.SubElement(st, 'CashTransactions')
    ET.SubElement(cash, 'CashTransaction', accountId=account, type='Broker Interest Paid', currency='USD',
                  amount='-2', reportDate=stamp, dateTime=stamp, transactionID='T1', levelOfDetail='DETAIL')
    evidence = _Evidence(root, account)
    parts, flows, cash = _movements(evidence, closes, [end], start)
    result = _convert(parts, flows, opening_nav=D(100), opening_rate=D('6.8'), closing_rate=D(7),
                      rates={end: D('6.9')}, share=D('.5'))
    assert result['overseas', 'HKD', 'X'] == D(70)
    assert result['fx', 'USD', '外币现金折算'] == D(28)
    assert result['fx', 'USD', '换汇成交价差'] == D(7)
    assert sum(result.values()) == (D(147)*7 - D(100)*D('6.8') - D(20)*D('6.9'))/2
    assert list(cash.values()) == [(end, 'USD', D(-2))]
    booked = dict(account_code='shared', transaction_date_obj=end, amount=-2, amount_cny=-14,
                  classification_status='confirmed', source_evidence_status='verified',
                  category='金融费用', subcategory='利息', flow_type='expense', source_item_id='source')
    ledger = _monthly_close([booked, {**booked, 'source_item_id':'other', 'amount_cny':-8}], [], end, end,
                            redesigned=True, account_profiles={}, counted_income_sources=set(),
                            statement_cash={'source'})
    assert ledger['totals']['expense'] == 8
    closes[end.isoformat()]['nav_base'] += 1
    with pytest.raises(ValueError, match='noncontiguous'):
        _movements(evidence, closes, [end], start)

