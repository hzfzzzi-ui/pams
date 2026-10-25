"""Synthetic, parameterized official evidence; no database or real account inputs."""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/api'))
from services.ibkr_fee_settlement import review_fee_settlement


QUALITY = {'status': 'ok', 'legacy_unverified': False, 'statement_as_of': '2031-04-18',
           'expected_statement_as_of': '2031-04-18'}


def case(*, account='SYNTH_A', execution='arbitrary-17', currency='CAD', fee='-1.25'):
    trade = {'accountId': account, 'tradeID': execution, 'transactionID': 'tx-' + execution,
             'symbol': 'SYNTHETIC', 'assetCategory': 'STK', 'buySell': 'BUY', 'levelOfDetail': 'EXECUTION',
             'tradeDate': '20310416', 'settleDateTarget': '20310418', 'currency': currency,
             'ibCommissionCurrency': currency, 'ibCommission': fee, 'proceeds': '-100',
             'taxes': '0', 'netCash': str(Decimal('-100') + Decimal(fee))}
    line = {'accountId': account, 'tradeID': execution, 'transactionID': 'tx-' + execution,
            'symbol': 'SYNTHETIC', 'assetCategory': 'STK', 'buySell': 'BUY', 'activityCode': 'BUY',
            'levelOfDetail': 'Currency', 'date': '20310416', 'settleDate': '20310418',
            'currency': currency, 'tradeGross': '-100', 'tradeCommission': fee, 'tradeTax': '0',
            'amount': trade['netCash'], 'debit': trade['netCash'], 'credit': '0'}
    return {'trades': [trade], 'lines': [line], 'quality': deepcopy(QUALITY)}


def xml_text(data):
    statement = ET.Element('FlexStatement', accountId=data['trades'][0]['accountId'], toDate='20310418')
    trades = ET.SubElement(statement, 'Trades')
    for trade in data['trades']:
        ET.SubElement(trades, 'Trade', trade)
    funds = ET.SubElement(statement, 'StatementOfFunds')
    for line in data['lines']:
        ET.SubElement(funds, 'StatementOfFundsLine', line)
    return ET.tostring(statement, encoding='unicode')


def review(data):
    return review_fee_settlement(xml_text=xml_text(data), quality=data['quality'])


def record(data):
    return review(data)['records'][0]


def separate(data):
    line = data['lines'][0]
    line.update(activityCode='ADJ', tradeGross='0', tradeTax='0', amount=line['tradeCommission'],
                debit=line['tradeCommission'], credit='0')
    return data


@pytest.mark.parametrize('account,execution,currency', [
    ('SYNTH_A', 'arbitrary-17', 'CAD'), ('not-an-IB-account-pattern', 'X/Y:42', 'JPY'),
    ('PORTFOLIO_992', 'another-execution', 'CHF'),
])
@pytest.mark.parametrize('classification', ['included', 'separate', 'explicit_zero'])
def test_rules_are_independent_of_account_execution_currency_or_security(account, execution, currency, classification):
    data = case(account=account, execution=execution, currency=currency,
                fee='0' if classification == 'explicit_zero' else '-1.25')
    data['trades'][0]['symbol'] = data['lines'][0]['symbol'] = 'UNRELATED-SYNTHETIC'
    if classification == 'separate':
        separate(data)
    before = deepcopy(data)
    result, item = review(data), record(data)
    assert data == before
    assert item['status'] == 'sufficient' and item['classification'] == classification
    assert item['individual_cash_settlement_verified'] == (classification != 'explicit_zero')
    assert item['report_line_arithmetic'] is True and item['funds_line_indices'] == [0]
    assert result['status'] == 'review' and result['read_only']
    assert not result['executable'] and not result['financial_values_changed']
    assert not item['additional_deduction_proposed'] and not result['additional_deduction_proposed']
    assert not result['daily_aggregate_is_individual_proof']
    assert result['report_sha256'] == hashlib.sha256(xml_text(data).encode()).hexdigest()
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('field', ['ibCommission', 'ibCommissionCurrency', 'currency', 'tradeDate',
                                 'levelOfDetail', 'assetCategory', 'symbol', 'buySell'])
