"""Account sync views over retained bank source evidence."""

from domain.imports.bank_source_confirmation import SOURCE_ONLY_ACCOUNTS


_DUPLICATES = """
WITH bank_rows AS (
    SELECT r.account_code,r.source_id,r.filename,i.source_item_id,i.source_position,
           i.parsed_fields->>'business_date' AS day,
           i.parsed_fields->>'currency' AS currency,
           CASE WHEN i.parsed_fields->>'net_cash' ~ '^[+-]?[0-9]{1,24}([.][0-9]{1,4})?$'
                THEN (i.parsed_fields->>'net_cash')::numeric END AS amount,
           COALESCE(i.parsed_fields->>'summary',i.parsed_fields->>'note') AS summary,
           COALESCE(i.parsed_fields->>'duplicate_decision'='distinct',false) AS distinct_review
    FROM public.source_records r JOIN public.source_items i USING(source_id)
    WHERE r.account_code=ANY(%s::text[]) AND r.status='published'
      AND r.document_type='cash' AND i.review_status='confirmed'
      AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'
), suspicious AS (
    SELECT account_code,day,currency,amount,count(*) AS row_count
    FROM bank_rows WHERE day IS NOT NULL AND currency IS NOT NULL AND amount IS NOT NULL
    GROUP BY account_code,day,currency,amount
    HAVING count(*)>1 AND NOT (count(DISTINCT source_id)=1 AND bool_and(distinct_review))
)
"""


def summaries(cur, active):
    accounts = sorted(SOURCE_ONLY_ACCOUNTS.intersection(active))
    if not accounts:
        return {}
    cur.execute("""SELECT r.account_code,count(DISTINCT r.source_id) AS documents,
        count(i.source_item_id) AS count,max(r.updated_at) AS synced_at
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
    return result


def anomalies(cur, account_code, offset=0):
    if account_code not in SOURCE_ONLY_ACCOUNTS:
        return []
    cur.execute(_DUPLICATES + """SELECT b.source_id,b.source_item_id,b.filename,b.source_position,
        b.day,b.currency,b.amount,b.summary FROM bank_rows b
        JOIN suspicious s USING(account_code,day,currency,amount)
        ORDER BY b.day DESC,b.currency,b.amount,b.filename,b.source_item_id LIMIT 100 OFFSET %s""",
        ([account_code], offset))
    return [dict(row) for row in cur.fetchall()]
