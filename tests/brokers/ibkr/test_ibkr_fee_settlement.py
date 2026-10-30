"""Synthetic, parameterized official evidence; no database or real account inputs."""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend/api'))
from domain.brokers.ibkr.ibkr_fee_settlement import review_fee_settlement


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
    statement = ET.Element('FlexStatement', accountId=data['trades'][0]['accountId'],
                           fromDate='20310416', toDate='20310418')
    trades = ET.SubElement(statement, 'Trades')
    for trade in data['trades']:
        ET.SubElement(trades, 'Trade', trade)
    funds = ET.SubElement(statement, 'StatementOfFunds')
    for line in data['lines']:
        ET.SubElement(funds, 'StatementOfFundsLine', line)
    return ET.tostring(statement, encoding='unicode')


def review(data):
    return review_fee_settlement(xml_text=xml_text(data), quality=data['quality'])


def review_xml(root, quality=None):
    return review_fee_settlement(xml_text=ET.tostring(root, encoding='unicode'),
                                 quality=QUALITY if quality is None else quality)


def record(data):
    return review(data)['records'][0]


def assert_quarantined_row(result, reason, tag):
    assert result['status'] == 'review' and result['total'] == 1 and len(result['records']) == 1
    item = result['records'][0]
    assert item['status'] == 'unverified' and item['reasons'] == [reason] and item['classification'] is None
    assert not item['individual_cash_settlement_verified'] and not item['additional_deduction_proposed']
    key = 'trade_scope_diagnostics' if tag == 'Trade' else 'funds_scope_diagnostics'
    summary = result[key]
    assert summary['evaluated'] and summary['issue_count'] == 1
    assert summary['reason_counts'] == {reason: 1} and summary['issues'][0]['section'] == tag
    evidence = item['trade_evidence_period'] if tag == 'Trade' else item['funds_evidence_periods'][0]
    assert evidence['scope_verified'] is False and evidence['reason'] == reason


def separate(data):
    line = data['lines'][0]
    line.update(activityCode='ADJ', tradeGross='0', tradeTax='0', amount=line['tradeCommission'],
                debit=line['tradeCommission'], credit='0')
    return data


def test_one_cash_line_cannot_be_owned_by_two_disjoint_execution_keys():
    data = case()
    original = data['trades'][0]
    other = deepcopy(original)
    original.pop('transactionID')
    other.pop('tradeID')
    data['trades'].append(other)
    result = review(data)
    assert all(item['status'] == 'conflict' for item in result['records'])
    assert all(item['reasons'] == ['funds_identity_shared_by_multiple_executions'] for item in result['records'])
    assert not any(item['individual_cash_settlement_verified'] for item in result['records'])


@pytest.mark.parametrize('fee', ['0', '-1.25'])
@pytest.mark.parametrize('invalid_date', [False, True])
def test_missing_leaf_account_does_not_hide_a_duplicate_candidate(fee, invalid_date):
    data = case(fee=fee)
    duplicate = deepcopy(data['lines'][0])
    duplicate.pop('accountId')
    if invalid_date:
        duplicate['date'] = '20310415'
    data['lines'].append(duplicate)
    item = record(data)
    assert item['candidate_line_count'] == 2 and item['status'] == 'conflict'
    assert not item['individual_cash_settlement_verified']


def test_single_missing_leaf_account_is_not_native_cash_proof():
    data = case()
    data['lines'][0].pop('accountId')
    assert record(data)['reasons'] == ['missing_explicit_funds_account']


@pytest.mark.parametrize('rate', [None, '1', '0.125'])
def test_base_currency_representation_never_certifies_native_settlement(rate):
    data = case()
    data['lines'][0]['levelOfDetail'] = 'BaseCurrency'
    if rate is not None:
        data['lines'][0]['fxRateToBase'] = rate
    item = record(data)
    assert item['status'] == 'unverified' and not item['individual_cash_settlement_verified']
    assert item['reasons'] == ['translated_principal_not_native_settlement']


