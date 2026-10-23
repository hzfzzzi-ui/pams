"""Read-only report-to-ledger evidence. Never refresh, repair or value an account."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
from typing import Any, Callable
import xml.etree.ElementTree as ET

import psycopg2.extras

from collectors.ibkr_flex import default_data_dir, parse_flex_report, _parse_ib_date, _num
from services.ibkr_performance import get_conn
from services.ibkr_report_state import PublishedReport, report_quality, select_report
from services.investment_trade_fact import classify_trade_type, normalize_side

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


def _scope(check: dict, expected: dict, actual: dict, fields: tuple[str, ...], **evidence) -> bool:
    matched = True
    for field in fields:
        left, right = _text(expected.get(field)), _text(actual.get(field))
        normalized = (left.upper(), right.upper()) if left and right and field in {'symbol','currency','side'} else (left,right)
        if not left or not right or normalized[0] != normalized[1]:
            _issue(check, 'scope_mismatch', field=field, expected=left, actual=right, **evidence)
            matched = False
    return matched


def _groups(rows: list[dict], key: Callable[[dict], Any]) -> dict[Any, list[dict]]:
    grouped: dict[Any, list] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return grouped


def _execution(row: dict) -> str:
    if row.get('trade_id'):
        return 'trade:' + str(row['trade_id'])
    if row.get('transaction_id'):
        return 'transaction:' + str(row['transaction_id'])
    return 'source:' + str(row.get('source_trade_key'))


def _one(check: dict, rows: list, key: str) -> dict | None:
    if len(rows) != 1:
        _issue(check, 'missing_row' if not rows else 'duplicate_row', status='error', key=key,
               expected=1, actual=len(rows), record_ids=[str(r.get('id')) for r in rows[:ISSUE_LIMIT]])
        return None
    return rows[0]


def _official_trade(row: dict) -> dict:
    # The writer's normalization stays authoritative; missing raw fields must not inherit its defaults.
    result = dict(row)
    raw = {key.lower(): value for key, value in row.get('raw', {}).items()}
    aliases = {'quantity': ('quantity','tradequantity','qty'), 'gross_amount': ('trademoney','tradegross','proceeds'),
               'commission': ('ibcommission','tradecommission','commission'), 'net_cash': ('netcash','amount')}
    for field, names in aliases.items():
        values = [_num(raw.get(name)) for name in names]
        if not any(value is not None and math.isfinite(value) for value in values):
            result[field] = None
    for field, names in {'currency': ('currency','reportingcurrency'),
                         'trade_date': ('tradedate','reportdate','date')}.items():
        values = [raw.get(name) for name in names if raw.get(name)]
        if not values or (field == 'trade_date' and _parse_ib_date(values[0]) is None):
            result[field] = None
    return result


def _fact_match(check: dict, by_key: dict, by_id: dict, stored: dict, expected: dict, key: str) -> dict | None:
    # Old imports may have recycled physical IDs. Never compare money using an unrelated ID match.
    source_key = 'ibkr:' + str(expected['source_trade_key'])
    canonical = by_key.get(source_key, [])
    references = by_id.get(str(stored['id']), [])
    if len(canonical) > 1:
        return _one(check, canonical, key)
    if canonical:
        row = canonical[0]
        if str(row.get('source_id')) != str(stored['id']) or len(references) != 1 or references[0] is not row:
            _issue(check, 'source_link_mismatch', key=key, field='source_id',
                   expected=str(stored['id']), actual=str(row.get('source_id')), source_key=source_key)
        return row
    if references:
        _issue(check, 'source_link_mismatch', key=key, field='source_key', expected=source_key,
               actual=[r.get('source_key') for r in references[:ISSUE_LIMIT]])
        return None
    return _one(check, [], key)


def _position_check(check: dict, expected: list, actual: list, account: str, *, history: bool) -> None:
    identity = lambda row: (row.get('symbol'), row.get('currency'))
    source = _groups(expected, identity)
    stored = _groups(actual, identity)
    for key, rows in source.items():
        evidence = {'key': '|'.join(map(str,key)), 'currency': key[1],
                    'source_table': 'pams_ibkr_position_history' if history else 'pams_holdings_snapshot'}
        if len(rows) != 1:
            _issue(check, 'source_duplicate', **evidence)
            continue
        match = _one(check, stored.get(key, []), evidence['key'])
        if match is None:
            continue
        expected_row = {**rows[0], 'source_account_id': account}
        fields = ('account_code','as_of','symbol','currency') + (('source_account_id',) if history else ())
        if _scope(check, expected_row, match, fields, **evidence):
            _numbers(check, expected_row, match, ('quantity',), record_id=str(match.get('id')), **evidence)
    extras = [row for key, rows in stored.items() if key not in source for row in rows]
    if extras:
        _issue(check, 'outside_report', actual=len(extras))


def reconcile_report(*, report: PublishedReport, xml_text: str, quality: dict,
                     trades: list[dict], facts: list[dict], history: list[dict], snapshots: list[dict]) -> dict:
    parsed = parse_flex_report(xml_text)
    checks = [_check('source','报告依据'), _check('trade_ledger','原始交易入账'),
              _check('trade_facts','投资交易 fact'), _check('position_history','报告日持仓历史'),
              _check('current_snapshot','当前快照份额')]
    source, ledger, fact_check, historical, current = checks
    scope: dict[str, Any] = {'account_code': 'ibkr', 'statement_as_of': report.as_of.isoformat(),
             'trade_scope': '仅核对原文明确列出的执行记录，未覆盖记录不自动认定为错单',
             'not_compared': '盘中/收盘价格、市值、成本、现金余额、NAV、FX、收益率和归属比例',
             'numeric_tolerance': str(TOLERANCE), 'comparison_currency': '原币，不折算 CNY'}
    if report.legacy or quality.get('legacy_unverified') or quality.get('status') != 'ok':
        _issue(source, 'source_unverified', expected=quality.get('expected_statement_as_of'), actual=report.as_of.isoformat())
    if parsed.statement_as_of != report.as_of:
        _issue(source, 'scope_mismatch', field='statement_as_of')
    if len(parsed.source_account_ids) != 1 or parsed.statement_as_of != report.as_of:
        _issue(source, 'ambiguous_account')
        for item in checks[1:]:
            _issue(item, 'scope_unavailable')
        return _result(checks, scope, report, quality)
    account = parsed.source_account_ids[0]
    scope['source_account_id'] = account
    root = ET.fromstring(xml_text)
    expected_groups = _groups(parsed.trades, lambda row: (row.get('source_account_id') or account, _execution(row)))
    stored_groups = _groups(trades, lambda row: (row.get('source_account_id'), _execution(row)))
    by_fact = _groups(facts, lambda row: str(row.get('source_id')))
    by_fact_key = _groups(facts, lambda row: row.get('source_key'))
    covered_ids = set()
    if not any(elem.tag.rsplit('}',1)[-1].lower() in {'trade','trades'} for elem in root.iter()):
        _issue(ledger, 'missing_trade_section')
        _issue(fact_check, 'missing_trade_section')
    for (source_account, execution), rows in expected_groups.items():
        key = str(source_account) + ':' + execution
        if len(rows) != 1:
            _issue(ledger, 'source_duplicate', key=key)
            _issue(fact_check, 'source_duplicate', key=key)
            continue
        expected = {**_official_trade(rows[0]), 'source_account_id': source_account}
        candidates = stored_groups.get((source_account, execution), [])
        if not candidates:
            other_accounts = [r for (_, identity), values in stored_groups.items() if identity == execution for r in values]
            if other_accounts:
                _issue(ledger, 'scope_mismatch', key=key, field='source_account_id')
                _issue(fact_check, 'scope_mismatch', key=key, field='source_account_id')
                continue
        stored = _one(ledger, candidates, key)
        if stored is None:
            _issue(fact_check, 'missing_value', key=key, field='ledger_identity')
            continue
        covered_ids.add(str(stored['id']))
        evidence = {'key': key, 'currency': expected['currency'], 'record_id': str(stored['id']),
                    'source_table': 'pams_ibkr_trades'}
        if execution.startswith('source:'):
            _issue(ledger, 'unstable_trade_identity', **evidence)
        fields = ('account_code','source_account_id','symbol','currency','trade_date','side')
        if not _scope(ledger, expected, stored, fields, **evidence):
            _issue(fact_check, 'scope_mismatch', key=key)
            continue
        if stored.get('source_trade_key') != expected['source_trade_key']:
            _issue(ledger, 'source_link_mismatch', field='source_trade_key', **evidence)
        raw = {k.lower():v for k,v in expected.get('raw',{}).items()}
        fee_comparable = raw.get('ibcommissioncurrency', expected['currency']) == expected['currency']
        amounts = ('quantity','gross_amount','net_cash') + (('commission',) if fee_comparable else ())
        if not fee_comparable:
            _issue(ledger, 'fee_currency_mismatch', **evidence)
            _issue(fact_check, 'fee_currency_mismatch', **evidence)
        _numbers(ledger, expected, stored, amounts, **evidence)
        fact = _fact_match(fact_check, by_fact_key, by_fact, stored, expected, key)
        if fact is None:
            continue
        expected_fact = {**expected, 'side': 'fx_conversion' if classify_trade_type(expected) == 'fx_conversion' else normalize_side(expected),
                         'gross_amount': abs(expected['gross_amount']) if expected['gross_amount'] is not None else None,
                         'fee_amount': abs(expected['commission']) if expected['commission'] is not None else None}
        evidence = {**evidence, 'source_table':'pams_investment_trade_fact', 'record_id':str(fact.get('trade_fact_id', fact.get('id')))}
        if _scope(fact_check, expected_fact, fact, ('account_code','symbol','currency','trade_date','side'), **evidence):
            _numbers(fact_check, expected_fact, fact, ('quantity','gross_amount','net_cash') + (('fee_amount',) if fee_comparable else ()), **evidence)
    scope['ledger_rows_not_covered'] = len(trades) - len(covered_ids)
    scope['execution_dates'] = sorted({row['trade_date'].isoformat() for row in parsed.trades})
    scope['reported_trade_rows'] = len(parsed.trades)
    if trades and scope['ledger_rows_not_covered']:
        _issue(ledger, 'outside_report', actual=scope['ledger_rows_not_covered'])
    positions = [row for row in parsed.rows if not str(row.get('symbol','')).startswith('CASH_')]
    scope['reported_security_rows'] = len(positions)
    scope['cash_rows_excluded'] = len(parsed.rows) - len(positions)
    securities = lambda rows: [row for row in rows if not str(row.get('symbol','')).startswith('CASH_')]
    _position_check(historical, positions, securities(history), account, history=True)
    snapshot_dates = {row.get('as_of') for row in snapshots}
    if snapshot_dates != {report.as_of}:
        _issue(current, 'snapshot_date_mismatch', expected=report.as_of.isoformat(),
               actual=sorted(str(value) for value in snapshot_dates))
    else:
        _position_check(current, positions, securities(snapshots), account, history=False)
    return _result(checks, scope, report, quality)


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
                report, latest = select_report(conn, raw_dir or default_data_dir() / 'raw/ibkr')
                if report is None:
                    item = _check('source','报告依据')
                    _issue(item, 'missing_report')
                    return _result([item], {})
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    tables = ('pams_ibkr_trades','pams_investment_trade_fact','pams_ibkr_position_history','pams_holdings_snapshot')
                    for table in tables:
                        cur.execute('SELECT to_regclass(%s) IS NOT NULL AS present', ('public.' + table,))
                        if not cur.fetchone()['present']:
                            item = _check('source','报告依据')
                            _issue(item, 'missing_table', source_table=table)
                            return _result([item], {}, report, report_quality(report,latest))
                    cur.execute("SELECT * FROM pams_ibkr_trades WHERE account_code = 'ibkr'")
                    trades = [dict(row) for row in cur.fetchall()]
                    cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table = 'pams_ibkr_trades'")
                    facts = [dict(row) for row in cur.fetchall()]
                    cur.execute("SELECT * FROM pams_ibkr_position_history WHERE account_code = 'ibkr' AND as_of = %s", (report.as_of,))
                    history = [dict(row) for row in cur.fetchall()]
                    cur.execute("""SELECT * FROM pams_holdings_snapshot WHERE account_code = 'ibkr'
                        AND as_of = (SELECT MAX(as_of) FROM pams_holdings_snapshot WHERE account_code = 'ibkr')""")
                    snapshots = [dict(row) for row in cur.fetchall()]
                content = report.path.read_bytes()
                if hashlib.sha256(content).hexdigest() != report.sha256:
                    raise ValueError('report changed during read')
                return reconcile_report(report=report, xml_text=content.decode('utf-8'), quality=report_quality(report,latest),
                                        trades=trades, facts=facts, history=history, snapshots=snapshots)
    except Exception as exc:
        item = _check('source','报告依据')
        _issue(item, 'read_failed', status='error', error_type=type(exc).__name__)
        return _result([item], {})
