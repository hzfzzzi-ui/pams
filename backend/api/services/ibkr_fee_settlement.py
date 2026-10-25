"""Pure official fee evidence, not a fee calculator or a fact/valuation writer."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from itertools import pairwise
from typing import Any

from services.ibkr_fee_currency import currency_code


def _attributes(element: ET.Element) -> dict[str, str]:
    result = {key.lower(): value.strip() for key, value in element.attrib.items()}
    if len(result) != len(element.attrib):
        raise ValueError('ambiguous attribute names')
    return result


def _date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(value, fmt).date()
            if parsed.strftime(fmt) == value:
                return parsed
        except ValueError:
            pass
    return None


def _number(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        number = Decimal(value)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _sum_matches(expected: Decimal, *terms: Decimal) -> bool | None:
    # Keep exact XML arithmetic independent of the caller's Decimal context.
    values = (expected, *terms)
    precision = max(value.adjusted() for value in values) - min(int(value.as_tuple().exponent) for value in values) + 4
    if precision > 4096 or any(abs(value.adjusted()) > 4096 for value in values):
        return None
    with localcontext() as context:
        context.prec = max(28, precision)
        context.Emax = max(context.Emax, 8192)
        context.Emin = min(context.Emin, -8192)
        return expected == sum(terms, Decimal(0))


def _identity(row: Mapping[str, str]) -> str | None:
    return next((label + ':' + row[field] for field, label in
                 (('tradeid', 'trade_id'), ('transactionid', 'transaction_id')) if row.get(field)), None)


def _related(trade: Mapping[str, str], line: Mapping[str, str]) -> bool:
    return any(trade.get(field) and trade[field] == line.get(field) for field in ('tradeid', 'transactionid'))


def _finish(item: dict[str, Any], status: str, reason: str, classification: str | None = None) -> dict[str, Any]:
    item.update(status=status, reasons=[reason], classification=classification)
    if status == 'sufficient':
        item['evidence_scope'] = ('official_explicit_zero_commission' if classification == 'explicit_zero'
                                  else 'individual_statement_of_funds_line')
        item['individual_cash_settlement_verified'] = classification != 'explicit_zero'
    return item


def _review_trade(trade: dict[str, str], trades: list[dict[str, str]],
                  lines: list[dict[str, str]], account: str, as_of: date) -> dict[str, Any]:
    day, settle = _date(trade.get('tradedate')), _date(trade.get('settledatetarget'))
    fee = _number(trade.get('ibcommission'))
    item: dict[str, Any] = {
        'source_account_id': trade.get('accountid'), 'execution_id': _identity(trade),
        'trade_date': day.isoformat() if day else None,
        'trade_currency': currency_code(trade.get('currency')),
        'fee_currency': currency_code(trade.get('ibcommissioncurrency')),
        'official_commission_signed': trade.get('ibcommission') if fee is not None else None,
        'evidence_scope': 'unverified', 'individual_cash_settlement_verified': False,
        'report_line_arithmetic': None, 'report_component_arithmetic': None,
        'funds_line_indices': [], 'candidate_line_count': 0,
        'additional_deduction_proposed': False, 'executable': False,
    }
    if trade.get('accountid') != account or not item['execution_id']:
        return _finish(item, 'unverified', 'missing_or_conflicting_execution_account')
    if any(sum(other.get('accountid') == account and other.get(field) == trade[field] for other in trades) != 1
           for field in ('tradeid', 'transactionid') if trade.get(field)):
        return _finish(item, 'conflict', 'duplicate_trade_identity')
    if trade.get('levelofdetail') != 'EXECUTION':
        return _finish(item, 'unverified', 'not_an_individual_execution')
    if day is None or day > as_of:
        return _finish(item, 'unverified', 'missing_or_invalid_trade_date')
    if not item['trade_currency'] or not item['fee_currency']:
        return _finish(item, 'unverified', 'missing_or_invalid_explicit_currency')
    if fee is None:
        return _finish(item, 'unverified', 'missing_or_invalid_explicit_commission')
    if not trade.get('symbol') or trade.get('buysell') not in {'BUY', 'SELL'} or not trade.get('assetcategory'):
        return _finish(item, 'unverified', 'missing_trade_semantics')

    candidates = [(index, line) for index, line in enumerate(lines)
                  if line.get('accountid') == account and _related(trade, line)]
    item['funds_line_indices'] = [index for index, _ in candidates]
    item['candidate_line_count'] = len(candidates)
    if len(candidates) > 1:
        return _finish(item, 'conflict', 'multiple_or_duplicate_funds_identities')
    if not candidates:
        return _finish(item, 'sufficient', 'official_explicit_zero_only', 'explicit_zero') if fee == 0 else _finish(
            item, 'unverified', 'no_uniquely_linked_statement_of_funds_line')
    line = candidates[0][1]
    # A secondary identity must not contradict a matching primary identity.
    if any(line.get(field) and trade.get(field) and line[field] != trade[field]
           for field in ('tradeid', 'transactionid')):
        return _finish(item, 'conflict', 'conflicting_execution_identity')
    if any(line.get(field) for field in ('relatedtradeid', 'relatedtransactionid', 'origtransactionid')):
        return _finish(item, 'unverified', 'related_or_adjusted_identity_not_supported')
    if line.get('levelofdetail') not in {'Currency', 'BaseCurrency', 'EXECUTION'}:
        return _finish(item, 'unverified', 'aggregate_or_unknown_funds_scope')
    if line.get('symbol') != trade['symbol'] or line.get('assetcategory') != trade['assetcategory']:
        return _finish(item, 'conflict', 'instrument_scope_mismatch')
    if trade.get('conid') and line.get('conid') and trade['conid'] != line['conid']:
        return _finish(item, 'conflict', 'instrument_scope_mismatch')
    if line.get('buysell') != trade['buysell']:
        return _finish(item, 'conflict', 'direction_scope_mismatch')
    activity = line.get('activitycode')
    expected = 'FOREX' if trade['assetcategory'] == 'CASH' else trade['buysell']
    if activity not in {expected, 'ADJ'}:
        return _finish(item, 'unverified', 'unsupported_cash_activity_semantics')
    if _date(line.get('date')) != day:
        return _finish(item, 'unverified', 'transaction_date_not_proven')
    if settle is None or settle < day or _date(line.get('settledate')) != settle:
        return _finish(item, 'unverified', 'settlement_date_not_proven')
    if settle > as_of:
        return _finish(item, 'unverified', 'settlement_after_report_as_of')
    if currency_code(line.get('currency')) != item['fee_currency']:
        return _finish(item, 'unverified', 'fee_currency_not_proven_no_fx_conversion')

    fields = ('amount', 'debit', 'credit', 'tradegross', 'tradecommission', 'tradetax')
    numbers = [_number(line.get(field)) for field in fields]
    reported_commission = _number(line.get('tradecommission'))
    if reported_commission is not None and reported_commission != fee:
        return _finish(item, 'conflict', 'signed_commission_mismatch')
    components = [numbers[index] for index in (0, 3, 4, 5)]
    if all(value is not None for value in components):
        amount, gross, commission, tax = (value for value in components if value is not None)
        item['report_component_arithmetic'] = _sum_matches(amount, gross, commission, tax)
        if item['report_component_arithmetic'] is False:
            return _finish(item, 'conflict', 'cash_line_arithmetic_or_sign_conflict')
    if any(value is None for value in numbers):
        if fee == 0 and reported_commission == 0:
            item['cash_evidence_reason'] = 'missing_or_invalid_funds_number'
            return _finish(item, 'sufficient', 'official_explicit_zero_only', 'explicit_zero')
        return _finish(item, 'unverified', 'missing_or_invalid_funds_number')
    amount, debit, credit, gross, commission, tax = (value for value in numbers if value is not None)
    checks = (_sum_matches(amount, debit, credit), item['report_component_arithmetic'])
    if None in checks:
        return _finish(item, 'unverified', 'unsupported_decimal_range')
    item['report_line_arithmetic'] = all(checks)
    if not all(checks) or debit > 0 or credit < 0 or (debit != 0 and credit != 0):
        return _finish(item, 'conflict', 'cash_line_arithmetic_or_sign_conflict')
    if fee == 0:
        return _finish(item, 'sufficient', 'official_and_funds_explicit_zero', 'explicit_zero')
    if gross == 0 and tax == 0 and amount == fee:
        return _finish(item, 'sufficient', 'uniquely_linked_commission_only_cash_line', 'separate')
    if activity == 'ADJ':
        return _finish(item, 'unverified', 'adjustment_is_not_a_commission_only_cash_line')
    if trade['assetcategory'] == 'CASH':
        return _finish(item, 'unverified', 'fx_cash_legs_not_individual_settlement')
    if item['trade_currency'] != item['fee_currency']:
        return _finish(item, 'unverified', 'translated_principal_not_native_settlement')
    proceeds, taxes, net_cash = (_number(trade.get(field)) for field in ('proceeds', 'taxes', 'netcash'))
    if proceeds is None or taxes is None or net_cash is None:
        return _finish(item, 'unverified', 'missing_or_invalid_trade_cash_numbers')
    if gross != proceeds or tax != taxes or amount != net_cash:
        return _finish(item, 'conflict', 'trade_and_funds_cash_mismatch')
    if (trade['buysell'] == 'BUY' and proceeds >= 0) or (trade['buysell'] == 'SELL' and proceeds <= 0):
        return _finish(item, 'conflict', 'principal_direction_conflict')
    return _finish(item, 'sufficient', 'same_currency_individual_cash_includes_commission', 'included')


def review_fee_settlement(*, xml_text: str, quality: Mapping[str, Any]) -> dict[str, Any]:
    """Review only the caller-selected official XML; never select reports or perform I/O.

    Append this result to fee_review without replacing its unit/fact/lineage checks.
    Sufficient means evidence in the stated scope, not a full account/NAV approval.
    """
    result: dict[str, Any] = {
        'status': 'unverified', 'read_only': True, 'executable': False,
        'additional_deduction_proposed': False, 'financial_values_changed': False,
        'scope': 'selected_official_trade_and_statement_of_funds_only_not_fact_or_nav_verification',
        'daily_aggregate_is_individual_proof': False, 'records': [], 'total': None,
        'report_sha256': hashlib.sha256(xml_text.encode('utf-8')).hexdigest(),
    }
    try:
        root = ET.fromstring(xml_text)
        statements = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'FlexStatement']
        if not statements:
            return {**result, 'reason': 'missing_statements'}
        attributes = [_attributes(statement) for statement in statements]
        accounts = {attrs.get('accountid') for attrs in attributes}
        ends = [_date(attrs.get('todate')) for attrs in attributes]
        if len(accounts) != 1 or not all(accounts) or any(end is None for end in ends):
            return {**result, 'reason': 'unverified_report_or_account'}
        account = str(next(iter(accounts)))
        as_of = max(end for end in ends if end is not None)
        if len(statements) > 1:
            periods = sorted((_date(attrs.get('fromdate')) or date.min, end)
                             for attrs, end in zip(attributes, ends) if end is not None)
            if (any(start == date.min or start > end for start, end in periods)
                    or any(left[1] >= right[0] for left, right in pairwise(periods))):
                return {**result, 'reason': 'missing_or_overlapping_statement_periods'}
        if (quality.get('status') != 'ok' or quality.get('legacy_unverified')
                or quality.get('statement_as_of') != as_of.isoformat()
                or quality.get('expected_statement_as_of') != as_of.isoformat()):
            return {**result, 'reason': 'unverified_report_or_account'}
        trades = [_attributes(node) for statement in statements for node in statement.iter()
                  if node.tag.rsplit('}', 1)[-1] == 'Trade']
        lines = [_attributes(node) for statement in statements for node in statement.iter()
                 if node.tag.rsplit('}', 1)[-1] == 'StatementOfFundsLine']
        records = [_review_trade(trade, trades, lines, account, as_of) for trade in trades]
    except (ET.ParseError, ValueError):
        return {**result, 'reason': 'malformed_or_ambiguous_xml'}
    return {**result, 'status': 'review', 'statement_as_of': as_of.isoformat(), 'statement_count': len(statements),
            'records': records, 'total': len(records), 'counts': dict(Counter(item['status'] for item in records)),
            'reason': 'scoped_evidence_only' if trades else 'missing_trade_section'}
