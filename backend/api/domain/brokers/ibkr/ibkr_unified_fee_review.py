"""Read-only commission review for a verified report comparison.

This examines explicit report values and source-linked facts; it neither
establishes account ownership nor proposes a further cash deduction.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

RECORD_LIMIT = 50


def _amount(value):
    try:
        number = Decimal(str(value)) if value not in (None, '') else None
        return number if number is not None and number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def review_unified_fees(rows, *, section_present):
    result = {'status': 'review', 'read_only': True, 'executable': False,
              'records': [], 'total': None, 'truncated': False,
              'existing_fee_and_lineage_blockers_preserved': True, 'tolerance': '1E-8',
              'base_currency': None,
              'scope': 'official commission rows and individually linked facts; not account expenses or cash settlement'}
    if not section_present:
        result['reason'] = 'missing_trade_section'
        return result
    selected = []
    totals = defaultdict(lambda: Decimal('0'))
    complete = True
    for node, comparison in rows:
        raw = {str(k).lower(): str(v).strip() for k, v in node.attrib.items()}
        currency = raw.get('currency') or None
        fee_currency = raw.get('ibcommissioncurrency') or None
        fee = _amount(raw.get('ibcommission'))
        unknown = set(comparison.get('unknown_fields') or [])
        proven = (bool(fee_currency) and comparison.get('status') in {'consistent', 'partial'}
                  and 'fee_amount' not in unknown and 'fee_currency' not in unknown)
        abnormal = fee is None or not fee_currency or fee_currency != currency or not proven
        if not abnormal:
            continue
        if fee is None or not fee_currency or not proven:
            complete = False
        else:
            totals[fee_currency] += fee
        issues = []
        if fee is None:
            issues.append('missing_or_invalid_official_fee')
        if not fee_currency:
            issues.append('missing_official_fee_currency')
        elif fee_currency != currency:
            issues.append('cross_currency_fee')
        if not proven:
            issues.append('fee_fact_source_unverified')
        fact_amounts = comparison.get('fact_amounts') or {}
        actual_currency = fact_amounts.get('fee_currency')
        unit_status = ('matched' if proven and fee_currency and actual_currency==fee_currency else
                       'missing' if comparison.get('fact_id') and actual_currency is None else 'unverified')
        selected.append({'execution_id': comparison.get('execution_id'),
            'source_key': comparison.get('source_key'), 'trade_date': raw.get('tradedate'),
            'symbol': raw.get('symbol'), 'trade_currency': currency, 'fee_currency': fee_currency,
            'official_commission_signed': str(fee) if fee is not None else None,
            'commission_sign': 'unknown' if fee is None else 'zero' if fee == 0 else 'negative' if fee < 0 else 'positive',
            'fact_amounts': comparison.get('fact_amounts'), 'fact_id': comparison.get('fact_id'),
            'source_link_matches': True if proven else None, 'checks': None,
            'stored_fee_numeric_currency_is_explicit': unit_status=='matched',
            'fee_unit_evidence': 'published_original_and_fact' if proven else 'unverified',
            'daily_cash_commission_evidence': {'status':'unavailable','reason':'not_individually_verified'},
            'status': 'review', 'executable': False, 'issues': issues,
            'fact_source_status': comparison.get('status'),
            'unit_review': {'status': 'source_unit_verified' if proven else 'unverified',
                            'reason':'published_original_and_fact_match' if proven else 'incomplete_or_conflicting_evidence',
                            'read_model': {'amount':fact_amounts.get('fee_amount'),'currency':fee_currency,
                                           'provenance':'published_original_and_fact'} if proven else None,
                            'fact_fee_currency_persisted': bool(actual_currency),
                            'currency_model':'explicit_fact_currency' if actual_currency else 'nullable_fact_currency_not_recorded',
                            'fact_currency_check': {'status':unit_status,'column_available':True,'expected':fee_currency,
                                                    'actual':actual_currency,'cash_settlement_verified':False,
                                                    'additional_deduction_proposed':False},
                            'individual_cash_settlement': 'unverified', 'executable':False,
                            'additional_deduction_proposed': False},
            'source_authentication': 'unknown', 'impact': None})
    result.update(status='review' if selected else 'ok', total=len(selected),
                  records=selected[:RECORD_LIMIT], truncated=len(selected)>RECORD_LIMIT,
                  summary={'scanned_trade_rows': len(rows), 'reviewed_rows': len(selected),
                           'numeric_evidence_matched_rows': sum(row['unit_review']['status']=='source_unit_verified' for row in selected),
                           'source_unit_verified_rows': sum(row['unit_review']['status']=='source_unit_verified' for row in selected),
                           'fact_fee_currency_matched_rows': sum(row['unit_review']['fact_currency_check']['status']=='matched' for row in selected),
                           'nonzero_fee_rows': sum(_amount(row['official_commission_signed']) is not None and _amount(row['official_commission_signed']) != 0 for row in selected),
                           'explicit_zero_fee_rows': sum(_amount(row['official_commission_signed']) == 0 for row in selected),
                           'daily_cash_aggregate_matched_rows': None,
                           'equity_reconstruction_excluded_rows': None,
                           'official_subtotal_complete': complete,
                           'aggregation_scope': 'selected_official_fee_rows_not_total_account_expenses',
                           'signed_official_fees_by_currency': {key:str(value) for key,value in sorted(totals.items())} if complete else None})
    return result