@pytest.mark.parametrize('value', ['', None])
def test_missing_explicit_trade_fields_never_use_defaults(field, value):
    data = case()
    if value is None:
        data['trades'][0].pop(field)
    else:
        data['trades'][0][field] = value
    assert record(data)['status'] == 'unverified'


@pytest.mark.parametrize('value', ['', 'NaN', 'sNaN', 'Infinity', '-Infinity', 'not-a-number'])
def test_bad_trade_fee_is_not_zero(value):
    data = case()
    data['trades'][0]['ibCommission'] = value
    item = record(data)
    assert item['status'] == 'unverified' and item['classification'] is None
    assert item['reasons'] == ['missing_or_invalid_explicit_commission']


@pytest.mark.parametrize('field', ['amount', 'debit', 'credit', 'tradeGross', 'tradeCommission', 'tradeTax'])
@pytest.mark.parametrize('value', ['', 'NaN', 'Infinity', 'bad'])
def test_missing_or_nonfinite_cash_components_are_not_zero(field, value):
    data = case()
    data['lines'][0][field] = value
    item = record(data)
    assert item['status'] == 'unverified' and item['reasons'] == ['missing_or_invalid_funds_number']


@pytest.mark.parametrize('fee', ['-1.25', '1.25', '0', '-0.0000'])
def test_commission_sign_and_explicit_zero_are_preserved(fee):
    data = case(fee=fee)
    item = record(data)
    assert item['status'] == 'sufficient' and item['official_commission_signed'] == fee
    assert item['classification'] == ('explicit_zero' if Decimal(fee) == 0 else 'included')
    if Decimal(fee) != 0:
        data['lines'][0]['tradeCommission'] = str(-Decimal(fee))
        assert record(data)['reasons'] == ['signed_commission_mismatch']


@pytest.mark.parametrize('source', ['trades', 'lines'])
@pytest.mark.parametrize('changed', [False, True])
def test_multiple_duplicate_or_conflicting_identities_are_never_first_selected(source, changed):
    data = case()
    duplicate = deepcopy(data[source][0])
    if changed:
        duplicate['tradeCommission' if source == 'lines' else 'ibCommission'] = '-99'
    data[source].append(duplicate)
    assert all(item['status'] == 'conflict' for item in review(data)['records'])


def test_secondary_transaction_identity_is_unique_and_must_not_contradict_trade_id():
    data = case()
    data['trades'].append({**data['trades'][0], 'tradeID': 'different-primary'})
    assert all(item['reasons'] == ['duplicate_trade_identity'] for item in review(data)['records'])
    data = case()
    data['lines'][0]['transactionID'] = 'contradictory-secondary'
    assert record(data)['reasons'] == ['conflicting_execution_identity']


@pytest.mark.parametrize('field', ['tradeID', 'transactionID'])
def test_single_available_stable_identity_can_link_but_no_identity_cannot(field):
    data = case()
    for row in (data['trades'][0], data['lines'][0]):
        row.pop(field)
    assert record(data)['status'] == 'sufficient'
    for row in (data['trades'][0], data['lines'][0]):
        row.pop('transactionID' if field == 'tradeID' else 'tradeID')
    assert record(data)['status'] == 'unverified'


@pytest.mark.parametrize('field,value,reason', [
    ('symbol', 'WRONG', 'instrument_scope_mismatch'),
    ('assetCategory', 'OPT', 'instrument_scope_mismatch'),
    ('buySell', 'SELL', 'direction_scope_mismatch'),
    ('date', '20310417', 'transaction_date_not_proven'),
    ('date', '', 'transaction_date_not_proven'),
    ('settleDate', '20310419', 'settlement_date_not_proven'),
    ('settleDate', '', 'settlement_date_not_proven'),
    ('currency', 'EUR', 'fee_currency_not_proven_no_fx_conversion'),
    ('activityCode', 'OFEE', 'unsupported_cash_activity_semantics'),
    ('activityCode', 'DIV', 'unsupported_cash_activity_semantics'),
    ('levelOfDetail', 'DailySummary', 'aggregate_or_unknown_funds_scope'),
    ('levelOfDetail', 'ORDER', 'aggregate_or_unknown_funds_scope'),
    ('relatedTradeID', 'another', 'related_or_adjusted_identity_not_supported'),
])
def test_scope_date_and_semantic_proof_cannot_be_replaced_by_amount_equality(field, value, reason):
    data = case()
    data['lines'][0][field] = value
    item = record(data)
    assert item['status'] in {'unverified', 'conflict'} and item['reasons'] == [reason]
    assert not item['individual_cash_settlement_verified']


