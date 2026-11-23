"""Publish and read derived investment page data from one input revision."""

from contextlib import closing

from psycopg2.extras import Json, RealDictCursor


PAGE_START = "2026-01-01"
_RECHECK_MINUTES = 5


def schema_available(cur):
    cur.execute("SELECT to_regclass('public.investment_page_projection') IS NOT NULL AS available")
    return cur.fetchone()["available"]


def read_page(cur, page, *, business_date, member=None):
    if not schema_available(cur):
        return None
    cur.execute(
        """SELECT payload #> %s::text[] AS data, published_revision, published_at,
                  EXISTS (SELECT 1 FROM public.investment_projection_invalidation) OR business_date <> %s
                  OR published_at < now() - make_interval(mins => %s) AS stale
             FROM public.investment_page_projection
            WHERE singleton AND payload IS NOT NULL""",
        ([page] if member is None else [page, member], business_date, _RECHECK_MINUTES),
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


def refresh(cur, *, business_date, build, connect):
    """Build under REPEATABLE READ and atomically replace the previous model."""
    if not schema_available(cur):
        return {"status": "unavailable"}
    cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('pams-investment-page-projection')) AS locked")
    if not cur.fetchone()["locked"]:
        return {"status": "busy"}
    cur.execute(
        """SELECT published_revision,
                  EXISTS (SELECT 1 FROM public.investment_projection_invalidation) AS dirty,
                  business_date = %s AND published_at >= now() - make_interval(mins => %s) AS recent
             FROM public.investment_page_projection WHERE singleton""",
        (business_date, _RECHECK_MINUTES),
    )
    state = cur.fetchone()
    if state["recent"] and not state["dirty"]:
        return {"status": "current", "revision": state["published_revision"]}
    # Evidence readers require REPEATABLE READ READ ONLY. Import the writer's
    # snapshot so the evidence and the markers being cleared are consistent.
    cur.execute("SELECT pg_export_snapshot() AS snapshot")
    snapshot = cur.fetchone()["snapshot"]
    with closing(connect()) as reader, reader:
        reader.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with reader.cursor(cursor_factory=RealDictCursor) as read:
            read.execute("SET TRANSACTION SNAPSHOT %s", (snapshot,))
            payload = build(read)
    revision = (state["published_revision"] or 0) + 1
    # Only markers visible to the computation are cleared. Concurrent commits
    # remain dirty, including writers that started before this transaction.
    cur.execute("DELETE FROM public.investment_projection_invalidation")
    cur.execute(
        """UPDATE public.investment_page_projection
              SET payload = %s, published_revision = %s, business_date = %s,
                  published_at = clock_timestamp()
            WHERE singleton""",
        (Json(payload), revision, business_date),
    )
    return {"status": "published", "revision": revision}
