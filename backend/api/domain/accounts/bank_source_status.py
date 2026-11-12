"""Account sync views over retained bank source evidence."""

from collections import defaultdict
from uuid import uuid4

from domain.imports.bank_source_confirmation import SOURCE_ONLY_ACCOUNTS
from domain.imports.source_records_v2 import add_item, create_source


BANK_DUPLICATE_REVIEW_SOURCE = 'bank_duplicate_review'

_UNPOSTED_MANUAL = """
    FROM public.source_records r JOIN public.source_items i USING(source_id)
    WHERE r.account_code=ANY(%s::text[]) AND r.status='published'
      AND r.document_type='cash' AND r.acquisition_method='manual' AND i.review_status='confirmed'
      AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'
      AND NOT EXISTS (SELECT 1 FROM public.pams_cashflow_fact f WHERE f.source_item_id=i.source_item_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.source_records fix JOIN public.source_items fi USING(source_id)
          JOIN public.pams_cashflow_fact f ON f.source_item_id=fi.source_item_id
          WHERE fix.account_code=r.account_code AND fix.status='published'
            AND fix.acquisition_method='manual' AND fi.review_status='confirmed'
            AND fi.raw_fields->>'corrects_source_item_id'=i.source_item_id::text
      )
"""

_DUPLICATES = f"""
WITH bank_rows AS (
    SELECT r.account_code,r.source_id,r.filename,i.source_item_id,i.source_position,
           i.record_hash,
           i.parsed_fields->>'business_date' AS day,
           i.parsed_fields->>'currency' AS currency,
           CASE WHEN i.parsed_fields->>'net_cash' ~ '^[+-]?[0-9]{{1,24}}([.][0-9]{{1,4}})?$'
                THEN (i.parsed_fields->>'net_cash')::numeric END AS amount,
           COALESCE(i.parsed_fields->>'summary',i.parsed_fields->>'note') AS summary,
           COALESCE(i.parsed_fields->>'duplicate_decision'='distinct',false) AS distinct_review,
           EXISTS (
               SELECT 1 FROM public.source_records review JOIN public.source_items decision USING(source_id)
               WHERE review.account_code=r.account_code AND review.source_name='{BANK_DUPLICATE_REVIEW_SOURCE}'
                 AND review.document_type='statement' AND review.acquisition_method='manual'
                 AND review.status='published' AND decision.review_status='confirmed'
                 AND decision.source_uid=i.source_item_id::text
                 AND decision.raw_fields->>'record_hash'=i.record_hash
                 AND decision.parsed_fields->>'duplicate_decision'='distinct'
           ) AS separately_reviewed
    FROM public.source_records r JOIN public.source_items i USING(source_id)
    WHERE r.account_code=ANY(%s::text[]) AND r.status='published'
      AND r.document_type='cash' AND i.review_status='confirmed'
      AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'
), collisions AS (
    SELECT account_code,day,currency,amount,count(*) AS row_count,
           (count(DISTINCT source_id)=1 AND bool_and(distinct_review)) OR bool_and(separately_reviewed) AS cleared
    FROM bank_rows WHERE day IS NOT NULL AND currency IS NOT NULL AND amount IS NOT NULL
    GROUP BY account_code,day,currency,amount
    HAVING count(*)>1
), suspicious AS (
    SELECT * FROM collisions WHERE NOT cleared
)
"""


def summaries(cur, active):
    accounts = sorted(SOURCE_ONLY_ACCOUNTS.intersection(active))
    if not accounts:
        return {}
    cur.execute("""SELECT r.account_code,count(DISTINCT r.source_id) AS documents,
        count(i.source_item_id) AS count,max(r.updated_at) AS synced_at,
        max(i.parsed_fields->>'business_date') FILTER (
            WHERE i.parsed_fields->>'business_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') AS as_of
        FROM public.source_records r LEFT JOIN public.source_items i ON i.source_id=r.source_id
          AND i.review_status='confirmed' AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'
        WHERE r.account_code=ANY(%s::text[]) AND r.status='published' AND r.document_type='cash'
        GROUP BY r.account_code""", (accounts,))
    result = {code: {'documents': 0, 'count': 0, 'duplicate_count': 0} for code in accounts}
    for row in cur.fetchall():
        result[row['account_code']].update(dict(row))
    cur.execute(_DUPLICATES + "SELECT account_code,sum(row_count)::int AS count FROM suspicious GROUP BY account_code",
                (accounts,))
    for row in cur.fetchall():
        result[row['account_code']]['duplicate_count'] = row['count']
    cur.execute("SELECT r.account_code,count(*) AS count " + _UNPOSTED_MANUAL + " GROUP BY r.account_code", (accounts,))
    for row in cur.fetchall():
        result[row['account_code']]['unposted_manual_count'] = row['count']
    return result


