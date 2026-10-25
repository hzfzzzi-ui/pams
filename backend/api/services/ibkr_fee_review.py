"""Read-only fee evidence; never supplies amounts to valuation or repairs facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any
import xml.etree.ElementTree as ET

from collectors.ibkr_flex import parse_flex_report, _parse_ib_date
from services.cashflow_fact import make_source_hash
from services.investment_return_data import IBKR_EQUITY_MARKETS
from services.investment_trade_fact import classify_trade_type, normalize_side

TOLERANCE = Decimal('0.00000001')
RECORD_LIMIT = 100


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _raw(row: dict) -> dict:
    if isinstance(row.get('raw'), dict) and isinstance(row.get('raw_json'), dict) and row['raw'] != row['raw_json']:
        return {}
    value = row.get('raw') or row.get('raw_json')
    return {str(k).lower(): v for k, v in value.items()} if isinstance(value, dict) else {}


def _currency(value: Any) -> str | None:
    return str(value).strip().upper() if value is not None and str(value).strip() else None


def _execution(row: dict) -> str | None:
    for field in ('trade_id', 'transaction_id'):
        if row.get(field):
            return field + ':' + str(row[field])
    return None


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _scope_matches(expected: dict, actual: dict, fields: tuple[str, ...]) -> bool:
    return all(expected.get(key) is not None and actual.get(key) is not None
               and str(expected[key]) == str(actual[key]) for key in fields)


def _comparison(expected: Any, actual: Any, *, absolute: bool = False) -> dict:
    left, right = _number(expected), _number(actual)
    if left is None or right is None:
        return {'status': 'unverified', 'expected': _text(expected), 'actual': _text(actual), 'delta': None}
    if absolute:
        left = abs(left)
    delta = right - left
    return {'status': 'matched' if abs(delta) <= TOLERANCE else 'mismatch',
            'expected': str(left), 'actual': str(right), 'delta': str(delta)}


def _daily_cash_evidence(root: ET.Element, trades: list[dict], base: str | None, day: Any, account: str) -> dict:
    """Compare the whole day's commissions, never allocate an aggregate to one fill."""
    result: dict[str, Any] = {'status': 'unverified', 'base_currency': base,
                             'individual_allocation_verified': False, 'date': str(day)}
    if not base or day is None:
        return {**result, 'reason': 'missing_base_currency_or_date'}
    total = Decimal('0')
    for trade in trades:
        if trade.get('trade_date') != day:
            continue
        raw = _raw(trade)
        if _parse_ib_date(raw.get('tradedate')) != day or not _currency(raw.get('currency')):
            return {**result, 'reason': 'incomplete_daily_trade_scope'}
        fee, fee_currency = _number(raw.get('ibcommission')), _currency(raw.get('ibcommissioncurrency'))
        trade_currency = _currency(trade.get('currency'))
        if fee is None or not fee_currency:
            return {**result, 'reason': 'incomplete_daily_fee_evidence'}
        rate: Decimal | None
        if fee_currency == base:
            rate = Decimal('1')
        elif fee_currency == trade_currency:
            rate = _number(raw.get('fxratetobase'))
        else:
            rate = None
        if rate is None or rate <= 0:
            return {**result, 'reason': 'missing_official_fee_fx'}
        total += fee * rate
    compact_day = str(day).replace('-', '')
    cash = [element.attrib for element in root.iter()
            if element.tag.rsplit('}', 1)[-1] == 'CashReportCurrency'
            and element.get('currency') == 'BASE_SUMMARY'
            and element.get('fromDate') == compact_day and element.get('toDate') == compact_day]
    if len(cash) != 1:
        return {**result, 'reason': 'missing_or_duplicate_daily_cash_report'}
    if cash[0].get('accountId', account) != account:
        return {**result, 'reason': 'cash_account_mismatch'}
    comparison = _comparison(total, cash[0].get('commissions'))
    return {**result, **comparison, 'scope': 'all_reported_trades_on_day',
            'reason': 'daily_base_commissions_only_not_individual_settlement'}


