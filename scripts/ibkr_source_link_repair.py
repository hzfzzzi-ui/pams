"""Approval-bound source-link rehearsal. Production execution is NOT supported."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, parse_dsn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/api'))
from services.ibkr_lineage_rehearsal import fingerprint, plan_rehearsal  # noqa: E402
from services.ibkr_report_state import report_quality, select_report  # noqa: E402

PURPOSE = 'isolated_source_link_rehearsal'
JOURNAL = 'public.pams_ibkr_source_link_repair_audit'
MARKER = 'pams-isolated-lineage-rehearsal'


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _idle(conn) -> None:
    if conn.autocommit or conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        raise ValueError('Fresh non-autocommit connection required')


def _scope(cur) -> dict:
    cur.execute("""SELECT current_database() AS database, session_user AS operator,
        inet_server_addr()::text AS server, inet_server_port() AS port""")
    return dict(cur.fetchone())


def _inputs(conn, cur, raw_dir: Path) -> dict:
    report, latest = select_report(conn, raw_dir)
    if report is None:
        raise ValueError('No published report')
    content = report.path.read_bytes()
    if hashlib.sha256(content).hexdigest() != report.sha256:
        raise ValueError('Published report changed')
    cur.execute('SELECT * FROM public.pams_ibkr_trades')
    trades = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM public.pams_investment_trade_fact WHERE source_table='pams_ibkr_trades'")
    return {'report': report, 'xml_text': content.decode('utf-8'), 'quality': report_quality(report, latest),
            'trades': trades, 'facts': [dict(row) for row in cur.fetchall()]}


def _request(inputs: dict, scope: dict) -> dict:
    plan = plan_rehearsal(**inputs)
    evidence = plan.get('audit_evidence', {})
    if (plan['status'] != 'ready_for_isolated_rehearsal' or not plan['mappings']
            or not evidence.get('complete_scope') or evidence.get('status') != 'review'
            or evidence.get('unresolved_evidence')):
        raise ValueError('Incomplete or blocked rehearsal evidence')
    payload = {'version': 1, 'purpose': PURPOSE, 'production_write_supported': False,
        'database_scope': scope, 'report': {'sha256': inputs['report'].sha256,
            'as_of': inputs['report'].as_of.isoformat(), 'run_id': inputs['report'].run_id},
        'plan_sha256': plan['plan_sha256'], 'source_sha256': plan['source_rows_sha256'],
        'before_sha256': plan['fact_rows_sha256'], 'after_sha256': plan['projected_fact_rows_sha256'],
        'protected_sha256': plan['protected_facts_sha256'], 'mappings': plan['mappings'],
        'components': plan['components'], 'counts': plan['counts'],
        'risks': sorted(set(evidence['remaining_requirements']) - {'user_approval_required'}
                        | {'historical_coverage_not_verified_by_this_tool'})}
    return {**payload, 'request_sha256': fingerprint([payload])}


def precheck(conn, raw_dir: Path) -> dict:
    """All queries, including report selection, are server-enforced READ ONLY."""
    _idle(conn)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            request = _request(_inputs(conn, cur, raw_dir), _scope(cur))
            cur.execute('SELECT to_regclass(%s) IS NOT NULL AS present', (JOURNAL,))
            return {'status': 'approval_required', 'read_only': True, 'executable': False,
                    'production_write_supported': False, 'audit_schema_present': cur.fetchone()['present'],
                    'request': request}
    finally:
        conn.rollback()


def validate_approval(request: dict, approval: dict) -> str:
    """An attestation file is not cryptographic authentication or production permission."""
    if set(approval) != {'purpose', 'request_sha256', 'approved_by', 'reference',
                         'issued_at', 'expires_at', 'accepted_risks'}:
        raise ValueError('Invalid approval fields')
    if approval['purpose'] != PURPOSE or approval['request_sha256'] != request.get('request_sha256'):
        raise ValueError('Approval purpose or request mismatch')
    for key in ('approved_by', 'reference'):
        if not isinstance(approval[key], str) or not 1 <= len(approval[key].strip()) <= 500:
            raise ValueError('Approval attribution required')
    if approval['accepted_risks'] != request.get('risks'):
        raise ValueError('All residual risks must be explicitly acknowledged')
    issued, expires = (datetime.fromisoformat(approval[key]) for key in ('issued_at', 'expires_at'))
    now = utc_now()
    if (issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires
            or not timedelta(0) < expires - issued <= timedelta(hours=24)):
        raise ValueError('Approval expired or outside bounded validity')
    payload = {key: value for key, value in request.items() if key != 'request_sha256'}
    if (request.get('purpose') != PURPOSE or request.get('production_write_supported') is not False
            or fingerprint([payload]) != request.get('request_sha256')):
        raise ValueError('Unverified request')
    return fingerprint([approval])


def _isolated(conn) -> None:
    """Reject normal databases before opening any read-write transaction."""
    _idle(conn)
    dsn = os.environ.get('PAMS_TEST_POSTGRES_DSN')
    expected = parse_dsn(dsn) if dsn else {}
    actual = conn.info.dsn_parameters
    if (not expected.get('host') or not expected.get('user')
            or any(actual.get(key) != expected.get(key, default)
                   for key, default in (('host', ''), ('port', '5432'), ('user', '')))
            or not re.fullmatch(r'pams_ibkr_tx_test_[0-9a-f]{32}', actual['dbname'])):
        raise ValueError('Explicit disposable test server and database required')
    try:
        with conn.cursor() as cur:
            cur.execute('BEGIN READ ONLY')
            cur.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
            if cur.fetchone()[0] != MARKER:
                raise ValueError('Isolated database marker required')
    finally:
        conn.rollback()


def _audit(cur, request: dict, approval: dict, approval_sha256: str) -> None:
    cur.execute(f"""INSERT INTO {JOURNAL}
        (request_sha256, approval_sha256, purpose, database_scope, request_json, approval_json, rows_affected)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (request['request_sha256'], approval_sha256, PURPOSE, Json(request['database_scope']),
         Json(request), Json(approval), len(request['mappings'])))


