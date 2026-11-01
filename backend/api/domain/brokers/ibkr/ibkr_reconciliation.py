"""Read-only report-to-ledger evidence. Never refresh, repair or value an account."""
from __future__ import annotations

from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


from collectors.ibkr_flex import default_data_dir
from domain.brokers.ibkr.ibkr_performance import get_conn
from domain.brokers.ibkr.ibkr_report_state import PublishedReport, StoredReport, report_quality, select_report

TOLERANCE = Decimal('0.00000001')  # NUMERIC(28,10) versus the existing float parser; no FX rounding.
ISSUE_LIMIT = 30
LABELS = {
    'source_unverified': '报告过期、历史未核准或沿用上一发布值',
    'missing_report': '缺少有效发布报告', 'read_failed': '只读对账暂不可用',
    'missing_table': '对账所需表缺失', 'ambiguous_account': '来源账户不唯一或缺失',
    'missing_row': '缺少对应入账记录', 'duplicate_row': '同一身份有多条入账记录',
    'source_duplicate': '原文同一身份有多条记录，需核准',
    'missing_value': '原文或入账字段缺失，未按零处理', 'invalid_value': '字段不是有效有限数值',
    'value_mismatch': '字段对账不一致', 'scope_mismatch': '账户、日期、标的或币种不可比',
    'snapshot_date_mismatch': '当前快照日期与报告日期不同，仅提示不比较',
    'missing_trade_section': '原文未提供交易节', 'unstable_trade_identity': '缺稳定执行编号，仅按源键比对',
    'fee_currency_mismatch': '手续费币种与交易币种不同，费用需另行核准',
    'outside_report': '存在未被本报告执行记录覆盖的入账，未认定为错单',
    'scope_unavailable': '依据不足，未执行本项对账',
    'source_link_mismatch': '稳定源键与当前物理来源 ID 关联需核准，未自动修复',
}


def _check(key: str, label: str) -> dict:
    return {'id': key, 'label': label, 'status': 'ok', 'compared': 0,
            'issue_count': 0, 'reason_counts': {}, 'issues': [], 'truncated': False}


def _issue(check: dict, reason: str, *, status: str = 'review', **evidence) -> None:
    if check['status'] != 'error':
        check['status'] = status
    check['issue_count'] += 1
    check['reason_counts'][reason] = check['reason_counts'].get(reason,0) + 1
    if len(check['issues']) < ISSUE_LIMIT:
        check['issues'].append({'reason': reason, 'label': LABELS[reason], 'status': status, **evidence})
    check['truncated'] = check['issue_count'] > len(check['issues'])


def _text(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, date) else str(value) if value is not None else None


def _number(value: Any) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('nonfinite reconciliation field')
    return number


def _numbers(check: dict, expected: dict, actual: dict, fields: tuple[str, ...], **evidence) -> None:
    check['compared'] += 1
    for field in fields:
        left, right = expected.get(field), actual.get(field)
        detail = {**evidence, 'field': field, 'expected': _text(left), 'actual': _text(right),
                  'tolerance': str(TOLERANCE)}
        if left is None or right is None:
            _issue(check, 'missing_value', **detail)
            continue
        try:
            delta = _number(right) - _number(left)
        except (InvalidOperation, ValueError):
            _issue(check, 'invalid_value', **detail)
            continue
        if abs(delta) > TOLERANCE:
            _issue(check, 'value_mismatch', status='error', delta=str(delta), **detail)


def _result(checks: list[dict], scope: dict, report: PublishedReport | None = None, quality: dict | None = None) -> dict:
    counts = {status: sum(check['status'] == status for check in checks) for status in ('ok','review','error')}
    status = 'error' if counts['error'] else 'review' if counts['review'] else 'ok'
    label = {'ok':'可比范围对账一致','review':'对账待核准','error':'对账发现差异或读取失败'}[status]
    details = f"只读对账：{label}；一致 {counts['ok']} 项，待核准 {counts['review']} 项，异常 {counts['error']} 项。"
    details += '；'.join(check['label'] + '：' + '、'.join(
        f"{LABELS[reason]} {count} 项" for reason,count in check['reason_counts'].items()
    ) for check in checks if check['issue_count'])
    return {'status':status, 'label':label, 'read_only':True, 'scope':scope, 'counts':counts,
            'report': {'run_id':report.run_id, 'sha256':report.sha256, 'quality':quality} if report else None,
            'checks':checks, 'details':details}


def read_ibkr_reconciliation(*, connection_factory=None, raw_dir: Path | None = None) -> dict:
    """Own a fresh repeatable-read, server-enforced read-only transaction; no runtime DDL."""
    try:
        with closing((connection_factory or get_conn)()) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '5000ms'")
                    required = ('broker_processing_state', 'source_batches', 'source_files', 'source_items',
                                'pams_cashflow_fact', 'pams_investment_trade_fact', 'pams_holdings_snapshot',
                                'account_snapshot_version', 'account_snapshot_member')
                    for table in required:
                        cur.execute('SELECT to_regclass(%s) IS NOT NULL AS present', ('public.' + table,))
                        row = cur.fetchone()
                        present = row.get('present') if isinstance(row, dict) else row[0]
                        if not present:
                            item = _check('source', '报告依据')
                            _issue(item, 'missing_table', source_table=table)
                            return _result([item], {'source_status': 'unknown'})
                report, latest = select_report(conn, raw_dir or default_data_dir() / 'raw/ibkr')
                if report is None:
                    item = _check('source','报告依据')
                    _issue(item, 'missing_report')
                    return _result([item], {})
                if isinstance(report.path, StoredReport):
                    from domain.brokers.ibkr.ibkr_unified_reconciliation import reconcile_unified_report
                    return reconcile_unified_report(conn, report, report_quality(report, latest))
                item = _check('source', '报告依据')
                _issue(item, 'source_unverified')
                return _result([item], {'source_status': 'unknown'}, report, report_quality(report, latest))
    except Exception as exc:
        item = _check('source','报告依据')
        _issue(item, 'read_failed', status='error', error_type=type(exc).__name__)
        return _result([item], {})
