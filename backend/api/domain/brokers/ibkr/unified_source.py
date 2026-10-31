"""Isolated-only provenance for newly inserted, verified IBKR trade facts."""
from __future__ import annotations

from hashlib import sha256
from datetime import date
import json
import re
from uuid import UUID, uuid5

from domain.imports import isolation
from domain.imports.unified_source import add_item, create_batch, lock_source_review, review_item
from domain.investments.investment_trade_fact import _json_ready

NAMESPACE = UUID('4bbbd902-36d3-4ea9-8b45-ce89f6ff86e6')


def link_new_fact(cur, fact: dict, row: dict, *, source_account_id: str) -> None:
    """Link a fact just inserted by the caller; never migrate an existing fact."""
    if not isolation.source_enabled(cur):
        return
    if cur.connection.autocommit:
        raise ValueError('IBKR source association requires a transaction')
    table = fact.get('source_table')
    key = fact.get('source_key')
    raw = row.get('raw_json')
    evidence = row.get('source_evidence')
    if (fact.get('account_code') != 'ibkr' or not source_account_id or
        table not in {'pams_ibkr_trades', 'ibkr_flex_cash_transaction'} or
        not isinstance(key, str) or not key or not isinstance(raw, dict) or
        not isinstance(evidence, dict) or not re.fullmatch(r'[0-9a-f]{64}', evidence.get('source_sha256', '')) or
        evidence.get('broker_source') not in {'ibkr_flex', 'ibkr_published_daily_close'}):
        raise ValueError('IBKR source requires verified account and report evidence')
    try:
        confirmed_day = date.fromisoformat(evidence['confirmed_day'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('IBKR source requires a valid confirmed day') from exc
    if not isinstance(fact.get('trade_date'), date) or fact['trade_date'] > confirmed_day:
        raise ValueError('IBKR trade date exceeds confirmed report evidence')
    accounts = {str(v).strip() for k, v in raw.items() if k.lower() in {'accountid', 'acctid'} and str(v).strip()}
    if accounts and accounts != {source_account_id}:
        raise ValueError('IBKR raw evidence account mismatch')
    if table == 'pams_ibkr_trades' and evidence.get('evidence_role') != 'confirmed_day_gate':
        raise ValueError('IBKR trade must distinguish day gate from original trade evidence')
    identity = json.dumps([table, key], ensure_ascii=True, separators=(',', ':'))
    batch_id = uuid5(NAMESPACE, 'batch:' + identity)
    item_id = uuid5(NAMESPACE, 'item:' + identity)
    raw_fields = _json_ready({'source_table': table, 'source_key': key,
                              'source_account_id': source_account_id, 'official_attributes': raw,
                              'report_evidence': evidence})
    parsed_fields = _json_ready({'fact': fact})
    encoded = json.dumps(raw_fields, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    create_batch(cur, batch_id=batch_id, account_code='ibkr', business_type='trade',
                 institution='ibkr', acquisition_method='api')
    add_item(cur, item_id=item_id, batch_id=batch_id, source_uid=key, parse_method='api',
             external_id=key, record_hash=sha256(encoded).hexdigest(), raw_fields=raw_fields,
             parsed_fields=parsed_fields)
    cur.execute('SELECT revision FROM public.source_batches WHERE batch_id=%s', (str(batch_id),))
    revision = cur.fetchone()['revision']
    revision = review_item(cur, batch_id=batch_id, item_id=item_id, expected_revision=revision,
                           parsed_fields=parsed_fields, review_status='confirmed',
                           review_note='verified IBKR source evidence')
    locked = lock_source_review(cur, batch_id=batch_id, expected_revision=revision)
    if locked['snapshot']['blockers']:
        raise ValueError('IBKR source review incomplete')
    cur.execute('UPDATE public.pams_investment_trade_fact SET source_item_id=%s '
                'WHERE source_table=%s AND source_key=%s AND source_id=%s AND source_item_id IS NULL '
                'RETURNING trade_id', (str(item_id), table, key, fact['source_id']))
    linked = cur.fetchall()
    if len(linked) != 1:
        raise ValueError('IBKR fact link missing or conflicting')
    from domain.imports.source_import_adapter import _check_all_source_links
    _check_all_source_links(cur, batch_id, [{'table': 'pams_investment_trade_fact',
                                             'id': linked[0]['trade_id'], 'row_id': str(item_id)}])
    cur.execute("UPDATE public.source_batches SET status='published',published_source_hash=%s,updated_at=now() WHERE batch_id=%s",
                (locked['source_snapshot_sha256'], str(batch_id)))
