"""Read-only comparison of a published isolated Flex source and current facts.

Consistency is not broker authentication, account ownership, or complete coverage.
The caller provides a single read-only repeatable-read transaction and a report
already selected and integrity-checked by ``select_report``.
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import uuid5
from xml.etree import ElementTree as ET

import psycopg2.extras

from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, verify_ibkr_fact
from domain.brokers.ibkr.ibkr_reconciliation import ISSUE_LIMIT, _check, _issue, _numbers, _result
from domain.brokers.ibkr.ibkr_report_batch import NAMESPACE
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


def _decimal(value):
    try:
        result = Decimal(value) if value not in (None, '') else None
        return result if result is not None and result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def reconcile_unified_report(conn, report, quality):
    content = report.path.read_bytes()
    if sha256(content).hexdigest() != report.sha256:
        raise ValueError('report changed during read')
    root = ET.fromstring(content)
    statements = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'FlexStatement'
                  and node.get('toDate', '').replace('-', '') == report.as_of.strftime('%Y%m%d')]
    if len(statements) != 1 or not statements[0].get('accountId'):
        raise ValueError('report account or date is ambiguous')
    statement = statements[0]
    account = statement.get('accountId')
    batch_id = uuid5(NAMESPACE, 'batch:' + account + ':' + report.as_of.isoformat() + ':' + report.sha256)
    checks = [_check('source', '报告依据'), _check('trade_ledger', '原始交易入账'),
              _check('trade_facts', '投资交易 fact'), _check('position_history', '报告日持仓历史'),
              _check('current_snapshot', '当前快照')]
    source, ledger, facts, history, current = checks
    _issue(source, 'source_unverified', field='broker_and_account_authentication')
    if report.legacy or quality.get('status') != 'ok':
        _issue(source, 'source_unverified', field='report_quality')
    _issue(ledger, 'scope_unavailable', field='legacy_ledger_replaced_by_unified_facts')
    _issue(history, 'scope_unavailable', field='legacy_position_history_not_reconstructed')
    scope = {'account_code': 'ibkr', 'source_account_id': account,
             'statement_as_of': report.as_of.isoformat(), 'source_authentication': 'unknown',
             'holding_identity': 'unknown', 'trade_scope': '仅核对原文行与其已发布关联事实；未关联观察不计为同源',
             'snapshot_coverage': 'unknown', 'snapshot_cash_status': 'unknown',
             'not_compared': '账户归属、券商认证、完整范围、现金余额、NAV、FX、收益率',
             'numeric_tolerance': '0.00000001', 'comparison_currency': '原币，不折算 CNY'}
    trades = [node for node in statement.iter() if node.tag.rsplit('}', 1)[-1] == 'Trade']
    positions = [node for node in statement.iter() if node.tag.rsplit('}', 1)[-1] == 'OpenPosition']
    scope['reported_trade_rows'] = len(trades)
    scope['reported_security_rows'] = len(positions)
    facts['records'] = []
    fee_rows = []
    facts['records_total'] = len(trades)
    facts['records_truncated'] = len(trades) > ISSUE_LIMIT
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND raw_fields->>'kind'='trade'", (str(batch_id),))
        items = [dict(row) for row in cur.fetchall()]
        by_raw = defaultdict(list)
        for item in items:
            raw = (item.get('raw_fields') or {}).get('official_attributes')
            if isinstance(raw, dict):
                by_raw[tuple(sorted(raw.items()))].append(item)
        raw_counts = Counter(tuple(sorted(node.attrib.items())) for node in trades)
        cache = EvidenceCache()
        trade_section_present = any(n.tag.rsplit('}', 1)[-1] in {'Trade', 'Trades'} for n in statement.iter())
        if not trade_section_present:
            _issue(facts, 'missing_trade_section')
        if any(key not in raw_counts for key in by_raw):
            _issue(facts, 'scope_unavailable', field='source_items_outside_report')
        for node in trades:
            raw_key = tuple(sorted(node.attrib.items()))
            identifier = node.get('tradeID') or node.get('transactionID') or node.get('ibExecID')
            record = {'execution_id': identifier, 'currency': node.get('currency'), 'status': 'unknown',
                      'source_authentication': 'unknown', 'raw_amounts': {
                          field: node.get(attribute) for field, attribute in (
                              ('quantity', 'quantity'), ('price', 'tradePrice'), ('gross_amount', 'tradeMoney'),
                              ('fee_amount', 'ibCommission'), ('fee_currency', 'ibCommissionCurrency'),
                              ('tax_amount', 'taxAmount'), ('net_cash', 'netCash'))}, 'fact_amounts': None}
            fee_rows.append((node, record))
            if len(facts['records']) < ISSUE_LIMIT:
                facts['records'].append(record)
            if raw_counts[raw_key] != 1 or len(by_raw[raw_key]) != 1:
                record['reason'] = 'unique_source_row'
                _issue(facts, 'scope_unavailable', field='unique_source_row', key=identifier)
                continue
            item = by_raw[raw_key][0]
            record['source_key'] = item.get('source_uid')
            parsed = item.get('parsed_fields') or {}
            if item.get('review_status') != 'confirmed' or parsed.get('observation') != 'created':
                record['reason'] = 'source_link'
                _issue(facts, 'scope_unavailable', field='source_link', key=identifier)
                continue
            fact_id = parsed.get('fact_id')
            if not isinstance(fact_id, int) or isinstance(fact_id, bool):
                record['reason'] = 'fact_identity'
                _issue(facts, 'scope_unavailable', field='fact_identity', key=identifier)
                continue
            result = verify_ibkr_fact(cur, trade_id=fact_id, report_bytes=content, cache=cache)
            if (result['status'] == 'unavailable' or result.get('source_item_id') != str(item['source_item_id'])
                    or result.get('report_sha256') != report.sha256):
                record['reason'] = 'fact_source_verification'
                _issue(facts, 'scope_unavailable', field='fact_source_verification', key=identifier,
                       evidence_reason=result.get('reason'))
                continue
            cur.execute("""SELECT quantity,price,gross_amount,fee_amount,fee_currency,tax_amount,net_cash
                FROM pams_investment_trade_fact WHERE trade_id=%s""", (fact_id,))
            values = cur.fetchone()
            if not values:
                raise ValueError('verified fact disappeared from transaction snapshot')
            record.update(status=result['status'], unknown_fields=result['unknown_fields'], fact_id=fact_id,
                          fact_amounts={key: str(value) if value is not None else None
                                        for key, value in values.items()})
            facts['compared'] += 1
            for field in result['unknown_fields']:
                _issue(facts, 'missing_value', field=field, key=identifier)
        cur.execute("""SELECT * FROM pams_holdings_snapshot WHERE account_code='ibkr'
            AND as_of=(SELECT MAX(as_of) FROM pams_holdings_snapshot WHERE account_code='ibkr')""")
        snapshots = [dict(row) for row in cur.fetchall()]
        dates = {row['as_of'] for row in snapshots}
        if dates != {report.as_of}:
            _issue(current, 'snapshot_date_mismatch', expected=report.as_of.isoformat(),
                   actual=sorted(str(day) for day in dates))
        else:
            coverage = load_account_snapshot_coverage(cur, {('ibkr', report.as_of)})[('ibkr', report.as_of)]
            scope['snapshot_coverage'] = coverage['status']
            scope['snapshot_cash_status'] = coverage['cash_status']
            if coverage['status'] != 'complete':
                _issue(current, 'scope_unavailable', field='account_snapshot_coverage', actual=coverage['status'])
            actual = defaultdict(list)
            for row in snapshots:
                if row.get('major_category') != '现金':
                    actual[(row.get('symbol'), row.get('currency'))].append(row)
            source_counts = Counter((n.get('symbol'), n.get('currency')) for n in positions)
            for node in positions:
                key = (node.get('symbol'), node.get('currency'))
                rows = actual.get(key, [])
                if (node.get('accountId') not in (None, '', account) or
                        node.get('reportDate', '').replace('-', '') != report.as_of.strftime('%Y%m%d')):
                    _issue(current, 'scope_unavailable', field='position_account_or_date', key=str(key))
                    continue
                if not all(key) or source_counts[key] != 1 or len(rows) != 1:
                    _issue(current, 'scope_unavailable', field='unique_position_candidate', key=str(key))
                    continue
                row = rows[0]
                # Symbol and currency select only a candidate; they never prove conid or provenance.
                _issue(current, 'scope_unavailable', field='holding_identity', key=str(key), record_id=str(row['id']))
                expected = {'quantity': _decimal(node.get('position') or node.get('quantity')),
                            'cost': _decimal(node.get('costBasisMoney')),
                            'market_value': _decimal(node.get('positionValue') or node.get('marketValue')),
                            'current_price': _decimal(node.get('markPrice')),
                            'buy_price': _decimal(node.get('costBasisPrice'))}
                candidate = _check('candidate', '身份待核验的数值候选')
                _numbers(candidate, expected, row, tuple(expected), key=str(key), record_id=str(row['id']),
                         comparison='candidate_only_identity_unknown')
                current['compared'] += candidate['compared']
                for issue in candidate['issues']:
                    _issue(current, issue['reason'], **{k: v for k, v in issue.items()
                           if k not in {'reason', 'label', 'status'}})
            extra = sum(len(rows) for key, rows in actual.items() if key not in source_counts)
            if extra:
                _issue(current, 'outside_report', actual=extra)
    result = _result(checks, scope, report, quality)
    from domain.brokers.ibkr.ibkr_unified_fee_review import review_unified_fees
    fee_review = review_unified_fees(fee_rows, section_present=trade_section_present)
    try:
        from domain.brokers.ibkr.ibkr_fee_settlement import _review_fee_settlement
        fee_review['settlement_evidence'] = _review_fee_settlement(xml_text=content.decode('utf8'),quality=quality,root=root)
    except Exception as exc:
        fee_review['settlement_evidence'] = {'status':'unavailable','read_only':True,'executable':False,
            'error_type':type(exc).__name__,'records':[],'total':None,
            'additional_deduction_proposed':False,'financial_values_changed':False}
    result['fee_review'] = fee_review
    return result