@pytest.mark.parametrize('field', ['accountId', 'tradeID', 'transactionID'])
def test_wrong_identity_never_links_by_symbol_amount_or_nearest_date(field):
    data = case()
    data['lines'][0][field] = 'wrong'
    if field != 'accountId':
        data['lines'][0].pop('transactionID' if field == 'tradeID' else 'tradeID')
    item = record(data)
    assert item['status'] == 'unverified' and item['candidate_line_count'] == 0


def test_identical_ids_in_other_accounts_are_not_duplicates_or_candidates():
    data = case()
    data['lines'].append({**data['lines'][0], 'accountId': 'OTHER-SYNTHETIC-ACCOUNT'})
    assert record(data)['status'] == 'sufficient' and record(data)['candidate_line_count'] == 1


def test_daily_cash_report_even_exactly_equal_never_proves_individual_settlement():
    data = case()
    data['lines'] = []
    text = xml_text(data).replace('</FlexStatement>',
        '<CashReportCurrency currency="BASE_SUMMARY" fromDate="20310416" toDate="20310416" '
        'commissions="-1.25" /></FlexStatement>')
    item = review_fee_settlement(xml_text=text, quality=QUALITY)['records'][0]
    assert item['status'] == 'unverified' and not item['individual_cash_settlement_verified']


@pytest.mark.parametrize('has_line', [False, True])
def test_explicit_zero_is_fee_evidence_not_a_cash_settlement_claim(has_line):
    data = case(fee='0')
    if not has_line:
        data['lines'] = []
    item = record(data)
    assert item['classification'] == 'explicit_zero' and item['status'] == 'sufficient'
    assert item['evidence_scope'] == 'official_explicit_zero_commission'
    assert not item['individual_cash_settlement_verified']


@pytest.mark.parametrize('fee', ['0', '-1.25'])
def test_empty_inactive_cash_leg_is_not_silently_coerced_to_zero(fee):
    data = case(fee=fee)
    data['lines'][0]['credit'] = ''
    item = record(data)
    assert not item['individual_cash_settlement_verified'] and item['report_line_arithmetic'] is None
    assert item['report_component_arithmetic'] is True
    if fee == '0':
        assert item['classification'] == 'explicit_zero'
        assert item['evidence_scope'] == 'official_explicit_zero_commission'
        assert item['cash_evidence_reason'] == 'missing_or_invalid_funds_number'
    else:
        assert item['status'] == 'unverified' and item['reasons'] == ['missing_or_invalid_funds_number']


def test_missing_cash_leg_does_not_mask_a_conflicting_explicit_fee():
    data = case(fee='0')
    data['lines'][0].update(credit='', tradeCommission='-3')
    assert record(data)['reasons'] == ['signed_commission_mismatch']


def test_missing_cash_leg_does_not_mask_a_conflicting_component_equation():
    data = case()
    data['lines'][0].update(credit='', amount='-999')
    item = record(data)
    assert item['status'] == 'conflict' and item['report_component_arithmetic'] is False


@pytest.mark.parametrize('fee', ['-2.75', '0'])
@pytest.mark.parametrize('principal', ['-100', '100'])
def test_fx_zero_netcash_and_misleading_crosscurrency_principal_are_not_missing_fees(fee, principal):
    data = case(fee=fee)
    trade, line = data['trades'][0], data['lines'][0]
    trade.update(assetCategory='CASH', currency='MXN', netCash='0')
    amount = str(Decimal(principal) + Decimal(fee))
    line.update(assetCategory='CASH', activityCode='FOREX', levelOfDetail='BaseCurrency',
                tradeGross=principal, amount=amount, debit=amount if Decimal(amount) < 0 else '0',
                credit=amount if Decimal(amount) > 0 else '0')
    item = record(data)
    assert item['report_line_arithmetic'] is True
    assert item['classification'] == ('explicit_zero' if fee == '0' else None)
    assert item['reasons'] == (['official_and_funds_explicit_zero'] if fee == '0' else
                               ['fx_cash_legs_not_individual_settlement'])
    assert not item['individual_cash_settlement_verified']


