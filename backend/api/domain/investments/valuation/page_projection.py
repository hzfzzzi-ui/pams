"""Publish and read derived investment page data from one input revision."""

from contextlib import closing

from psycopg2.extras import Json, RealDictCursor

from domain.investments.returns.benchmark_summary import build_performance_summary_payload


PAGE_START = "2026-01-01"
_RECHECK_MINUTES = 5
PROJECTION_SCHEMA_VERSION = "8"


def schema_available(cur):
    cur.execute("SELECT to_regclass('public.investment_page_projection') IS NOT NULL AS available")
    return cur.fetchone()["available"]


def read_page(cur, page, *, business_date, member=None):
    if not schema_available(cur):
        return None
    cur.execute(
        """SELECT payload #> %s::text[] AS data, published_revision, published_at,
                  EXISTS (SELECT 1 FROM public.investment_projection_invalidation) OR business_date <> %s
                  OR published_at < now() - make_interval(mins => %s)
                  OR (payload ->> '_schema_version') IS DISTINCT FROM %s AS stale
             FROM public.investment_page_projection
            WHERE singleton AND payload IS NOT NULL""",
        ([page] if member is None else [page, member], business_date, _RECHECK_MINUTES, PROJECTION_SCHEMA_VERSION),
    )
    row = cur.fetchone()
    if not row or row["data"] is None:
        return None
    return {
        "data": row["data"],
        "projection": {
            "revision": row["published_revision"],
            "published_at": row["published_at"].isoformat(),
            "stale": row["stale"],
        },
    }


def performance_summary(cur, business_date, pages):
    return build_performance_summary_payload(
        cur, today=business_date, holdings_data=pages["holdings"]["holdings"],
        sections=pages["analysis"]["sections"], total=pages["analysis"]["total"],
    )


def refresh(cur, *, business_date, build, connect, expected_through=None):
    """Build under REPEATABLE READ and atomically replace the previous model."""
    if not schema_available(cur):
        return {"status": "unavailable"}
    cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('pams-investment-page-projection')) AS locked")
    if not cur.fetchone()["locked"]:
        return {"status": "busy"}
    cur.execute(
        """SELECT published_revision, payload #>> '{analysis,total,rows,-1,date}' AS total_as_of,
                  EXISTS (SELECT 1 FROM public.investment_projection_invalidation) AS dirty,
                  business_date = %s AND published_at >= now() - make_interval(mins => %s)
                  AND (payload ->> '_schema_version') = %s AS recent
             FROM public.investment_page_projection WHERE singleton""",
        (business_date, _RECHECK_MINUTES, PROJECTION_SCHEMA_VERSION),
    )
    state = cur.fetchone()
    if state["recent"] and not state["dirty"] and (
        expected_through is None or (state["total_as_of"] or "") >= expected_through.isoformat()
    ):
        return {"status": "current", "revision": state["published_revision"],
                "analysis_total_as_of": state.get("total_as_of")}
    # Evidence readers require REPEATABLE READ READ ONLY. Import the writer's
    # snapshot so the evidence and the markers being cleared are consistent.
    cur.execute("SELECT pg_export_snapshot() AS snapshot")
    snapshot = cur.fetchone()["snapshot"]
    with closing(connect()) as reader, reader:
        reader.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with reader.cursor(cursor_factory=RealDictCursor) as read:
            read.execute("SET TRANSACTION SNAPSHOT %s", (snapshot,))
            payload = build(read)
    stored_payload = {**payload, "_schema_version": PROJECTION_SCHEMA_VERSION} if isinstance(payload, dict) else payload
    revision = (state["published_revision"] or 0) + 1
    # Only markers visible to the computation are cleared. Concurrent commits
    # remain dirty, including writers that started before this transaction.
    cur.execute("DELETE FROM public.investment_projection_invalidation")
    cur.execute(
        """UPDATE public.investment_page_projection
              SET payload = %s, published_revision = %s, business_date = %s,
                  published_at = clock_timestamp()
            WHERE singleton""",
        (Json(stored_payload), revision, business_date),
    )
    if expected_through:
        rows = payload["analysis"]["total"].get("rows") or []
        actual = rows[-1].get("date") if rows else None
        if not actual or str(actual) < expected_through.isoformat():
            return {"status": "waiting", "revision": revision, "analysis_total_as_of": actual,
                    "reason": "waiting_for_return_inputs"}
        return {"status": "published", "revision": revision, "analysis_total_as_of": actual}
    return {"status": "published", "revision": revision}
