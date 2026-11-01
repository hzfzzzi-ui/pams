"""Read account-snapshot evidence without treating row counts as completeness."""

from __future__ import annotations

from typing import Any


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
    SELECT v.version_id,v.account_code,v.as_of,v.coverage_status,v.cash_status,
           COALESCE(array_agg(m.holding_id) FILTER (WHERE h.id IS NOT NULL
             AND h.account_code=v.account_code AND h.as_of=v.as_of
             AND md5(to_jsonb(h)::text)=m.holding_row_hash), '{}') AS valid_members,
           COUNT(m.holding_id) AS member_count,
           COUNT(m.holding_id) FILTER (WHERE h.id IS NOT NULL AND h.account_code=v.account_code
             AND h.as_of=v.as_of AND md5(to_jsonb(h)::text)=m.holding_row_hash) AS valid_count
    FROM latest v LEFT JOIN public.account_snapshot_member m ON m.version_id=v.version_id
    LEFT JOIN public.pams_holdings_snapshot h ON h.id=m.holding_id
    GROUP BY v.version_id,v.account_code,v.as_of,v.coverage_status,v.cash_status""",
                ([account for account, _ in ordered], [day for _, day in ordered]))
    for raw in cur.fetchall():
        row = dict(raw)
        pair = (str(row["account_code"]), row["as_of"])
        valid = int(row["member_count"]) == int(row["valid_count"])
        status = "partial" if valid and row["coverage_status"] == "partial" else "unknown"
        if valid and row["coverage_status"] == "complete":
            from domain.investments.account_snapshot_evidence import verified_complete
            if verified_complete(cur, row["version_id"]):
                status = "complete"
        reason = {"partial":"snapshot_scope_partial", "complete":"user_confirmed_complete_scope",
                  "unknown":"snapshot_scope_unproven"}[status]
        result[pair] = {"status": status, "reason": reason,
                        "member_ids": set(row["valid_members"] or []) if valid else set(),
                        "cash_status": "known" if status == "complete" else "unknown", "version_id": str(row["version_id"])}
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
