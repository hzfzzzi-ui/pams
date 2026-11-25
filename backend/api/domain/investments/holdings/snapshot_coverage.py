"""Read account-snapshot evidence without treating row counts as completeness."""

from __future__ import annotations

from typing import Any


def point_in_time_visible_sql(holding_alias: str, date_sql: str) -> str:
    """A single-day member cannot stand in for a later account observation."""
    if not holding_alias.replace('_', '').isalnum():
        raise ValueError('invalid holding alias')
    return f"""(NOT EXISTS (
        SELECT 1 FROM public.account_snapshot_member point_member
        JOIN public.account_snapshot_version point_version ON point_version.version_id=point_member.version_id
        WHERE point_member.holding_id={holding_alias}.id AND point_version.coverage_status='partial'
          AND point_version.scope_attestation->'point_in_time_only'='true'::jsonb)
      OR ({holding_alias}.as_of = ({date_sql}) AND EXISTS (
        SELECT 1 FROM public.account_snapshot_member valid_member
        JOIN public.account_snapshot_version valid_version ON valid_version.version_id=valid_member.version_id
        WHERE valid_member.holding_id={holding_alias}.id
          AND valid_member.holding_row_hash=md5(to_jsonb({holding_alias})::text)
          AND valid_version.account_code={holding_alias}.account_code AND valid_version.as_of={holding_alias}.as_of
          AND valid_version.version_number=(SELECT MAX(v.version_number) FROM public.account_snapshot_version v
              WHERE v.account_code={holding_alias}.account_code AND v.as_of={holding_alias}.as_of))))"""


