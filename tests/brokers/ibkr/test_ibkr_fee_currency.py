"""Fee units require explicit original evidence and never imply settlement."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
from psycopg2.extensions import connection
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend/api'))
from domain.brokers.ibkr.ibkr_fee_currency import currency_code, explicit_ibkr_fee_currency  # noqa: E402
from tests.brokers.ibkr.test_ibkr_sync_transaction import database as database, sync_case as sync_case, business_state, rows  # noqa: E402

XML = '''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U_TEST" toDate="20261022">
<AccountInformation accountId="U_TEST" currency="USD" />
<OpenPosition accountId="U_TEST" symbol="TEST" currency="USD" reportDate="20261022"
 position="2" markPrice="190.5" positionValue="381" costBasisMoney="340" assetCategory="STK" />
<CashReportCurrency currency="BASE_SUMMARY" fromDate="20261022" toDate="20261022"
 endingCash="-500" depositWithdrawalsYTD="100" commissions="-3" />
<CashReportCurrency accountId="U_TEST" currency="USD" fromDate="20261022" toDate="20261022" endingCash="-500" />
<Trades><Trade accountId="U_TEST" symbol="USD.MXN" assetCategory="CASH" currency="MXN" tradeID="FX1"
 tradeDate="20261022" buySell="BUY" quantity="100" tradePrice="20" tradeMoney="2000"
 ibCommission="-2" ibCommissionCurrency="USD" netCash="0" fxRateToBase="0.05" />
<Trade accountId="U_TEST" symbol="TEST" assetCategory="STK" currency="USD" tradeID="T1"
 tradeDate="20261022" buySell="BUY" quantity="2" tradePrice="170" tradeMoney="340"
 ibCommission="-1" ibCommissionCurrency="USD" netCash="-341" fxRateToBase="1" />
</Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''


@pytest.mark.parametrize('value,expected', [('USD', 'USD'), (' usd ', 'USD'), ('JPY', 'JPY'),
    ('', None), (None, None), (True, None), (123, None), ('US', None), ('USDD', None),
    ('12D', None), ('\uff35\uff33\uff24', None), ('u\u017fd', None), ('\u00dfD', None)])
def test_currency_codes_are_explicit_ascii_units(value, expected):
    assert currency_code(value) == expected


@pytest.mark.parametrize('commission,fee', [('-2', '2'), ('2', '2'), ('0', '0'), ('-0.0000000001', '0.0000000001')])
def test_signed_and_explicit_zero_fees_keep_their_unit_without_conversion(commission, fee):
    row = {'source_table': 'ibkr_flex_trade', 'currency': 'MXN',
           'raw_json': {'ibCommissionCurrency': 'USD', 'ibCommission': commission}}
    before = deepcopy(row)
    assert explicit_ibkr_fee_currency(row, Decimal(fee)) == 'USD' and row == before


@pytest.mark.parametrize('change', ['missing_unit', 'missing_amount', 'null', 'invalid_amount', 'nan',
    'infinite', 'boolean', 'conflicting_unit', 'duplicate_amount', 'wrong_source', 'wrong_amount', 'invalid_unit'])
def test_missing_or_conflicting_raw_evidence_is_not_guessed(change):
    row = {'source_table': 'ibkr_flex_trade', 'currency': 'USD',
           'raw_json': {'ibCommissionCurrency': 'USD', 'ibCommission': '-2'}}
    raw = row['raw_json']
    if change == 'missing_unit': raw.pop('ibCommissionCurrency')
    elif change == 'missing_amount': raw.pop('ibCommission')
    elif change == 'null': raw['ibCommission'] = None
    elif change == 'invalid_amount': raw['ibCommission'] = 'bad'
    elif change == 'nan': raw['ibCommission'] = 'NaN'
    elif change == 'infinite': raw['ibCommission'] = 'Infinity'
    elif change == 'boolean': raw['ibCommission'] = False
    elif change == 'conflicting_unit': raw['IBCOMMISSIONCURRENCY'] = 'JPY'
    elif change == 'duplicate_amount': raw['IBCOMMISSION'] = '-2'
    elif change == 'wrong_source': row['source_table'] = 'manual'
    elif change == 'invalid_unit': raw['ibCommissionCurrency'] = 'USDD'
    else: raw['ibCommission'] = '-3'
    assert explicit_ibkr_fee_currency(row, Decimal('2')) is None


def test_real_new_sync_preserves_fee_currency_and_replay_never_mutates(sync_case, database):
    client, _, _ = sync_case
    before = rows(database, 'pams_investment_trade_fact')
    response = client.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    after = rows(database, 'pams_investment_trade_fact')
    added = [row for row in after if row['trade_id'] not in {r['trade_id'] for r in before}]
    trade = next(row for row in added if row['source_table'] == 'ibkr_flex_trade')
    assert trade['fee_currency'] == 'USD'
    assert (trade['fee_amount'], trade['net_cash'], trade['net_cash_cny']) == (1.25, -341.25, -2388.75)
    assert all(row in after for row in before)
    response = client.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    assert response.json()['success']
    assert rows(database, 'pams_investment_trade_fact') == after


def test_real_currency_audit_is_server_readonly_after_migration(sync_case, database):
    from domain.brokers.ibkr.ibkr_reconciliation import read_ibkr_reconciliation
    client, _, report = sync_case
    report['xml'] = XML.replace('<OpenPosition accountId="U_TEST" symbol="TEST"', '<OpenPosition accountId="U_TEST" symbol="TEST" description="TEST STOCK"').replace('assetCategory="CASH"', 'assetCategory="CASH" multiplier="1"')
    response = client.post('/api/ibkr/flex/sync', json={})
    assert response.status_code == 200, response.text
    assert response.json()['success']
    before = business_state(database)

    class ReadOnlyConnection(connection):
        def set_session(self, *args, **kwargs):
            assert kwargs.get('readonly') and kwargs.get('isolation_level') == 'REPEATABLE READ'
            return super().set_session(*args, **kwargs)

    result = read_ibkr_reconciliation(connection_factory=lambda: database(connection_factory=ReadOnlyConnection))
    assert result['fee_review']['summary']['fact_fee_currency_matched_rows'] == 1
    fee = result['fee_review']['records'][0]
    assert 'cross_currency_fee' in fee['issues']
    assert fee['unit_review']['individual_cash_settlement'] == 'unverified'
    assert fee['source_authentication'] == 'unknown'
    assert business_state(database) == before