def test_fx_leg_with_displayed_zero_principal_is_not_a_separate_commission():
    data = separate(case())
    data['trades'][0].update(assetCategory='CASH', currency='MXN', netCash='0')
    data['lines'][0].update(assetCategory='CASH', activityCode='FOREX')
    item = record(data)
    assert item['status'] == 'unverified' and item['reasons'] == ['fx_cash_legs_not_individual_settlement']


def test_later_empty_statement_does_not_confirm_earlier_projected_settlement():
    first = ET.fromstring(xml_text(case()))
    first.set('toDate', '20310416')
    root = ET.Element('FlexStatements')
    root.append(first)
    later = ET.SubElement(root, 'FlexStatement', accountId='SYNTH_A', fromDate='20310418', toDate='20310418')
    ET.SubElement(later, 'Trades')
    item = review_xml(root)['records'][0]
    assert item['status'] == 'unverified'
    assert item['reasons'] == ['settlement_after_funds_evidence_as_of']


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
    if field == 'tradeDate':
        assert_quarantined_row(review(data), 'missing_or_invalid_row_date', 'Trade')
    else:
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
    if field == 'date' and not value:
        assert_quarantined_row(review(data), 'missing_or_invalid_row_date', 'StatementOfFundsLine')
        return
    item = record(data)
    assert item['status'] in {'unverified', 'conflict'} and item['reasons'] == [reason]
    assert not item['individual_cash_settlement_verified']


@pytest.mark.parametrize('field', ['accountId', 'tradeID', 'transactionID'])
def test_wrong_identity_never_links_by_symbol_amount_or_nearest_date(field):
    data = case()
    data['lines'][0][field] = 'wrong'
    if field != 'accountId':
        data['lines'][0].pop('transactionID' if field == 'tradeID' else 'tradeID')
    else:
        assert review(data)['reason'] == 'contradictory_account_or_model_scope'
        assert review(data)['total'] is None
        return
    item = record(data)
    assert item['status'] == 'unverified' and item['candidate_line_count'] == 0


def test_contradictory_child_account_is_blocked_instead_of_silently_ignored():
    data = case()
    data['lines'].append({**data['lines'][0], 'accountId': 'OTHER-SYNTHETIC-ACCOUNT'})
    assert review(data)['reason'] == 'contradictory_account_or_model_scope'
    assert review(data)['records'] == [] and review(data)['total'] is None


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
    assert_quarantined_row(review(data), 'missing_or_invalid_row_date', 'StatementOfFundsLine')


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
    second = ET.fromstring(xml_text(case(execution='another-independent-fill')))
    second.set('fromDate', '20310419')
    second.set('toDate', '20310419')
    second.find('./Trades/Trade').set('tradeDate', '20310419')
    second.find('./Trades/Trade').set('settleDateTarget', '20310419')
    second.find('./StatementOfFunds/StatementOfFundsLine').set('date', '20310419')
    second.find('./StatementOfFunds/StatementOfFundsLine').set('settleDate', '20310419')
    root = ET.Element('FlexStatements')
    root.extend([first, second])
    quality = {**QUALITY, 'statement_as_of': '2031-04-19', 'expected_statement_as_of': '2031-04-19'}
    result = review_fee_settlement(xml_text=ET.tostring(root, encoding='unicode'), quality=quality)
    assert result['statement_count'] == 2 and result['total'] == 2
    assert all(item['classification'] == 'included' for item in result['records'])
    assert result['records'][1]['funds_line_indices'] == [1]
    assert result['records'][0]['trade_evidence_period'] == {'from_date': '2031-04-16', 'to_date': '2031-04-18'}
    assert result['records'][1]['funds_evidence_periods'] == [
        {'line_index': 1, 'from_date': '2031-04-19', 'to_date': '2031-04-19'}]
    second.set('fromDate', '20310418')
    assert review_fee_settlement(xml_text=ET.tostring(root, encoding='unicode'), quality=quality)['reason'] == (
        'missing_or_overlapping_statement_periods')