def load_account_snapshot_coverage(cur, pairs: set[tuple[str, Any]]) -> dict[tuple[str, Any], dict[str, Any]]:
    """Read and revalidate immutable membership and explicit scope evidence."""
    if not pairs:
        return {}
    result = {pair: {"status": "unknown", "reason": "snapshot_scope_unproven", "member_ids": set(),
                     "cash_status": "unknown", "version_id": None}
              for pair in pairs}
    cur.execute("""SELECT to_regclass('public.account_snapshot_version') IS NOT NULL AS version_exists,
        to_regclass('public.account_snapshot_member') IS NOT NULL AS member_exists""")
    exists = cur.fetchone()
    if not exists or not exists.get("version_exists") or not exists.get("member_exists"):
        return result
    ordered = sorted(pairs, key=lambda item: (item[0], str(item[1])))
    cur.execute("""WITH requested AS (
        SELECT * FROM unnest(%s::text[],%s::date[]) AS r(account_code,as_of)
    ), latest AS (
        SELECT DISTINCT ON (v.account_code,v.as_of) v.*
        FROM public.account_snapshot_version v
        JOIN requested r ON r.account_code=v.account_code AND r.as_of=v.as_of
        ORDER BY v.account_code,v.as_of,v.version_number DESC
    )
    SELECT v.version_id,v.account_code,v.as_of,v.coverage_status,v.cash_status,v.scope_attestation,
           COALESCE(array_agg(m.holding_id) FILTER (WHERE h.id IS NOT NULL
             AND h.account_code=v.account_code AND h.as_of=v.as_of
             AND md5(to_jsonb(h)::text)=m.holding_row_hash), '{}') AS valid_members,
           COALESCE((SELECT array_agg(DISTINCT pm.holding_id)
             FROM public.account_snapshot_version pv
             JOIN public.account_snapshot_member pm ON pm.version_id=pv.version_id
             WHERE pv.account_code=v.account_code AND pv.as_of=v.as_of AND pv.coverage_status='partial'
               AND pv.scope_attestation->'point_in_time_only'='true'::jsonb), '{}') AS point_members,
           COUNT(m.holding_id) AS member_count,
           COUNT(m.holding_id) FILTER (WHERE h.id IS NOT NULL AND h.account_code=v.account_code
             AND h.as_of=v.as_of AND md5(to_jsonb(h)::text)=m.holding_row_hash) AS valid_count
    FROM latest v LEFT JOIN public.account_snapshot_member m ON m.version_id=v.version_id
    LEFT JOIN public.pams_holdings_snapshot h ON h.id=m.holding_id
    GROUP BY v.version_id,v.account_code,v.as_of,v.coverage_status,v.cash_status,v.scope_attestation""",
                ([account for account, _ in ordered], [day for _, day in ordered]))
    for raw in cur.fetchall():
        row = dict(raw)
        pair = (str(row["account_code"]), row["as_of"])
        valid = int(row["member_count"]) == int(row["valid_count"])
        status = "partial" if valid and row["coverage_status"] == "partial" else "unknown"
        if valid and row["coverage_status"] == "complete":
            from domain.investments.holdings.account_snapshot_evidence import verified_complete
            if verified_complete(cur, row["version_id"]):
                status = "complete"
        reason = {"partial":"snapshot_scope_partial", "complete":"user_confirmed_complete_scope",
                  "unknown":"snapshot_scope_unproven"}[status]
        result[pair] = {"status": status, "reason": reason,
                        "member_ids": set(row["valid_members"] or []) if valid else set(),
                        "cash_status": "known" if status == "complete" else "unknown", "version_id": str(row["version_id"]),
                        "point_in_time_member_ids": set(row['point_members'] or ())}
    # Reconstructed members depend on the retained trades and daily mark as
    # well as the holding row. A changed or missing dependency cannot certify
    # the historical value even if the holding itself has not changed.
    point_ids = set().union(*(proof.get('point_in_time_member_ids', set()) for proof in result.values()))
    if point_ids:
        cur.execute("""SELECT h.id,h.symbol,h.as_of,h.current_price,i.parsed_fields->'reconstruction_evidence' AS reconstruction,v.version_key FROM pams_holdings_snapshot h
            JOIN source_items i ON i.source_item_id=h.source_item_id
            LEFT JOIN valuation_evidence v ON v.account_code=h.account_code AND v.business_date=h.as_of
              AND v.result_kind='holding' AND v.inputs @> jsonb_build_array(jsonb_build_object(
                  'input_kind','holding','evidence',jsonb_build_object('source_ref',jsonb_build_object('key',jsonb_build_object('id',h.id)))))
            WHERE h.id=ANY(%s::bigint[]) AND i.parsed_fields ? 'reconstruction_evidence'""", (list(point_ids),))
        versions: dict[int, list[str]] = {}
        reconstructions = {}
        for proof in cur.fetchall():
            reconstructions[proof['id']] = dict(proof)
            versions.setdefault(proof['id'], [])
            if proof['version_key']:
                versions[proof['id']].append(proof['version_key'])
        if versions:
            from domain.investments.valuation.valuation_input_evidence import bound_holding_freshness
            rejected = set()
            for ident, keys in versions.items():
                if not keys or not any(bound_holding_freshness(cur, key) in {'unknown', 'unchanged_for_recorded_inputs'} for key in keys):
                    rejected.add(ident)
                    continue
                row = reconstructions[ident]
                if 'quote_id' in (row.get('reconstruction') or {}):
                    from domain.investments.returns.fund_return_history import NAV_SOURCES
                    cur.execute("""SELECT MIN(unit_nav) AS value,COUNT(DISTINCT unit_nav) AS count
                        FROM market_observations WHERE observation_type='daily' AND market='基金' AND currency='CNY'
                          AND symbol=%s AND as_of=%s AND source=ANY(%s) AND unit_nav>0""",
                        (row['symbol'], row['as_of'], list(NAV_SOURCES)))
                    mark = cur.fetchone()
                    if mark['count'] != 1 or mark['value'] != row['current_price']:
                        rejected.add(ident)
            for proof in result.values():
                proof['member_ids'].difference_update(rejected)
    return result


def annotate_snapshot_coverage(cur, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = {(str(row.get("account_code") or row.get("account")), row["as_of"])
             for row in rows if (row.get("account_code") or row.get("account")) and row.get("as_of")
             and row.get("holding_id", row.get("id")) is not None}
    coverage = load_account_snapshot_coverage(cur, pairs)
    for row in rows:
        key = (str(row.get("account_code") or row.get("account")), row.get("as_of"))
        holding_id = row.get("holding_id", row.get("id"))
        if holding_id is None or key not in coverage:
            continue
        proof = coverage[key]
        row["snapshot_scope_status"] = proof["status"]
        row["snapshot_scope_reason"] = proof["reason"]
        row["snapshot_scope_member"] = "recorded" if holding_id in proof["member_ids"] else "unverified"
        row["snapshot_scope_cash_status"] = proof["cash_status"]
        row["snapshot_scope_version_id"] = proof["version_id"]
        row["snapshot_market_evidence"] = "unknown"
    return rows
