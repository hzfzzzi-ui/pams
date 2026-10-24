"""Scratch-only SQL experiment. Deliberately outside the API image and entrypoints."""
import os
import re
import hashlib
from uuid import uuid4

import psycopg2.extras
from psycopg2.extensions import parse_dsn

from services.ibkr_lineage_rehearsal import fingerprint, protected_facts_fingerprint


def apply_in_scratch(conn, plan: dict, *, xml_text: str, interrupt_at: str | None = None) -> dict:
    """This test helper cannot connect; caller must supply a marked disposable DB."""
    expected = parse_dsn(os.environ.get('PAMS_TEST_POSTGRES_DSN', ''))
    actual = conn.info.dsn_parameters
    if not expected or any(actual.get(k) != str(expected.get(k, v))
                           for k, v in (('host',''),('port','5432'),('user',''))):
        raise ValueError('Explicit isolated test DSN required')
    if not re.fullmatch(r'pams_ibkr_tx_test_[0-9a-f]{32}', actual['dbname']):
        raise ValueError('Disposable database name required')
    if hashlib.sha256(xml_text.encode('utf-8')).hexdigest() != plan.get('report_sha256'):
        raise ValueError('Frozen report changed')
    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=current_database()")
            if cur.fetchone()['marker'] != 'pams-isolated-lineage-rehearsal':
                raise ValueError('Isolated database marker required')
            cur.execute("SET LOCAL lock_timeout = '2000ms'")
            cur.execute('LOCK TABLE pams_investment_trade_fact IN SHARE ROW EXCLUSIVE MODE')
            cur.execute('LOCK TABLE pams_ibkr_trades IN SHARE MODE')
            cur.execute('SELECT * FROM pams_ibkr_trades')
            sources = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='pams_ibkr_trades'")
            facts = [dict(row) for row in cur.fetchall()]
            if plan.get('status') != 'ready_for_isolated_rehearsal' or plan.get('executable') is not False:
                raise ValueError('Unverified plan')
            signed = {k: v for k, v in plan.items() if k != 'plan_sha256'}
            if fingerprint([signed]) != plan.get('plan_sha256'):
                raise ValueError('Plan changed')
            if (fingerprint(sources) != plan['source_rows_sha256']
                    or protected_facts_fingerprint(facts) != plan['protected_facts_sha256']):
                raise ValueError('Frozen source or protected facts changed')
            fact_keys = {str(row['trade_id']): row['source_key'] for row in facts}
            targets: dict[str, list[str]] = {}
            for source in sources:
                key = str(source['account_code']) + ':' + str(source['source_trade_key'])
                targets.setdefault(key, []).append(str(source['id']))
            for row in plan['mappings']:
                if (fact_keys.get(row['fact_id']) != row['source_key']
                        or targets.get(row['source_key']) != [row['after']]):
                    raise ValueError('Mapping differs from stable source identity')
            current = fingerprint(facts)
            if current == plan['projected_fact_rows_sha256']:
                return {'updated': 0, 'already_applied': True}
            if current != plan['fact_rows_sha256']:
                raise ValueError('Frozen fact links changed')
            token = uuid4().hex
            for row in plan['mappings']:
                cur.execute("UPDATE pams_investment_trade_fact SET source_id=%s WHERE trade_id=%s AND source_table='pams_ibkr_trades' AND source_id=%s",
                            (f'rehearsal:{token}:{row["fact_id"]}', row['fact_id'], row['before']))
                if cur.rowcount != 1:
                    raise ValueError('Stage row count changed')
            if interrupt_at == 'after_stage':
                raise RuntimeError('Injected interruption')
            for index, row in enumerate(plan['mappings']):
                cur.execute("UPDATE pams_investment_trade_fact SET source_id=%s WHERE trade_id=%s AND source_table='pams_ibkr_trades' AND source_id=%s",
                            (row['after'], row['fact_id'], f'rehearsal:{token}:{row["fact_id"]}'))
                if cur.rowcount != 1:
                    raise ValueError('Final row count changed')
                if index == 0 and interrupt_at == 'after_first_final':
                    raise RuntimeError('Injected interruption')
            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='pams_ibkr_trades'")
            changed = [dict(row) for row in cur.fetchall()]
            if fingerprint(changed) != plan['projected_fact_rows_sha256']:
                raise ValueError('Unexpected row or field change')
            if interrupt_at == 'before_commit':
                raise RuntimeError('Injected interruption')
    return {'updated': len(plan['mappings']), 'already_applied': False}