def test_multiple_accounts_are_not_silently_combined():
    first = xml_text(case())
    second = xml_text(case(account='SYNTH_B'))
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


@pytest.mark.parametrize('classification', ['included', 'separate', 'explicit_zero'])
@pytest.mark.parametrize('account,execution', [('RANDOM_ACCOUNT_A', 'fill-x'), ('OTHER_SYNTHETIC', 'fill-y')])
def test_using_previous_publication_blocks_every_commission_classification(classification, account, execution):
    data = case(account=account, execution=execution, fee='0' if classification == 'explicit_zero' else '-1.25')
    if classification == 'separate':
        separate(data)
    data['quality']['using_previous'] = True
    result = review(data)
    assert result['reason'] == 'unverified_report_or_account'
    assert result['status'] == 'unverified' and result['total'] is None and result['records'] == []
    data['quality']['using_previous'] = False
    assert record(data)['classification'] == classification


@pytest.mark.parametrize('tag', ['FlexStatement', 'Trades', 'Trade', 'StatementOfFunds',
                               'StatementOfFundsLine', 'AccountInformation', 'ArbitraryAncestor', 'OuterWrapper'])
@pytest.mark.parametrize('attribute,value', [('model', 'SYNTHETIC_MODEL'), ('accountId', 'WRONG_ACCOUNT'),
                                            ('acctId', 'WRONG_ACCOUNT')])
@pytest.mark.parametrize('fee', ['0', '-1.25'])
def test_ancestor_scope_contradictions_block_before_even_explicit_zero(tag, attribute, value, fee):
    root = ET.fromstring(xml_text(case(account='ARBITRARY_ACCOUNT', execution='ARBITRARY_FILL', fee=fee)))
    if tag == 'AccountInformation':
        node = ET.SubElement(root, tag, accountId='ARBITRARY_ACCOUNT', currency='CAD')
    elif tag == 'ArbitraryAncestor':
        trades = root.find('Trades')
        trade = trades.find('Trade')
        trades.remove(trade)
        node = ET.SubElement(trades, tag)
        node.append(trade)
    elif tag == 'OuterWrapper':
        node = ET.Element(tag)
        node.append(root)
        root = node
    else:
        node = next(root.iter(tag))
    node.set(attribute, value)
    result = review_xml(root)
    assert result['reason'] == 'contradictory_account_or_model_scope'
    assert result['status'] == 'unverified' and result['records'] == [] and result['total'] is None


@pytest.mark.parametrize('tag', ['Trades', 'StatementOfFunds', 'StmtFunds'])
@pytest.mark.parametrize('attribute,value', [('model', 'MODEL'), ('accountId', 'OTHER')])
def test_empty_containers_still_carry_binding_scope_before_zero(tag, attribute, value):
    data = case(fee='0')
    data['lines'] = []
    root = ET.fromstring(xml_text(data))
    if tag == 'Trades':
        root.find('Trades').clear()
    elif tag == 'StmtFunds':
        root.find('StatementOfFunds').tag = tag
    next(root.iter(tag)).set(attribute, value)
    assert review_xml(root)['reason'] == 'contradictory_account_or_model_scope'
    assert review_xml(root)['total'] is None


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('field,value', [('fromDate', None), ('fromDate', ''), ('fromDate', 'invalid'),
                                      ('fromDate', '20310419'), ('toDate', None), ('toDate', ''),
                                      ('toDate', '20310230'), ('toDate', '20310415')])