def unposted_manual(cur, account_code, offset):
    if account_code not in SOURCE_ONLY_ACCOUNTS:
        return []
    cur.execute("""SELECT i.parsed_fields->>'business_date' AS day,i.parsed_fields->>'currency' AS currency,
        i.parsed_fields->>'net_cash' AS amount,COALESCE(i.parsed_fields->>'summary',i.parsed_fields->>'note') AS summary
        """ + _UNPOSTED_MANUAL + " ORDER BY i.created_at DESC,i.source_item_id LIMIT 100 OFFSET %s",
        ([account_code], offset))
    return [dict(row) for row in cur.fetchall()]


def anomalies(cur, account_code, offset=0):
    if account_code not in SOURCE_ONLY_ACCOUNTS:
        return []
    cur.execute(_DUPLICATES + """SELECT b.source_id,b.source_item_id,b.filename,b.source_position,
        b.day,b.currency,b.amount,b.summary FROM bank_rows b
        JOIN suspicious s USING(account_code,day,currency,amount)
        ORDER BY b.day DESC,b.currency,b.amount,b.filename,b.source_item_id LIMIT 100 OFFSET %s""",
        ([account_code], offset))
    return [dict(row) for row in cur.fetchall()]


def confirm_distinct(cur, reviews):
    """Save conclusions against immutable originals, without changing their published rows."""
    accounts = [entry['account_code'] for entry in reviews]
    if not accounts or len(set(accounts)) != len(accounts) or not set(accounts) <= SOURCE_ONLY_ACCOUNTS:
        raise ValueError('请指定不同且支持来源核查的银行账户')
    for account in sorted(accounts):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    results = []
    for entry in reviews:
        account = entry['account_code']
        requested = {str(item_id) for item_id in entry['source_item_ids']}
        if len(requested) != len(entry['source_item_ids']) or not entry['note'].strip():
            raise ValueError('请提供唯一的原始明细编号和核查依据')
        cur.execute(_DUPLICATES + """SELECT b.source_item_id,b.source_id,b.record_hash,b.source_position,
            b.day,b.currency,b.amount,b.separately_reviewed,c.cleared
            FROM bank_rows b JOIN collisions c USING(account_code,day,currency,amount)""", ([account],))
        groups = defaultdict(list)
        for row in cur.fetchall():
            groups[(row['day'], row['currency'], row['amount'])].append(row)
        matched = set()
        pending = []
        for group in groups.values():
            group_ids = {str(row['source_item_id']) for row in group}
            if not requested.intersection(group_ids):
                continue
            if not group_ids <= requested:
                raise ValueError('同日同额分组已变化，请重新核查整组记录')
            matched.update(group_ids)
            if not group[0]['cleared']:
                pending.extend(row for row in group if not row['separately_reviewed'])
        if matched != requested:
            raise ValueError('有明细不属于该账户当前的同日同额分组')
        result = {'account_code': account, 'reviewed': len(requested), 'added': len(pending)}
        if pending:
            source_id = uuid4()
            create_source(cur, source_id=source_id, account_code=account, source_name=BANK_DUPLICATE_REVIEW_SOURCE,
                          acquisition_method='manual', document_type='statement')
            for row in pending:
                add_item(cur, source_id=source_id, item_id=uuid4(), source_uid=str(row['source_item_id']),
                         parse_method='manual', parsed_fields={'duplicate_decision': 'distinct'},
                         raw_fields={'source_id': str(row['source_id']), 'record_hash': row['record_hash'],
                                     'source_position': row['source_position']})
            cur.execute("""UPDATE public.source_items SET review_status='confirmed',review_note=%s,updated_at=now()
                WHERE source_id=%s""", (entry['note'].strip(), str(source_id)))
            cur.execute("""UPDATE public.source_records SET status='published',revision=revision+1,updated_at=now()
                WHERE source_id=%s""", (str(source_id),))
            result['review_source_id'] = str(source_id)
        results.append(result)
    return results
