"""Offline source-link planning. No connections, SQL, executor or API registration."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any

from services.ibkr_fee_review import review_fees
from services.ibkr_lineage_preflight import SOURCE_TABLE, preflight_lineage
from services.ibkr_report_state import PublishedReport


def _canonical(value: Any):
    if isinstance(value, datetime):
        text = value.isoformat()
        if value.microsecond:
            fraction = f'.{value.microsecond:06d}'
            text = text.replace(fraction, fraction.rstrip('0'), 1)
        return ['text', text]
    if isinstance(value, date):
        return ['text', value.isoformat()]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('Nonfinite fingerprint input')
        text = format(number, 'f')
        return ['number', (text.rstrip('0').rstrip('.') if '.' in text else text) if number else '0']
    if isinstance(value, dict):
        return ['object', [[key, _canonical(value[key])] for key in sorted(value)]]
    if isinstance(value, (list, tuple)):
        return ['array', [_canonical(item) for item in value]]
    if value is None or isinstance(value, (bool, str)):
        return [type(value).__name__, value] if not isinstance(value, str) else ['text', value]
    raise TypeError(type(value).__name__)


def fingerprint(rows: list[dict]) -> str:
    """Order-independent, whole-row evidence; duplicates are not discarded."""
    encoded = sorted(json.dumps(_canonical(row), sort_keys=True, ensure_ascii=True) for row in rows)
    return hashlib.sha256(json.dumps(encoded, ensure_ascii=True).encode()).hexdigest()


def protected_facts_fingerprint(facts: list[dict]) -> str:
    return fingerprint([{k: v for k, v in row.items() if k != 'source_id'} for row in facts])


def _components(mappings: list[dict]) -> list[dict]:
    by_id = {row['fact_id']: row for row in mappings}
    graph: dict[str, set[str]] = {key: set() for key in by_id}
    for row in mappings:
        for occupant in row['target_occupants']:
            if occupant in graph:
                graph[row['fact_id']].add(occupant)
                graph[occupant].add(row['fact_id'])
    result = []
    remaining = set(graph)
    while remaining:
        pending, group = [min(remaining)], set()
        while pending:
            key = pending.pop()
            if key not in group:
                group.add(key)
                pending.extend(graph[key] - group)
        remaining -= group
        free = sum(not by_id[key]['target_occupants'] for key in group)
        result.append({'fact_ids': sorted(group), 'size': len(group), 'free_targets': free,
                       'kind': 'cycle' if not free else 'free_target' if len(group) == 1 else 'chain'})
    return result


def plan_rehearsal(*, report: PublishedReport, xml_text: str, quality: dict,
                   trades: list[dict], facts: list[dict]) -> dict:
    """Return a bounded isolated-experiment plan, never production repair approval."""
    result: dict[str, Any] = {'status': 'blocked', 'simulation_only': True, 'executable': False,
        'production_write_supported': False, 'production_repair_ready': False,
        'mappings': [], 'components': [], 'blockers': []}
    if (hashlib.sha256(xml_text.encode('utf-8')).hexdigest() != report.sha256
            or quality.get('statement_as_of') != report.as_of.isoformat()
            or quality.get('expected_statement_as_of') != report.as_of.isoformat()):
        return {**result, 'blockers': ['report_identity_or_date_mismatch']}
    before = preflight_lineage(report=report, xml_text=xml_text, quality=quality,
                               trades=trades, facts=facts, limit=500)
    if before['has_more'] or before['global_blockers']:
        return {**result, 'blockers': ['incomplete_or_unverified_scope']}
    if not before['records'] or len(before['records']) != len(trades):
        return {**result, 'blockers': ['incomplete_or_empty_scope']}
    fees = review_fees(xml_text=xml_text, quality=quality, trades=trades, facts=facts)
    fee_rows = {row['source_key']: row for row in fees.get('records', [])}
    if fees.get('total') is None or fees.get('truncated'):
        return {**result, 'blockers': ['incomplete_fee_evidence']}
    if any(row.get('fee_unit_evidence') != 'raw_and_embedded_only_no_fact_fee_currency_column'
           or not row.get('checks') or any(check['status'] != 'matched' for check in row['checks'].values())
           for row in fee_rows.values()):
        return {**result, 'blockers': ['fee_evidence_not_matched']}
    mappings = []
    for row in before['records']:
        reasons = {item['reason'] for item in row['blockers']}
        # These remain production blockers; only a scratch-database experiment may proceed.
        if reasons - {'target_id_in_use', 'fee_currency_unverified'}:
            result['blockers'].append({'fact_id': row['fact_id'], 'reasons': sorted(reasons)})
        if 'fee_currency_unverified' in reasons:
            evidence = fee_rows.get(row['source_key'], {})
            if (evidence.get('fee_unit_evidence') != 'raw_and_embedded_only_no_fact_fee_currency_column'
                    or not evidence.get('checks')
                    or any(check['status'] != 'matched' for check in evidence['checks'].values())):
                result['blockers'].append({'fact_id': row['fact_id'], 'reason': 'fee_evidence_not_matched'})
        if row['link_mismatch'] and row['proposal']:
            occupants = [key for block in row['blockers'] if block['reason'] == 'target_id_in_use'
                         for key in block['conflicting_fact_ids']]
            mappings.append({'fact_id': row['fact_id'], 'source_key': row['source_key'],
                'before': row['current_source_id'], 'after': row['target_source_id'],
                'target_occupants': sorted(occupants), 'retained_blockers': sorted(reasons)})
    batch = {row['fact_id'] for row in mappings}
    if len(batch) != len(mappings) or len({row['after'] for row in mappings}) != len(mappings):
        result['blockers'].append('duplicate_batch_identity_or_target')
    if any(occupant not in batch for row in mappings for occupant in row['target_occupants']):
        result['blockers'].append('target_occupied_outside_batch')
    result.update(mappings=mappings, components=_components(mappings),
        report_sha256=report.sha256, source_rows_sha256=fingerprint(trades),
        fact_rows_sha256=fingerprint(facts), protected_facts_sha256=protected_facts_fingerprint(facts),
        before_counts=before['counts'], retained_before_reasons=before['reason_counts'],
        counts={'mismatches': len(mappings), 'occupied_targets': sum(bool(r['target_occupants']) for r in mappings),
                'free_targets': sum(not r['target_occupants'] for r in mappings)})
    if result['blockers']:
        return result
    projected = deepcopy(facts)
    by_id = {str(row['trade_id']): row for row in projected if row.get('source_table') == SOURCE_TABLE}
    for row in mappings:
        by_id[row['fact_id']]['source_id'] = row['after']
    assert protected_facts_fingerprint(projected) == result['protected_facts_sha256']
    after = preflight_lineage(report=report, xml_text=xml_text, quality=quality,
                              trades=trades, facts=projected, limit=500)
    result.update(status='ready_for_isolated_rehearsal', projected_fact_rows_sha256=fingerprint(projected),
                  projected_after_counts=after['counts'], projected_after_reasons=after['reason_counts'],
                  projected_history_id_mismatches=after['reason_counts'].get('embedded_evidence_mismatch', 0),
                  note='Preserve historical raw_json IDs; projected audit blockers are not suppressed.')
    result['plan_sha256'] = fingerprint([result])
    return result


def safe_plan_rehearsal(**kwargs) -> dict:
    try:
        return plan_rehearsal(**kwargs)
    except Exception as exc:
        return {'status': 'unavailable', 'simulation_only': True, 'executable': False,
                'production_write_supported': False, 'production_repair_ready': False,
                'error_type': type(exc).__name__, 'mappings': []}