def test_every_statement_requires_valid_explicit_ordered_period_even_single(count, field, value):
    first = ET.fromstring(xml_text(case(fee='0')))
    if value is None:
        first.attrib.pop(field)
    else:
        first.set(field, value)
    root, quality = first, deepcopy(QUALITY)
    if count == 2:
        second = ET.Element('FlexStatement', accountId='SYNTH_A', fromDate='20310420', toDate='20310420')
        ET.SubElement(second, 'Trades')
        root = ET.Element('FlexStatements')
        root.extend([first, second])
        quality.update(statement_as_of='2031-04-20', expected_statement_as_of='2031-04-20')
    result = review_xml(root, quality)
    assert result['status'] == 'unverified' and result['records'] == [] and result['total'] is None
    assert result['reason'] in {'missing_or_overlapping_statement_periods', 'unverified_report_or_account'}


@pytest.mark.parametrize('tag,field', [('Trade', 'tradeDate'), ('StatementOfFundsLine', 'date')])
@pytest.mark.parametrize('day', ['20310415', '20310419'])
@pytest.mark.parametrize('fee', ['0', '-1.25'])
def test_row_date_must_be_inside_its_own_statement_not_just_before_report_as_of(tag, field, day, fee):
    root = ET.fromstring(xml_text(case(fee=fee)))
    next(root.iter(tag)).set(field, day)
    result = review_xml(root)
    assert_quarantined_row(result, 'row_date_outside_ancestor_period', tag)


@pytest.mark.parametrize('tag', ['Trade', 'StatementOfFundsLine'])
@pytest.mark.parametrize('day', ['20310416', '20310419'])
@pytest.mark.parametrize('fee', ['0', '-1.25'])
def test_reparented_rows_cannot_borrow_sibling_coverage_or_fill_period_gaps(tag, day, fee):
    first = ET.fromstring(xml_text(case(fee=fee)))
    second = ET.Element('FlexStatement', accountId='SYNTH_A', fromDate='20310420', toDate='20310420')
    second_trades = ET.SubElement(second, 'Trades')
    second_funds = ET.SubElement(second, 'StatementOfFunds')
    parent = first.find('Trades' if tag == 'Trade' else 'StatementOfFunds')
    row = parent.find(tag)
    parent.remove(row)
    row.set('tradeDate' if tag == 'Trade' else 'date', day)
    (second_trades if tag == 'Trade' else second_funds).append(row)
    root = ET.Element('FlexStatements')
    root.extend([first, second])
    quality = {**QUALITY, 'statement_as_of': '2031-04-20', 'expected_statement_as_of': '2031-04-20'}
    result = review_xml(root, quality)
    assert_quarantined_row(result, 'row_date_outside_ancestor_period', tag)


@pytest.mark.parametrize('container', ['Trades', 'StatementOfFunds'])
@pytest.mark.parametrize('start,end,reason', [('20310417', '20310418', 'row_date_outside_ancestor_period'),
                                           ('20310419', '20310420', 'disjoint_ancestor_periods'),
                                           ('20310416', None, 'missing_or_invalid_ancestor_period')])
def test_generic_ancestor_period_can_narrow_but_never_expand_statement_coverage(container, start, end, reason):
    root = ET.fromstring(xml_text(case(fee='0')))
    node = root.find(container)
    node.set('fromDate', start)
    if end is not None:
        node.set('toDate', end)
    result = review_xml(root)
    assert_quarantined_row(result, reason, 'Trade' if container == 'Trades' else 'StatementOfFundsLine')


