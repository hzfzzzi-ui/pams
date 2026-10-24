"""Trace source identities and propose metadata only. No repair executor exists here."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
from typing import Any

import psycopg2.extras

from collectors.ibkr_flex import default_data_dir, parse_flex_report
from services.cashflow_fact import make_source_hash
from services.ibkr_performance import get_conn
from services.ibkr_reconciliation import TOLERANCE, _execution, _official_trade
from services.ibkr_report_state import PublishedReport, report_quality, select_report
from services.investment_trade_fact import classify_trade_type, normalize_side

SOURCE_TABLE = 'pams_ibkr_trades'
REASONS = {
    'report_unverified': '报告过期、沿用旧值或未核准，不能批准修复',
    'ambiguous_account': '报告来源账户不唯一或缺失',
    'outside_report': '稳定源键不在当前发布报告的执行记录内',
    'duplicate_identity': '原文、来源或事实身份不唯一',
    'missing_source': '稳定源键未找到当前来源行',
    'unstable_execution': '缺少可核对的官方执行编号',
    'scope_mismatch': '账户、日期、标的、币种或方向不一致',
    'missing_value': '必需数值缺失，未补零',
    'invalid_value': '数值无效或非有限值',
    'value_mismatch': '金额、价格或份额不变量不一致',
    'embedded_evidence_mismatch': '事实内嵌的历史来源证据缺失或不一致',
    'source_hash_mismatch': '事实来源哈希与既有稳定键规则不一致',
    'fee_currency_unverified': '手续费币种不同，未换汇或核准费用',
    'target_id_in_use': '目标来源 ID 已被其他事实占用，禁止逐条修复',
    'missing_fact_id': '缺少唯一事实记录 ID',
}


def _text(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value) if value is not None else None


def _group(rows, key):
    groups: dict[Any, list] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def _block(item: dict, reason: str, **detail) -> None:
    item['blockers'].append({'reason': reason, 'label': REASONS[reason], **detail})


def _scope(item: dict, expected: dict, actual: dict, fields: tuple[str, ...], stage: str) -> None:
    for field in fields:
        left, right = _text(expected.get(field)), _text(actual.get(field))
        if field in {'side', 'currency', 'symbol'} and left and right:
            left, right = left.upper(), right.upper()
        if not left or not right or left != right:
            _block(item, 'scope_mismatch', stage=stage, field=field, expected=left, actual=right)


def _values(item: dict, expected: dict, actual: dict, fields: tuple[str, ...], stage: str) -> None:
    for field in fields:
        left, right = expected.get(field), actual.get(field)
        detail = {'stage': stage, 'field': field, 'expected': _text(left), 'actual': _text(right),
                  'tolerance': str(TOLERANCE)}
        check = {**detail, 'equal': False}
        item['invariants'].append(check)
        if left is None or right is None:
            _block(item, 'missing_value', **detail)
            continue
        try:
            a, b = Decimal(str(left)), Decimal(str(right))
            if not a.is_finite() or not b.is_finite():
                raise ValueError('nonfinite')
            delta = b - a
        except (InvalidOperation, ValueError):
            _block(item, 'invalid_value', **detail)
            continue
        check.update(delta=str(delta), equal=abs(delta) <= TOLERANCE)
        if not check['equal']:
            _block(item, 'value_mismatch', delta=str(delta), **detail)


def _trace(item: dict, official: dict, source: dict, fact: dict, source_account: str) -> None:
    expected = {**_official_trade(official), 'source_account_id': source_account}
    _scope(item, expected, source, ('account_code','source_account_id','trade_date','symbol','currency','side'), 'source')
    normal = {**expected, 'side': 'fx_conversion' if classify_trade_type(expected) == 'fx_conversion' else normalize_side(expected)}
    _scope(item, normal, fact, ('account_code','trade_date','symbol','currency','side'), 'fact')
    _values(item, expected, source, ('quantity','price','gross_amount','net_cash'), 'source')
    # Preserve the existing writer's absolute gross/fee convention, never convert currencies.
    normal['gross_amount'] = abs(Decimal(str(expected['gross_amount']))) if expected['gross_amount'] is not None else None
    normal['fee_amount'] = abs(Decimal(str(expected['commission']))) if expected['commission'] is not None else None
    _values(item, normal, fact, ('quantity','price','gross_amount','net_cash'), 'fact')
    raw = {k.lower():v for k,v in expected.get('raw',{}).items()}
    if str(raw.get('ibcommissioncurrency', expected.get('currency'))).upper() != expected.get('currency'):
        _block(item, 'fee_currency_unverified')
    else:
        _values(item, expected, source, ('commission',), 'source')
        _values(item, normal, fact, ('fee_amount',), 'fact')
    container = fact.get('raw_json')
    embedded = container.get('source_row') if isinstance(container, dict) else None
    if not isinstance(embedded, dict):
        _block(item, 'embedded_evidence_mismatch', field='source_row')
        return
    for field in ('source_table','source_id','source_key'):
        if _text(embedded.get(field)) != _text(fact.get(field)) or embedded.get(field) is None:
            _block(item, 'embedded_evidence_mismatch', field=field)
    _scope(item, fact, embedded, ('account_code','trade_date','symbol','currency'), 'embedded')
    embedded_side = 'fx_conversion' if classify_trade_type(embedded) == 'fx_conversion' else normalize_side(embedded)
    _scope(item, fact, {'side':embedded_side}, ('side',), 'embedded')
    _values(item, fact, embedded, ('quantity','price','gross_amount','fee_amount','net_cash','tax_amount'), 'embedded')
    old_raw = embedded.get('raw_json')
    raw_identity = old_raw if isinstance(old_raw, dict) else {}
    old_execution = raw_identity.get('tradeID') if official.get('trade_id') else raw_identity.get('transactionID')
    official_execution = official.get('trade_id') or official.get('transaction_id')
    if not old_execution or str(old_execution) != str(official_execution):
        _block(item, 'embedded_evidence_mismatch', field='execution_id')
    if raw_identity.get('accountId') != source_account:
        _block(item, 'embedded_evidence_mismatch', field='source_account_id')
    item['history'] = {'fact_created_at':_text(fact.get('created_at')), 'source_created_at':_text(source.get('created_at')),
                       'embedded_source_id':_text(embedded.get('source_id')), 'embedded_execution_id':_text(old_execution),
                       'conclusion':'历史内嵌证据可用于追溯，但不证明何时或由谁重建来源行'}


def preflight_lineage(*, report: PublishedReport, xml_text: str, quality: dict,
                      trades: list[dict], facts: list[dict], offset: int = 0, limit: int = 100) -> dict:
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError('Invalid preflight page')
    parsed = parse_flex_report(xml_text)
    source_account = parsed.source_account_ids[0] if len(parsed.source_account_ids) == 1 else None
    expected = _group(parsed.trades, lambda r: 'ibkr:' + str(r['source_trade_key']))
    official_ids = _group(parsed.trades, lambda r: (r.get('source_account_id'), _execution(r)))
    by_key = _group(trades, lambda r: str(r.get('account_code')) + ':' + str(r.get('source_trade_key')))
    source_ids = _group(trades, lambda r: (r.get('source_account_id'), _execution(r)))
    physical = _group(trades, lambda r: _text(r.get('id')))
    facts = [r for r in facts if r.get('source_table') == SOURCE_TABLE]
    fact_keys = _group(facts, lambda r: r.get('source_key'))
    references = _group(facts, lambda r: _text(r.get('source_id')))
    fact_ids = _group(facts, lambda r: _text(r.get('trade_id')))
    records = []
    verified = not report.legacy and quality.get('status') == 'ok' and not quality.get('legacy_unverified')
    verified = verified and parsed.statement_as_of == report.as_of
    global_blockers = [reason for reason, blocked in
                       (('report_unverified',not verified),('ambiguous_account',not source_account)) if blocked]
    for fact in sorted(facts, key=lambda r: (str(r.get('source_key')), str(r.get('trade_id')))):
        key = fact.get('source_key')
        old_id = _text(fact.get('source_id'))
        item: dict[str, Any] = {'fact_id':_text(fact.get('trade_id')), 'source_key':key,
              'account_code':fact.get('account_code'), 'trade_date':_text(fact.get('trade_date')),
              'symbol':fact.get('symbol'), 'currency':fact.get('currency'),
              'current_source_id':old_id, 'target_source_id':None, 'link_mismatch':False,
              'proposal':None, 'invariants':[], 'blockers':[], 'executable':False}
        if not verified:
            _block(item, 'report_unverified')
        if not source_account:
            _block(item, 'ambiguous_account')
        if not item['fact_id'] or len(fact_ids[item['fact_id']]) != 1:
            _block(item, 'missing_fact_id')
        raw_rows, sources = expected.get(key, []), by_key.get(key, [])
        if not raw_rows:
            _block(item, 'outside_report')
        if not sources:
            _block(item, 'missing_source')
        if len(raw_rows) > 1 or len(sources) > 1 or len(fact_keys[key]) != 1:
            _block(item, 'duplicate_identity')
        if len(raw_rows) == len(sources) == len(fact_keys[key]) == 1:
            official, source = raw_rows[0], sources[0]
            target = _text(source.get('id'))
            item.update(target_source_id=target, link_mismatch=old_id != target,
                        execution_id=_execution(official), source_account_id=source_account)
            identity = (official.get('source_account_id'), _execution(official))
            if len(official_ids[identity]) != 1 or len(source_ids[identity]) != 1 or len(physical[target]) != 1:
                _block(item, 'duplicate_identity')
            if not official.get('trade_id') and not official.get('transaction_id'):
                _block(item, 'unstable_execution')
            if not target or not old_id:
                _block(item, 'missing_source')
            if source_account:
                _trace(item, official, source, fact, source_account)
            if fact.get('source_hash') != make_source_hash(SOURCE_TABLE, {'source_key':key, 'source_id':old_id}):
                _block(item, 'source_hash_mismatch')
            occupants = [r for r in references.get(target, []) if r is not fact]
            if occupants:
                _block(item, 'target_id_in_use', conflicting_fact_ids=[_text(r.get('trade_id')) for r in occupants])
            old_sources = physical.get(old_id, [])
            item['old_reference'] = {'state': 'absent' if not old_sources else
                'same_execution' if len(old_sources) == 1 and old_sources[0] is source else 'other_or_ambiguous_execution',
                'source_keys':[str(r.get('account_code'))+':'+str(r.get('source_trade_key')) for r in old_sources]}
            if item['link_mismatch'] and old_id and target:
                item['proposal'] = {'field':'source_id', 'before':old_id, 'after':target,
                                    'source_hash_unchanged':True, 'requires_user_approval':True}
        item['status'] = 'blocked' if item['blockers'] else 'candidate' if item['link_mismatch'] else 'unchanged'
        records.append(item)
    counts = Counter(row['status'] for row in records)
    reasons = Counter(b['reason'] for row in records for b in row['blockers'])
    return {'read_only':True, 'dry_run':True, 'executable':False, 'repair_implemented':False,
            'status':'blocked' if counts['blocked'] or global_blockers else 'review' if counts['candidate'] else 'ok',
            'global_blockers':[{'reason':reason,'label':REASONS[reason]} for reason in global_blockers],
            'report':{'run_id':report.run_id,'sha256':report.sha256,'statement_as_of':report.as_of.isoformat(),'quality':quality},
            'scope':{'account_code':'ibkr','source_account_id':source_account,'source_table':SOURCE_TABLE,
                     'not_modified':'所有金额、份额、NAV、收益率、FX、费用币种、source_hash 和历史 raw_json',
                     'causation':'旧 ID 失配是可验证现象；具体重建操作和责任人缺少操作日志，尚未证实'},
            'counts':{'facts':len(records),'link_mismatches':sum(r['link_mismatch'] for r in records),
                      **{s:counts[s] for s in ('candidate','blocked','unchanged')}},
            'reason_counts':dict(sorted(reasons.items())), 'total':len(records), 'offset':offset, 'limit':limit,
            'has_more':offset+limit < len(records), 'records':records[offset:offset+limit]}


def read_lineage_preflight(*, connection_factory=None, raw_dir: Path | None = None, offset: int = 0, limit: int = 100) -> dict:
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError('Invalid preflight page')
    try:
        with closing((connection_factory or get_conn)()) as conn:
            conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '5000ms'")
                report, latest = select_report(conn, raw_dir or default_data_dir()/'raw/ibkr')
                if report is None:
                    return _unavailable('missing_report')
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    for table in (SOURCE_TABLE,'pams_investment_trade_fact'):
                        cur.execute('SELECT to_regclass(%s) IS NOT NULL AS present', ('public.'+table,))
                        if not cur.fetchone()['present']:
                            return _unavailable('missing_table')
                    cur.execute('SELECT * FROM pams_ibkr_trades')
                    trades = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table = 'pams_ibkr_trades'")
                    facts = [dict(r) for r in cur.fetchall()]
                content = report.path.read_bytes()
                if hashlib.sha256(content).hexdigest() != report.sha256:
                    raise ValueError('Report changed during read')
                return preflight_lineage(report=report, xml_text=content.decode('utf-8'), quality=report_quality(report,latest),
                                         trades=trades, facts=facts, offset=offset, limit=limit)
    except Exception as exc:
        return _unavailable('read_failed', type(exc).__name__)


def _unavailable(reason: str, error_type: str | None = None) -> dict:
    return {'status':'unavailable','read_only':True,'dry_run':True,'executable':False,'repair_implemented':False,
            'reason':reason,'error_type':error_type,'records':[],'counts':None,'total':None}
