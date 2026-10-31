"""Read the compact operational status needed by the sync center.

The legacy job table remains the current provider. This interface separates
status consumers from a future logging provider; it does not move or delete
business publication pointers, evidence, or audit records.
"""
from typing import Any
from domain.operations.runtime_events import emit_status_observation


def latest_sync_run(cur: Any, sync_key: str, asset_scope: str | None = None) -> dict[str, Any] | None:
    where = ['job_key = %s']
    params: list[Any] = [sync_key]
    if asset_scope:
        where.append('asset_scope = %s')
        params.append(asset_scope)
    cur.execute(
        f"""
        SELECT job_key AS sync_key,
               asset_scope,
               status,
               started_at,
               finished_at,
               target_date AS target_trade_date,
               data_as_of AS latest_trade_date_after,
               rows_affected AS rows_upserted,
               summary_json, error_text
        FROM pams_data_job_run
        WHERE {' AND '.join(where)}
        ORDER BY started_at DESC
        LIMIT 1
        """,
        params,
    )
    row = cur.fetchone()
    result = dict(row) if row else None
    emit_status_observation(result)
    return result
