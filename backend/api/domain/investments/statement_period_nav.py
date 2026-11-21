"""Read balance points backed by an already reconciled, dated bank statement."""
from bisect import bisect_right
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache
import json

from domain.imports.account_import_statement import profiles
from domain.imports.historical_statement_balance import (
    _statement_balance_rows, credit_statement_bridge, read_original_credit_pair, read_original_debit_statement,
)
from domain.imports.source_original_read import SourceOriginals
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


@lru_cache(maxsize=4)
def _balance_curve(method, originals, config_json, currency):
    """Cache parsing only. Original contents, profile and currency identify each entry."""
    cfg = json.loads(config_json)
    if method == 'original_statement_opening':
        statement, tail = read_original_debit_statement(content=originals[0], cfg=cfg, currency=currency, filename='statement.pdf')
        if not statement.statement_start:
            raise ValueError('Missing statement start')
        opening_day = statement.statement_start - timedelta(days=1)
        opening, rows = _statement_balance_rows(statement, target=opening_day, currency=currency, account_tail=tail)
        values = {opening_day: opening}
        for row in rows:
            values[row['transaction_date']] = row['balance']
        end = statement.statement_end
    else:
        (first, previous), (later, later_previous) = read_original_credit_pair(originals=originals, cfg=cfg, currency=currency)
        if not first.statement_start or not first.statement_end or later.statement_balance is None:
            raise ValueError('Missing statement dates or balance')
        if later_previous - sum(row['amount'] for row in later.rows) != later.statement_balance:
            raise ValueError('Unreconciled following credit statement')
        days = {first.statement_start}
        days.update(row['transaction_date'] for report in (first, later) for row in report.rows
                    if first.statement_start <= row['transaction_date'] <= first.statement_end)
        values = {day: credit_statement_bridge(first, later, target=day, currency=currency,
                                               first_previous=previous, following_previous=later_previous)
                  for day in sorted(days)}
        end = first.statement_end
    return tuple(values), tuple(values.values()), end


def _source_bytes(cur, loader, anchor):
    if anchor['method'] == 'original_statement_opening':
        if anchor['asset_liability'] != '资产' or anchor['acquisition_method'] != 'file' or anchor['file_format'] != 'pdf':
            return None
        rows = [anchor]
    else:
        refs = anchor['reconstruction'].get('statement_source_item_ids')
        if anchor['asset_liability'] != '负债' or not isinstance(refs, list) or len(refs) != 2 or refs[0] == refs[1]:
            return None
        cur.execute("""SELECT r.source_id original_source_id, r.sha256, r.content IS NOT NULL inline_original,
                    r.account_code source_account, i.source_item_id FROM source_items i JOIN source_records r USING(source_id)
                    WHERE r.account_code=%s AND r.status='published' AND r.acquisition_method='file' AND r.file_format='pdf'
                    AND r.document_type IN ('cash','statement') AND i.review_status IN ('confirmed','excluded')
                    AND i.source_position->>'row'='statement_balance' AND i.source_item_id::text=ANY(%s::text[])""",
                    (anchor['account_code'], refs))
        by_id = {str(r['source_item_id']): r for r in cur.fetchall()}
        if not all(ref in by_id for ref in refs):
            return None
        rows = [by_id[ref] for ref in refs]
    contents = [loader.read(r)[0] for r in rows]
    return tuple(bytes(content) for content in contents) if all(contents) else None


def load_statement_period_points(cur, accounts, dates):
    """Recover only within report coverage, rechecking the published anchor and original."""
    if not accounts or not dates:
        return {}
    cur.execute("""SELECT h.id,h.account_code,h.as_of,h.asset_liability,h.market_value,h.currency,
            i.parsed_fields->'reconstruction_evidence' AS reconstruction,
            i.parsed_fields->'reconstruction_evidence'->>'method' AS method,r.account_code source_account,
            r.source_id original_source_id,r.sha256,r.content IS NOT NULL inline_original,r.acquisition_method,r.file_format
        FROM pams_holdings_snapshot h JOIN source_items i ON h.source_item_id=i.source_item_id
        JOIN source_records r USING(source_id) WHERE h.account_code=ANY(%s::text[]) AND r.account_code=h.account_code
          AND h.as_of<=%s AND h.major_category='现金' AND r.document_type='holding'
          AND r.status='published' AND i.review_status='confirmed'
          AND i.parsed_fields->'reconstruction_evidence'->>'method' IN ('original_statement_opening','credit_statement_bridge')
        """, (sorted(accounts), max(dates)))
    anchors = cur.fetchall()
    if not anchors:
        return {}
    coverage = load_account_snapshot_coverage(cur, {(r['account_code'], r['as_of']) for r in anchors})
    by_account = profiles()
    loader = SourceOriginals(cur)
    candidates = defaultdict(set)
    for anchor in anchors:
        code, anchor_day, currency = anchor['account_code'], anchor['as_of'], anchor['currency']
        proof = coverage[code, anchor_day]
        configs = by_account.get(code, [])
        if (anchor['id'] not in proof['member_ids'] or anchor['id'] not in proof.get('point_in_time_member_ids', ())
                or len(configs) != 1 or configs[0].get('currency') != currency):
            continue
        contents = _source_bytes(cur, loader, anchor)
        if not contents:
            continue
        try:
            days, values, end = _balance_curve(anchor['method'], contents, json.dumps(configs[0], sort_keys=True, default=str), currency)
        except (ValueError, ArithmeticError, TypeError):
            continue
        if not days[0] <= anchor_day <= end or values[bisect_right(days, anchor_day)-1] != anchor['market_value']:
            continue
        for day in dates:
            if days[0] <= day <= end:
                candidates[code, day].add((currency, anchor['asset_liability'], values[bisect_right(days, day)-1]))
    return {key: next(iter(matches)) for key, matches in candidates.items() if len(matches) == 1}