def rehearse(conn, raw_dir: Path, request: dict, approval: dict) -> dict:
    """Atomically update ONLY source_id in a marked disposable DB, never production."""
    approval_sha256 = validate_approval(request, approval)
    _isolated(conn)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            cur.execute("SET LOCAL lock_timeout = '2000ms'")
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            # Serialize both writers and publication while re-deriving the full frozen plan.
            for table, mode in (('pams_investment_trade_fact', 'SHARE ROW EXCLUSIVE'),
                                ('pams_ibkr_trades', 'SHARE'), ('pams_data_sync_runs', 'SHARE'),
                                ('pams_ibkr_source_link_repair_audit', 'SHARE ROW EXCLUSIVE')):
                cur.execute(f'LOCK TABLE public.{table} IN {mode} MODE')
            inputs = _inputs(conn, cur, raw_dir)
            current = fingerprint(inputs['facts'])
            if current not in (request['before_sha256'], request['after_sha256']):
                raise ValueError('Fact drift or partially applied batch')
            if current == request['after_sha256']:
                inputs['facts'] = deepcopy(inputs['facts'])
                by_id = {str(row['trade_id']): row for row in inputs['facts']}
                for mapping in request['mappings']:
                    by_id[mapping['fact_id']]['source_id'] = mapping['before']
                if fingerprint(inputs['facts']) != request['before_sha256']:
                    raise ValueError('Cannot verify original batch')
            expected = _request(inputs, _scope(cur))
            if fingerprint([request]) != fingerprint([expected]):
                raise ValueError('Request differs from current published evidence')
            validate_approval(request, approval)
            cur.execute(f'SELECT * FROM {JOURNAL} WHERE request_sha256=%s', (request['request_sha256'],))
            entry = cur.fetchone()
            if entry:
                if (current != request['after_sha256'] or entry['purpose'] != PURPOSE
                        or entry['approval_sha256'] != approval_sha256
                        or entry['database_scope'] != request['database_scope']
                        or fingerprint([entry['request_json']]) != fingerprint([request])
                        or fingerprint([entry['approval_json']]) != approval_sha256
                        or entry['rows_affected'] != len(request['mappings'])):
                    raise ValueError('Existing audit or applied state differs')
                return {'status': 'already_applied_in_isolation', 'updated': 0,
                        'request_sha256': request['request_sha256'], 'production_write_supported': False}
            if current != request['before_sha256']:
                raise ValueError('Applied links without matching audit')
            token = uuid4().hex
            for target in ('stage', 'final'):
                for mapping in request['mappings']:
                    temporary = f'rehearsal:{token}:{mapping["fact_id"]}'
                    old, new = ((mapping['before'], temporary) if target == 'stage'
                                else (temporary, mapping['after']))
                    cur.execute("""UPDATE public.pams_investment_trade_fact SET source_id=%s
                        WHERE trade_id=%s AND source_table='pams_ibkr_trades' AND source_id=%s""",
                        (new, mapping['fact_id'], old))
                    if cur.rowcount != 1:
                        raise ValueError('Batch row count changed')
            cur.execute("SELECT * FROM public.pams_investment_trade_fact WHERE source_table='pams_ibkr_trades'")
            if fingerprint([dict(row) for row in cur.fetchall()]) != request['after_sha256']:
                raise ValueError('Unexpected field change')
            validate_approval(request, approval)
            _audit(cur, request, approval, approval_sha256)
        conn.commit()
        return {'status': 'applied_in_isolation', 'updated': len(request['mappings']),
                'request_sha256': request['request_sha256'], 'production_write_supported': False}
    finally:
        # Also handles commit/acknowledgement failures; a retry requires the journal and full state.
        conn.rollback()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir', required=True, type=Path)
    parser.add_argument('--rehearse', action='store_true', help='Marked disposable databases ONLY')
    parser.add_argument('--request', type=Path)
    parser.add_argument('--approval', type=Path)
    args = parser.parse_args(argv)
    if args.rehearse != bool(args.request and args.approval) or (
            not args.rehearse and (args.request or args.approval)):
        parser.error('--rehearse requires both --request and --approval; default mode accepts neither')
    conn = None
    try:
        dsn = os.environ.get('PAMS_REPAIR_DSN')
        if not dsn:
            raise ValueError('Explicit PAMS_REPAIR_DSN required; PG* fallback forbidden')
        conn = psycopg2.connect(dsn, connect_timeout=5, application_name='pams-source-link-precheck')
        if args.rehearse:
            request = json.loads(args.request.read_text(encoding='utf-8'))
            approval = json.loads(args.approval.read_text(encoding='utf-8'))
            result = rehearse(conn, args.raw_dir, request, approval)
        else:
            result = precheck(conn, args.raw_dir)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__,
                          'production_write_supported': False, 'committed': 'unverified'}))
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
