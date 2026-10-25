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

ISSUE_LIMIT = 30


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


def _ancestor_scope(node: ET.Element, parents: Mapping[ET.Element, ET.Element],
                    account: str) -> tuple[tuple[date, date] | None, str | None]:
    """Intersect explicit ancestor coverage; never borrow a sibling's period/scope."""
    chain = []
    current: ET.Element | None = node
    while current is not None:
        chain.append(current)
        current = parents.get(current)
    # Account/model contradictions stay global even when an earlier period is bad.
    for ancestor in reversed(chain):
        attrs = _attributes(ancestor)
        if attrs.get('model') or any(attrs.get(key) and attrs[key] != account for key in ('accountid', 'acctid')):
            return None, 'contradictory_account_or_model_scope'
    period = None
    for ancestor in reversed(chain):
        attrs = _attributes(ancestor)
        if 'fromdate' in attrs or 'todate' in attrs:
            start, end = _date(attrs.get('fromdate')), _date(attrs.get('todate'))
            if start is None or end is None or start > end:
                return None, 'missing_or_invalid_ancestor_period'
            period = (max(start, period[0]), min(end, period[1])) if period else (start, end)
            if period[0] > period[1]:
                return None, 'disjoint_ancestor_periods'
    if not any(ancestor.tag.rsplit('}', 1)[-1] == 'FlexStatement' for ancestor in chain):
        return None, 'missing_statement_ancestor'
    if node.tag.rsplit('}', 1)[-1] == 'Trade' and not any(
            ancestor.tag.rsplit('}', 1)[-1] == 'Trades' for ancestor in chain[1:]):
        return None, 'missing_trade_section_ancestor'
    return period, None


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
                  lines: list[dict[str, str]], account: str, as_of: date,
                  line_issues: Mapping[int, str], trade_issue: str | None) -> dict[str, Any]:
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
    if trade_issue:
        return _finish(item, 'unverified', trade_issue)
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
    linked_issues = [{'line_index': index, 'reason': line_issues[index]}
                     for index, _ in candidates if index in line_issues]
    item['funds_scope_issues'] = linked_issues[:ISSUE_LIMIT]
    item['funds_scope_issue_count'] = len(linked_issues)
    item['funds_scope_issues_truncated'] = len(linked_issues) > ISSUE_LIMIT
    if len(candidates) > 1:
        return _finish(item, 'conflict', 'multiple_or_duplicate_funds_identities')
    if linked_issues:
        return _finish(item, 'unverified', line_issues[candidates[0][0]])
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