def _impact(fact: dict, checks: dict) -> dict:
    market = next((fact.get(key) for key in ('market', 'asset_minor_category', 'asset_major_category')
                   if fact.get(key)), None)
    excluded = market is not None and market not in IBKR_EQUITY_MARKETS
    cash_matched = all(checks[key]['status'] == 'matched' for key in ('source_net_cash', 'fact_net_cash'))
    return {
        'cash': {'status': 'source_value_preserved' if cash_matched else 'unverified',
                 'net_cash': _text(fact.get('net_cash')), 'currency': fact.get('currency'),
                 'net_cash_cny': _text(fact.get('net_cash_cny')), 'affects_cash': fact.get('affects_cash'),
                 'reader': 'account_balance: net_cash_cny/net_cash; no separate fee_amount subtraction',
                 'fee_inclusion_in_individual_cash': 'unverified',
                 'note': 'A zero FX netCash is not proof of a missing fee; cash legs and settlement are not reconstructed.'},
        'cost': {'status': 'excluded_from_equity_reconstruction' if excluded else 'requires_review',
                 'effective_market': market, 'affects_cost_basis': fact.get('affects_cost_basis'),
                 'reader': 'investment_return_ibkr: equity-market filter; nonzero net_cash before fee fallback'},
        'pnl': {'affects_realized_pnl': fact.get('affects_realized_pnl'),
                'official_account_reader': 'ibkr_performance: CashReport endingCash and positions, not fact fee_amount',
                'individual_fee_effect_on_nav_or_pnl': None,
                'status': 'not_quantified_no_extra_fee_deduction_proposed'},
    }


def _unit_review(item: dict, fact: dict | None = None, embedded: dict | None = None) -> dict:
    """A derived unit is not a persisted currency or a cash-settlement approval."""
    fact, embedded = fact or {}, embedded or {}
    currency = item.get('fee_currency')
    side = 'fx_conversion' if classify_trade_type(embedded) == 'fx_conversion' else normalize_side(embedded)
    verified = bool(
        currency and len(currency) == 3 and currency.isascii() and currency.isalpha()
        and item.get('fee_unit_evidence') == 'raw_and_embedded_only_no_fact_fee_currency_column'
        and item.get('checks') and all(check['status'] == 'matched' for check in item['checks'].values())
        and str(embedded.get('side', '')).upper() == str(_raw(embedded).get('buysell', '')).upper() != ''
        and _scope_matches(fact, {**embedded, 'side': side},
                           ('source_table','source_key','account_code','trade_date','symbol','currency','side'))
        and fact.get('source_hash') == make_source_hash('pams_ibkr_trades', fact)
        and all(_comparison(fact.get(key), embedded.get(key))['status'] == 'matched'
                for key in ('quantity','price','gross_amount','fee_amount','tax_amount','net_cash')))
    return {'status': 'source_unit_verified' if verified else 'unverified',
            'reason': 'raw_embedded_and_scalar_match' if verified else 'incomplete_or_conflicting_evidence',
            'read_model': {'amount': _text(fact['fee_amount']), 'currency': currency,
                           'provenance': 'official_and_immutable_embedded_raw'} if verified else None,
            'fact_fee_currency_persisted': False, 'individual_cash_settlement': 'unverified',
            'currency_model': 'not_modeled', 'additional_deduction_proposed': False, 'executable': False}


def review_fees(*, xml_text: str, quality: dict, trades: list[dict], facts: list[dict]) -> dict:
    """All inputs are frozen by the caller's read-only report transaction."""
    try:
        return _review_fees(xml_text=xml_text, quality=quality, trades=trades, facts=facts)
    except Exception as exc:
        return {'status': 'unavailable', 'read_only': True, 'executable': False,
                'error_type': type(exc).__name__, 'records': [], 'total': None}