@pytest.mark.parametrize('namespace', ['', 'urn:synthetic:flex'])
def test_valid_ancestor_scope_and_narrowed_periods_are_preserved_in_evidence(namespace):
    root = ET.fromstring(xml_text(case(account='SCOPE_ACCOUNT', execution='SCOPE_EXECUTION')))
    root.find('Trades/Trade').set('settleDateTarget', '20310416')
    root.find('StatementOfFunds/StatementOfFundsLine').set('settleDate', '20310416')
    ET.SubElement(root, 'AccountInformation', accountId='SCOPE_ACCOUNT', currency='CAD')
    for node in (root.find('Trades'), root.find('StatementOfFunds')):
        node.set('accountId', 'SCOPE_ACCOUNT')
        node.set('model', '')
        node.set('fromDate', '20310416')
        node.set('toDate', '20310416')
    if namespace:
        for node in root.iter():
            node.tag = '{' + namespace + '}' + node.tag
    result = review_xml(root)
    item = result['records'][0]
    assert item['classification'] == 'included'
    assert item['trade_evidence_period'] == {'from_date': '2031-04-16', 'to_date': '2031-04-16'}
    assert item['funds_evidence_periods'] == [
        {'line_index': 0, 'from_date': '2031-04-16', 'to_date': '2031-04-16'}]


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('present', [False, True])
def test_missing_trades_section_is_unknown_not_an_explicit_empty_result(count, present):
    root = ET.Element('FlexStatements')
    for index in range(count):
        day = '20310417' if index == 0 and count == 2 else '20310418'
        statement = ET.SubElement(root, 'FlexStatement', accountId='EMPTY_ACCOUNT', fromDate=day, toDate=day)
        if present or index != count - 1:
            ET.SubElement(statement, 'Trades')
    result = review_xml(root)
    assert result['records'] == []
    assert result['total'] == (0 if present else None)
    assert result['reason'] == ('explicit_empty_trade_section' if present else 'missing_trade_section')
    assert result['status'] == ('review' if present else 'unverified')


def test_one_missing_trades_section_blocks_otherwise_sufficient_zero_in_another_statement():
    root = ET.Element('FlexStatements')
    root.append(ET.fromstring(xml_text(case(fee='0'))))
    ET.SubElement(root, 'FlexStatement', accountId='SYNTH_A', fromDate='20310420', toDate='20310420')
    quality = {**QUALITY, 'statement_as_of': '2031-04-20', 'expected_statement_as_of': '2031-04-20'}
    result = review_xml(root, quality)
    assert result['reason'] == 'missing_trade_section' and result['total'] is None and result['records'] == []


def test_trade_outside_explicit_trades_section_is_not_borrowed_from_empty_sibling():
    root = ET.fromstring(xml_text(case(fee='0')))
    trades = root.find('Trades')
    row = trades.find('Trade')
    trades.remove(row)
    root.append(row)
    result = review_xml(root)
    assert_quarantined_row(result, 'missing_trade_section_ancestor', 'Trade')


@pytest.mark.parametrize('tag', ['UnknownExecution', 'TradeSummary'])
def test_nonempty_unsupported_trades_content_does_not_masquerade_as_explicit_empty(tag):
    root = ET.Element('FlexStatement', accountId='EMPTY_ACCOUNT', fromDate='20310418', toDate='20310418')
    ET.SubElement(ET.SubElement(root, 'Trades'), tag)
    result = review_xml(root)
    assert result['reason'] == 'unsupported_nonempty_trade_section' and result['total'] is None


@pytest.mark.parametrize('classification', ['included', 'separate', 'explicit_zero'])
@pytest.mark.parametrize('activity', ['DEP', 'FOREX', 'UNSUPPORTED_ACTIVITY'])
@pytest.mark.parametrize('day', ['20310415', '20310419', 'invalid'])
def test_unrelated_bad_funds_row_is_visible_without_blocking_valid_evidence(classification, activity, day):
    data = case(fee='0' if classification == 'explicit_zero' else '-1.25')
    if classification == 'separate':
        separate(data)
    data['lines'].append({'accountId': 'SYNTH_A', 'transactionID': 'unrelated-posting',
                          'activityCode': activity, 'date': day})
    result = review(data)
    assert result['total'] == 1 and result['records'][0]['classification'] == classification
    summary = result['funds_scope_diagnostics']
    assert summary['evaluated'] and summary['line_count'] == 2 and summary['unsupported_line_count'] == 1
    assert summary['issue_count'] == 1 and summary['issues'][0]['line_index'] == 1
    assert summary['issues'][0]['reason'] == ('missing_or_invalid_row_date' if day == 'invalid' else
                                             'row_date_outside_ancestor_period')
    assert result['records'][0]['funds_line_indices'] == [0]