def test_crosscurrency_fee_only_line_has_direct_proof_without_principal_conversion():
    data = separate(case())
    data['trades'][0].update(currency='MXN', assetCategory='CASH', netCash='0')
    data['lines'][0]['assetCategory'] = 'CASH'
    assert record(data)['classification'] == 'separate'


def test_translated_security_cash_line_is_not_native_settlement():
    data = case()
    data['trades'][0]['currency'] = 'SEK'
    data['lines'][0].update(levelOfDetail='BaseCurrency', fxRateToBase='0.1')
    assert record(data)['reasons'] == ['translated_principal_not_native_settlement']


@pytest.mark.parametrize('field,value', [('amount', '-99'), ('debit', '-99'), ('tradeGross', '-99'),
                                      ('tradeTax', '5'), ('credit', '5')])
def test_cash_arithmetic_conflicts_are_not_accepted_or_repaired(field, value):
    data = case()
    data['lines'][0][field] = value
    before = deepcopy(data)
    assert record(data)['reasons'] == ['cash_line_arithmetic_or_sign_conflict']
    assert data == before


@pytest.mark.parametrize('field,value', [('proceeds', '-99'), ('taxes', '-1'), ('netCash', '-99')])
def test_native_trade_cash_must_match_statement_of_funds(field, value):
    data = case()
    data['trades'][0][field] = value
    assert record(data)['reasons'] == ['trade_and_funds_cash_mismatch']


@pytest.mark.parametrize('field,value', [('status', 'stale'), ('legacy_unverified', True),
                                      ('statement_as_of', None), ('expected_statement_as_of', '2031-04-17')])
def test_unverified_selected_report_cannot_supply_settlement_proof(field, value):
    data = case()
    data['quality'][field] = value
    result = review(data)
    assert result['status'] == 'unverified' and result['records'] == [] and result['total'] is None


@pytest.mark.parametrize('currency', ['US$', 'U\u017fD', '\u00dfA', 'BASE_SUMMARY', ''])
def test_currency_is_explicit_ascii_not_a_unicode_or_summary_alias(currency):
    data = case()
    data['trades'][0]['ibCommissionCurrency'] = currency
    assert record(data)['reasons'] == ['missing_or_invalid_explicit_currency']


def test_xml_namespace_and_iso_dates_are_supported_without_substring_date_parsing():
    data = case()
    for row, fields in [(data['trades'][0], ['tradeDate', 'settleDateTarget']),
                        (data['lines'][0], ['date', 'settleDate'])]:
        for field in fields:
            value = row[field]
            row[field] = value[:4] + '-' + value[4:6] + '-' + value[6:]
    text = xml_text(data).replace('<FlexStatement ', '<FlexStatement xmlns="urn:synthetic-flex" ')
    assert review_fee_settlement(xml_text=text, quality=QUALITY)['records'][0]['status'] == 'sufficient'
    data['lines'][0]['date'] += ';12:34:56'
    assert record(data)['reasons'] == ['transaction_date_not_proven']


def test_duplicate_case_insensitive_attributes_are_rejected_instead_of_overwritten():
    text = xml_text(case()).replace('ibCommission="-1.25"', 'ibCommission="-1.25" ibcommission="0"')
    result = review_fee_settlement(xml_text=text, quality=QUALITY)
    assert result['reason'] == 'malformed_or_ambiguous_xml' and result['total'] is None


@pytest.mark.parametrize('text', ['<broken', '<FlexStatement/>', '<FlexStatements/>'])
def test_malformed_or_unscoped_xml_fails_closed(text):
    result = review_fee_settlement(xml_text=text, quality=QUALITY)
    assert result['status'] == 'unverified' and result['records'] == []