def _review_fees(*, xml_text: str, quality: dict, trades: list[dict], facts: list[dict]) -> dict:
    parsed, root = parse_flex_report(xml_text), ET.fromstring(xml_text)
    result: dict[str, Any] = {'status': 'review', 'read_only': True, 'executable': False,
        'tolerance': str(TOLERANCE), 'records': [], 'total': 0, 'truncated': False,
        'scope': 'cross-currency, missing-currency or invalid official fees; not a NAV or settlement reconciliation',
        'existing_fee_and_lineage_blockers_preserved': True}
    if (len(parsed.source_account_ids) != 1 or quality.get('status') != 'ok' or quality.get('legacy_unverified')
            or quality.get('statement_as_of') != str(parsed.statement_as_of)
            or quality.get('expected_statement_as_of') != str(parsed.statement_as_of)):
        return {**result, 'reason': 'unverified_report_or_account', 'total': None}
    if not any(element.tag.rsplit('}', 1)[-1] in {'Trade', 'Trades'} for element in root.iter()):
        return {**result, 'reason': 'missing_trade_section', 'total': None}
    account = parsed.source_account_ids[0]
    bases = {_currency(element.get('currency')) for element in root.iter()
             if element.tag.rsplit('}', 1)[-1] == 'AccountInformation'} - {None}
    base = next(iter(bases)) if len(bases) == 1 else None
    by_execution: dict[tuple, list] = defaultdict(list)
    by_key: dict[Any, list] = defaultdict(list)
    official_counts = Counter((row.get('source_account_id'), _execution(row)) for row in parsed.trades)
    for row in trades:
        by_execution[(row.get('source_account_id'), _execution(row))].append(row)
    for row in facts:
        by_key[row.get('source_key')].append(row)
    records: list[dict] = []
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    cash_groups: dict[Any, dict] = {}
    numeric_matches = nonzero = zero = 0
    subtotal_complete = True
    for official in parsed.trades:
        raw = _raw(official)
        currency, fee_currency = _currency(raw.get('currency')), _currency(raw.get('ibcommissioncurrency'))
        fee = _number(raw.get('ibcommission'))
        if currency and fee_currency == currency and fee is not None:
            continue
        execution = _execution(official)
        key = (official.get('source_account_id'), execution)
        source_key = 'ibkr:' + str(official.get('source_trade_key'))
        item: dict[str, Any] = {'execution_id': execution, 'source_key': source_key,
            'trade_date': str(official.get('trade_date')), 'symbol': official.get('symbol'),
            'trade_currency': currency, 'fee_currency': fee_currency,
            'official_commission_signed': _text(fee), 'status': 'review', 'executable': False,
            'commission_sign': 'unknown' if fee is None else 'zero' if fee == 0 else 'negative' if fee < 0 else 'positive',
            'issues': [], 'checks': {}, 'impact': None}
        item['unit_review'] = _unit_review(item)
        if fee is not None:
            nonzero += fee != 0
            zero += fee == 0
            if fee_currency:
                totals[fee_currency] += fee
        if fee is None or not fee_currency or not execution or official_counts[key] != 1:
            subtotal_complete = False
        candidates, matched_facts = by_execution.get(key, []), by_key.get(source_key, [])
        if not execution or official_counts[key] != 1 or len(candidates) != 1 or len(matched_facts) != 1:
            item['issues'].append('missing_or_ambiguous_execution_or_fact')
        else:
            stored, fact = candidates[0], matched_facts[0]
            expected_side = 'fx_conversion' if classify_trade_type(official) == 'fx_conversion' else normalize_side(official)
            expected_fact = {**official, 'side': expected_side}
            source_ok = _scope_matches(official, stored, ('account_code', 'source_account_id', 'symbol', 'currency', 'trade_date', 'side'))
            fact_ok = (_scope_matches(expected_fact, fact, ('account_code', 'symbol', 'currency', 'trade_date', 'side'))
                       and fact.get('source_table') == 'pams_ibkr_trades'
                       and stored.get('source_trade_key') == official.get('source_trade_key'))
            if (not source_ok or not fact_ok or not currency
                    or _parse_ib_date(raw.get('tradedate')) != official.get('trade_date')):
                item['issues'].append('scope_mismatch')
            else:
                item['fact_id'] = str(fact.get('trade_id'))
                item['source_link_matches'] = str(fact.get('source_id')) == str(stored.get('id'))
                checks = {
                    'source_commission': _comparison(raw.get('ibcommission'), stored.get('commission')),
                    'fact_fee_numeric': _comparison(raw.get('ibcommission'), fact.get('fee_amount'), absolute=True),
                    'source_net_cash': _comparison(raw.get('netcash'), stored.get('net_cash')),
                    'fact_net_cash': _comparison(raw.get('netcash'), fact.get('net_cash')),
                    'source_quantity': _comparison(raw.get('quantity'), stored.get('quantity')),
                    'fact_quantity': _comparison(raw.get('quantity'), fact.get('quantity')),
                }
                item['checks'] = checks
                embedded = fact.get('raw_json')
                embedded = embedded.get('source_row') if isinstance(embedded, dict) else None
                embedded_raw = _raw(embedded) if isinstance(embedded, dict) else {}
                unit_proven = bool(fee_currency and all(
                    fee_currency == _currency(evidence.get('ibcommissioncurrency'))
                    and all(evidence.get(field) == raw.get(field)
                            for field in ('tradeid', 'transactionid', 'accountid', 'currency', 'tradedate'))
                    and _comparison(raw.get('ibcommission'), evidence.get('ibcommission'))['status'] == 'matched'
                    for evidence in (_raw(stored), embedded_raw)))
                item['fee_unit_evidence'] = 'raw_and_embedded_only_no_fact_fee_currency_column' if unit_proven else 'unverified'
                item['stored_fee_numeric_currency_is_explicit'] = False
                if not unit_proven:
                    item['issues'].append('missing_or_conflicting_fee_currency_evidence')
                if any(check['status'] != 'matched' for check in checks.values()):
                    item['issues'].append('numeric_evidence_not_matched')
                else:
                    numeric_matches += 1
                if not fee_currency:
                    item['issues'].append('missing_official_fee_currency')
                # Currency provenance survives in raw JSON, not in the scalar fee_amount schema.
                item['issues'].append('fee_currency_not_modeled')
                item['impact'] = _impact(fact, checks)
                day = official.get('trade_date')
                if day not in cash_groups:
                    cash_groups[day] = _daily_cash_evidence(root, parsed.trades, base, day, account)
                item['daily_cash_commission_evidence'] = cash_groups[day]
                item['unit_review'] = _unit_review(item, fact, embedded)
        item['conclusion'] = ('explicit_zero_fee_not_an_inferred_zero' if fee == 0 else
                              'nonzero_fee_requires_currency_and_cash_scope_review' if fee is not None else
                              'missing_or_invalid_official_fee')
        records.append(item)
    result.update(total=len(records), records=records[:RECORD_LIMIT], truncated=len(records) > RECORD_LIMIT,
                  summary={'scanned_trade_rows': len(parsed.trades), 'reviewed_rows': len(records),
                           'numeric_evidence_matched_rows': numeric_matches, 'nonzero_fee_rows': nonzero,
                           'explicit_zero_fee_rows': zero,
                           'source_unit_verified_rows': sum(
                               row['unit_review']['status'] == 'source_unit_verified' for row in records),
                           'daily_cash_aggregate_matched_rows': sum(
                               row.get('daily_cash_commission_evidence', {}).get('status') == 'matched' for row in records),
                           'equity_reconstruction_excluded_rows': sum(
                               (row.get('impact') or {}).get('cost', {}).get('status') == 'excluded_from_equity_reconstruction'
                               for row in records),
                           'official_subtotal_complete': subtotal_complete,
                           'aggregation_scope': 'selected_official_fee_rows_not_total_account_expenses',
                           'signed_official_fees_by_currency':
                               {key: str(value) for key, value in sorted(totals.items())} if subtotal_complete else None},
                  base_currency=base)
    if not records:
        result['status'] = 'ok'
    return result