def _scope_issue(summary: dict[str, Any], reason: str, section: str, index_key: str, index: int | None) -> None:
    summary['issue_count'] += 1
    summary['reason_counts'][reason] = summary['reason_counts'].get(reason, 0) + 1
    if len(summary['issues']) < ISSUE_LIMIT:
        summary['issues'].append({'reason': reason, 'section': section, index_key: index})
    summary['truncated'] = summary['issue_count'] > len(summary['issues'])


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
        'funds_scope_diagnostics': {'evaluated': False, 'line_count': None, 'unsupported_line_count': None,
                                  'issue_count': 0, 'reason_counts': {}, 'issues': [], 'truncated': False},
        'trade_scope_diagnostics': {'evaluated': False, 'trade_count': None, 'unsupported_trade_count': None,
                                  'issue_count': 0, 'reason_counts': {}, 'issues': [], 'truncated': False},
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
        starts = [_date(attrs.get('fromdate')) for attrs in attributes]
        periods = sorted((start, end) for start, end in zip(starts, ends) if start is not None and end is not None)
        if (len(periods) != len(statements) or any(start > end for start, end in periods)
                or any(left[1] >= right[0] for left, right in pairwise(periods))):
            return {**result, 'reason': 'missing_or_overlapping_statement_periods'}
        if (quality.get('status') != 'ok' or quality.get('legacy_unverified') or quality.get('using_previous')
                or quality.get('statement_as_of') != as_of.isoformat()
                or quality.get('expected_statement_as_of') != as_of.isoformat()):
            return {**result, 'reason': 'unverified_report_or_account'}
        parents = {child: parent for parent in root.iter() for child in parent}
        scoped_tags = {'FlexStatement', 'AccountInformation', 'Trades', 'Trade',
                       'StatementOfFunds', 'StmtFunds', 'StatementOfFundsLine'}
        funds_tags = {'StatementOfFunds', 'StmtFunds', 'StatementOfFundsLine'}
        trade_nodes = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'Trade']
        line_nodes = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'StatementOfFundsLine']
        trade_indices = {node: index for index, node in enumerate(trade_nodes)}
        line_indices = {node: index for index, node in enumerate(line_nodes)}
        trade_issues: dict[int, str] = {}
        line_issues: dict[int, str] = {}
        diagnostics = result['funds_scope_diagnostics']
        trade_diagnostics = result['trade_scope_diagnostics']
        coverage: dict[ET.Element, tuple[date, date]] = {}
        for node in root.iter():
            tag = node.tag.rsplit('}', 1)[-1]
            if tag not in scoped_tags:
                continue
            period, reason = _ancestor_scope(node, parents, account)
            if period is None and reason is None:
                reason = 'missing_ancestor_period'
            if period is not None:
                coverage[node] = period
            field = {'Trade': 'tradedate', 'StatementOfFundsLine': 'date'}.get(tag)
            if field and not reason and period is not None:
                day = _date(_attributes(node).get(field))
                if day is None:
                    reason = 'missing_or_invalid_row_date'
                elif not period[0] <= day <= period[1]:
                    reason = 'row_date_outside_ancestor_period'
            if reason:
                if tag not in funds_tags | {'Trades', 'Trade'} or reason == 'contradictory_account_or_model_scope':
                    return {**result, 'reason': reason, 'section': tag, 'field': field}
                if tag == 'StatementOfFundsLine':
                    index = line_indices[node]
                    line_issues[index] = reason
                    _scope_issue(diagnostics, reason, tag, 'line_index', index)
                elif tag == 'Trade':
                    index = trade_indices[node]
                    trade_issues[index] = reason
                    _scope_issue(trade_diagnostics, reason, tag, 'trade_index', index)
                elif tag == 'Trades' and not any(child in trade_indices for child in node.iter()):
                    return {**result, 'reason': reason, 'section': tag, 'field': field}
                elif tag in funds_tags and not any(child in line_indices for child in node.iter()):
                    _scope_issue(diagnostics, reason, tag, 'line_index', None)
        diagnostics.update(evaluated=True, line_count=len(line_nodes), unsupported_line_count=len(line_issues))
        trade_diagnostics.update(evaluated=True, trade_count=len(trade_nodes), unsupported_trade_count=len(trade_issues))
        if any(not any(node.tag.rsplit('}', 1)[-1] == 'Trades' for node in statement.iter())
               for statement in statements):
            return {**result, 'reason': 'missing_trade_section'}
        if not trade_nodes and any(list(node) for node in coverage if node.tag.rsplit('}', 1)[-1] == 'Trades'):
            return {**result, 'reason': 'unsupported_nonempty_trade_section'}
        trades, lines = [_attributes(node) for node in trade_nodes], [_attributes(node) for node in line_nodes]
        records = []
        for index, (node, trade) in enumerate(zip(trade_nodes, trades)):
            item = _review_trade(trade, trades, lines, account, as_of, line_issues, trade_issues.get(index))
            item['trade_evidence_period'] = {
                'from_date': coverage[node][0].isoformat() if node in coverage else None,
                'to_date': coverage[node][1].isoformat() if node in coverage else None,
                **({'scope_verified': False, 'reason': trade_issues[index]} if index in trade_issues else {})}
            item['funds_evidence_periods'] = [
                {'line_index': index,
                 'from_date': coverage[line_nodes[index]][0].isoformat() if line_nodes[index] in coverage else None,
                 'to_date': coverage[line_nodes[index]][1].isoformat() if line_nodes[index] in coverage else None,
                 **({'scope_verified': False, 'reason': line_issues[index]} if index in line_issues else {})}
                for index in item['funds_line_indices']]
            records.append(item)
    except (ET.ParseError, ValueError):
        return {**result, 'reason': 'malformed_or_ambiguous_xml'}
    return {**result, 'status': 'review', 'statement_as_of': as_of.isoformat(), 'statement_count': len(statements),
            'records': records, 'total': len(records), 'counts': dict(Counter(item['status'] for item in records)),
            'reason': 'scoped_evidence_only' if trades else 'explicit_empty_trade_section'}
