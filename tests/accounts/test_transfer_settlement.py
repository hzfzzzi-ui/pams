"""Funding can enter a daily broker NAV after the cash transaction's business date."""
from datetime import date
from decimal import Decimal as D
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest

from domain.accounts.balances import transfer_settlement as settlement
from domain.brokers.ibkr.funding_marks import _funding_day
from domain.brokers.ibkr.ibkr_daily_close import _Evidence


def test_daily_funding_requires_both_currency_legs_and_the_daily_capital_total():
    day = date(2024, 2, 1)
    st = ET.fromstring('''<FlexStatement accountId="DEMO" fromDate="20240201" toDate="20240201">
      <CashTransactions><CashTransaction accountId="DEMO" amount="40" currency="CAD"
        dateTime="20240131" settleDate="20240131" reportDate="20240201"
        transactionID="abc" type="Deposits/Withdrawals" /></CashTransactions>
      <StatementOfFunds><StatementOfFundsLine accountId="DEMO" transactionID="abc" activityCode="DEP"
        levelOfDetail="Currency" currency="CAD" amount="40" date="20240131" reportDate="20240201"/>
        <StatementOfFundsLine accountId="DEMO" transactionID="abc" activityCode="DEP"
        levelOfDetail="BaseCurrency" currency="USD" amount="30" date="20240131" reportDate="20240201"/>
      </StatementOfFunds><ChangeInNAV depositsWithdrawals="30"/></FlexStatement>''')
    def verify():
        return _funding_day(st, _Evidence(st, 'DEMO'), day, {'base_currency': 'USD'})
    assert verify()[0][1:] == ('2024-01-31', 'CAD', D(40), day, 'USD', D(30))
    st.find('ChangeInNAV').set('depositsWithdrawals', '29')
    with pytest.raises(ValueError):
        verify()
    st.find('ChangeInNAV').set('depositsWithdrawals', '30')
    st.find('StatementOfFunds')[0].set('transactionID', 'other')
    with pytest.raises(ValueError):
        verify()


def test_broker_custody_and_fx_use_independent_settlement_and_business_dates(monkeypatch):
    jan, feb, received = date(2024, 1, 31), date(2024, 2, 29), date(2024, 2, 1)
    cur = Mock()
    cur.fetchall.return_value = [{'confirmation_id': 'test', 'sent': jan, 'received': jan}]
    members = {'left': {'owner_ratio': D('0.5')},
               'right': {'owner_ratio': D('0.5'), 'method': 'broker_cash_legs_and_executions'}}
    pair = {'confirmation_id': 'test', 'evidence_status': 'confirmed',
            'from_evidence': {'currency': 'CAD', 'amount': '-40', 'account_code': 'left', 'day': '2024-01-30'},
            'to_evidence': {'currency': 'CAD', 'amount': '40', 'account_code': 'right', 'id': 1}}
    monkeypatch.setattr(settlement.confirmed_transfers_v2, 'read', lambda *a, **kw: [pair])
    marks = {1: (received, 'USD', D(30))}
    monkeypatch.setattr(settlement, 'funding_marks', lambda *a: marks)
    closes = {('CAD', jan): D(5)}
    flows = {('CAD', date(2024, 1, 30)): D('4.9'), ('USD', received): D(7)}
    def run():
        changes = {}
        result = settlement.cash_in_transit(cur, {jan:members, feb:members}, lambda u,d: closes.get((u,d)),
                                            fx_changes=changes, flow_rate_at=lambda u,d: flows.get((u,d)))
        return result, changes
    balances, changes = run()
    assert balances[jan][0]['personal'] == 100 and feb not in balances
    assert changes == {jan: D(2), feb: D(5)}
    flows.clear()
    balances, changes = run()
    assert balances[jan][0]['personal'] == 100 and changes == {jan: None, feb: None}
    marks[1] = (received, None, None)
    balances, changes = run()
    assert balances[jan][0]['personal'] is None and changes == {jan: None, feb: None}