def test_multiple_statements_are_not_merged_or_first_selected():
    text = '<FlexStatements>' + xml_text(case()) * 2 + '</FlexStatements>'
    assert review_fee_settlement(xml_text=text, quality=QUALITY)['reason'] == 'missing_or_overlapping_statement_periods'


def test_disjoint_daily_statements_share_exact_identity_scope_not_date_heuristics():
    first = ET.fromstring(xml_text(case()))
    first.set('fromDate', '20310418')
    second = ET.fromstring(xml_text(case(execution='another-independent-fill')))
    second.set('fromDate', '20310419')
    second.set('toDate', '20310419')
    root = ET.Element('FlexStatements')
    root.extend([first, second])
    quality = {**QUALITY, 'statement_as_of': '2031-04-19', 'expected_statement_as_of': '2031-04-19'}
    result = review_fee_settlement(xml_text=ET.tostring(root, encoding='unicode'), quality=quality)
    assert result['statement_count'] == 2 and result['total'] == 2
    assert all(item['classification'] == 'included' for item in result['records'])
    assert result['records'][1]['funds_line_indices'] == [1]
    second.set('fromDate', '20310418')
    assert review_fee_settlement(xml_text=ET.tostring(root, encoding='unicode'), quality=quality)['reason'] == (
        'missing_or_overlapping_statement_periods')


def test_multiple_accounts_are_not_silently_combined():
    first = xml_text(case()).replace('toDate=', 'fromDate="20310418" toDate=', 1)
    second = xml_text(case(account='SYNTH_B')).replace('toDate=', 'fromDate="20310419" toDate=', 1)
    result = review_fee_settlement(xml_text='<FlexStatements>' + first + second + '</FlexStatements>', quality=QUALITY)
    assert result['reason'] == 'unverified_report_or_account'


def test_matching_but_future_settlement_dates_are_not_completed_settlement():
    data = case()
    data['trades'][0]['settleDateTarget'] = data['lines'][0]['settleDate'] = '20310419'
    assert record(data)['reasons'] == ['settlement_after_report_as_of']


def test_matching_symbol_but_conflicting_explicit_contract_is_rejected():
    data = case()
    data['trades'][0]['conid'] = 'synthetic-contract-a'
    data['lines'][0]['conid'] = 'synthetic-contract-b'
    assert record(data)['reasons'] == ['instrument_scope_mismatch']


def test_same_arithmetic_with_wrong_buy_principal_sign_is_not_proof():
    data = case()
    data['trades'][0].update(proceeds='100', netCash='98.75')
    data['lines'][0].update(tradeGross='100', amount='98.75', debit='0', credit='98.75')
    assert record(data)['reasons'] == ['principal_direction_conflict']


def test_sell_semantics_and_transaction_id_namespace_match_existing_fee_review():
    data = case()
    data['trades'][0].update(buySell='SELL', proceeds='100', netCash='98.75')
    data['lines'][0].update(buySell='SELL', activityCode='SELL', tradeGross='100',
                            amount='98.75', debit='0', credit='98.75')
    assert record(data)['classification'] == 'included'
    assert record(data)['execution_id'] == 'trade_id:arbitrary-17'
    data['trades'][0].pop('tradeID')
    data['lines'][0].pop('tradeID')
    assert record(data)['execution_id'] == 'transaction_id:tx-arbitrary-17'


def test_arithmetic_is_exact_under_small_caller_decimal_context_and_deterministic():
    data = case(fee='-0.00000000000000000000001')
    data['trades'][0]['netCash'] = '-100.00000000000000000000001'
    data['lines'][0].update(amount=data['trades'][0]['netCash'], debit=data['trades'][0]['netCash'])
    with localcontext() as context:
        context.prec = 6
        assert record(data)['status'] == 'sufficient'
        assert review(data) == review(data)


def test_small_caller_exponent_range_does_not_overflow_valid_evidence():
    data = case()
    with localcontext() as context:
        context.Emax = 1
        context.Emin = -1
        assert record(data)['classification'] == 'included'


def test_unsupported_decimal_range_does_not_overflow_or_approve_evidence():
    data = case()
    data['lines'][0]['tradeGross'] = '1e99999'
    assert record(data)['reasons'] == ['unsupported_decimal_range']