@pytest.mark.parametrize('tag', ['Trade', 'StatementOfFundsLine'])
@pytest.mark.parametrize('day', ['', '20310415'])
@pytest.mark.parametrize('bad_fee', ['0', '-1.25'])
@pytest.mark.parametrize('good_fee', ['0', '-1.25'])
def test_bad_trade_or_linked_funds_row_is_quarantined_and_other_execution_survives(tag, day, bad_fee, good_fee):
    data = case(execution='bad-execution', fee=bad_fee)
    other = case(execution='independent-good-execution', fee=good_fee)
    data['trades'].extend(other['trades'])
    data['lines'].extend(other['lines'])
    row = data['trades'][0] if tag == 'Trade' else data['lines'][0]
    row['tradeDate' if tag == 'Trade' else 'date'] = day
    result = review(data)
    bad, good = result['records']
    reason = 'missing_or_invalid_row_date' if not day else 'row_date_outside_ancestor_period'
    assert result['total'] == 2 and result['status'] == 'review'
    assert bad['status'] == 'unverified' and bad['classification'] is None and bad['reasons'] == [reason]
    assert not bad['individual_cash_settlement_verified']
    assert good['status'] == 'sufficient' and good['classification'] == ('explicit_zero' if good_fee == '0' else 'included')
    if tag == 'StatementOfFundsLine':
        assert bad['funds_line_indices'] == [0] and bad['funds_scope_issue_count'] == 1
        assert bad['funds_scope_issues'] == [{'line_index': 0, 'reason': reason}]
        assert bad['funds_evidence_periods'][0]['scope_verified'] is False
    else:
        assert bad['trade_evidence_period']['scope_verified'] is False


def test_bad_funds_duplicate_is_not_dropped_to_select_the_good_line():
    data = case(fee='0')
    data['lines'].append({**data['lines'][0], 'date': '20310415'})
    item = record(data)
    assert item['status'] == 'conflict' and item['classification'] is None
    assert item['candidate_line_count'] == 2 and item['funds_line_indices'] == [0, 1]
    assert item['funds_scope_issue_count'] == 1 and item['funds_scope_issues'][0]['line_index'] == 1


def test_bad_trade_duplicate_identity_remains_visible_to_the_other_execution():
    data = case(fee='0')
    data['trades'].append({**data['trades'][0], 'tradeDate': '20310415'})
    result = review(data)
    assert result['total'] == 2
    assert result['records'][0]['reasons'] == ['duplicate_trade_identity']
    assert result['records'][1]['reasons'] == ['row_date_outside_ancestor_period']
    assert all(item['classification'] is None for item in result['records'])


def test_funds_summary_is_bounded_but_tail_linked_bad_line_is_still_checked():
    data = case(fee='0')
    linked = data['lines'][0]
    data['lines'] = [{'accountId': 'SYNTH_A', 'transactionID': 'unrelated-' + str(index), 'date': '20310415'}
                     for index in range(35)]
    data['lines'].append({**linked, 'date': '20310415'})
    result = review(data)
    summary = result['funds_scope_diagnostics']
    assert summary['issue_count'] == summary['unsupported_line_count'] == 36
    assert len(summary['issues']) == 30 and summary['truncated']
    assert summary['reason_counts'] == {'row_date_outside_ancestor_period': 36}
    item = result['records'][0]
    assert item['status'] == 'unverified' and item['funds_line_indices'] == [35]
    assert item['funds_scope_issues'][0]['line_index'] == 35


def test_trade_summary_is_bounded_without_dropping_bad_rows_or_valid_tail_execution():
    data = case(fee='0')
    good = data['trades'][0]
    data['trades'] = [{**good, 'tradeID': 'bad-' + str(index), 'transactionID': 'bad-tx-' + str(index),
                       'tradeDate': '20310415'} for index in range(35)] + [good]
    result = review(data)
    summary = result['trade_scope_diagnostics']
    assert result['total'] == summary['trade_count'] == 36
    assert summary['issue_count'] == summary['unsupported_trade_count'] == 35
    assert len(summary['issues']) == 30 and summary['truncated']
    assert all(item['status'] == 'unverified' for item in result['records'][:-1])
    assert result['records'][-1]['classification'] == 'explicit_zero'


@pytest.mark.parametrize('tag,container', [('Trade', 'Trades'), ('StatementOfFundsLine', 'StatementOfFunds')])
@pytest.mark.parametrize('attribute,value', [('accountId', 'WRONG_ACCOUNT'), ('model', 'MODEL')])
def test_bad_ancestor_period_does_not_mask_global_child_account_or_model_contradictions(tag, container, attribute, value):
    root = ET.fromstring(xml_text(case(fee='0')))
    root.find(container).set('fromDate', '20310419')
    root.find(container).set('toDate', '20310420')
    next(root.iter(tag)).set(attribute, value)
    result = review_xml(root)
    assert result['reason'] == 'contradictory_account_or_model_scope'
    assert result['total'] is None and result['records'] == []


@pytest.mark.parametrize('change', ['included', 'separate', 'zero', 'arithmetic', 'ancestor',
                                  'row_period', 'duplicate', 'ambiguous_attribute', 'missing_fee', 'stale'])
def test_shared_tree_keeps_independent_scope_arithmetic_hash_and_full_result(monkeypatch, change):
    from domain.brokers.ibkr import ibkr_fee_settlement as service

    data = case(fee='0' if change == 'zero' else '-1.25')
    if change == 'separate':
        separate(data)
    elif change == 'arithmetic':
        data['lines'][0]['amount'] = '-99'
    elif change == 'row_period':
        data['lines'][0]['date'] = '20310415'
    elif change == 'duplicate':
        data['lines'].append(deepcopy(data['lines'][0]))
    elif change == 'ambiguous_attribute':
        data['lines'][0]['tradecommission'] = '-99'
    elif change == 'missing_fee':
        data['trades'][0].pop('ibCommission')
    elif change == 'stale':
        data['quality']['status'] = 'stale'
    root = ET.fromstring(xml_text(data))
    if change == 'ancestor':
        root.set('model', 'UNSUPPORTED_MODEL')
    text = ET.tostring(root, encoding='unicode')
    expected = review_fee_settlement(xml_text=text, quality=data['quality'])
    before = ET.tostring(root)
    counts = {'scope': 0, 'arithmetic': 0}
    scope, arithmetic = service._ancestor_scope, service._sum_matches

    def observe_scope(*args):
        counts['scope'] += 1
        return scope(*args)

    def observe_arithmetic(*args):
        counts['arithmetic'] += 1
        return arithmetic(*args)

    def unexpected_parse(*args, **kwargs):
        pytest.fail('Settlement must reuse the supplied tree without reparsing')

    monkeypatch.setattr(ET, 'fromstring', unexpected_parse)
    monkeypatch.setattr(service, '_ancestor_scope', observe_scope)
    monkeypatch.setattr(service, '_sum_matches', observe_arithmetic)
    result = service._review_fee_settlement(xml_text=text, quality=data['quality'], root=root)
    assert result == expected and ET.tostring(root) == before
    assert result['report_sha256'] == hashlib.sha256(text.encode('utf-8')).hexdigest()
    if change in {'included', 'separate', 'zero', 'arithmetic'}:
        assert counts['scope'] > 0 and counts['arithmetic'] > 0
        status = 'conflict' if change == 'arithmetic' else 'sufficient'
        assert result['records'][0]['status'] == status
    else:
        assert not any(item['status'] == 'sufficient' for item in result['records'])
