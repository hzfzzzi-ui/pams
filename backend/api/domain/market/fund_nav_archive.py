"""Explicit archive of published fund NAVs used by the period analysis."""

from datetime import timedelta
import os

import requests

from domain.market.fund_nav_estimates import (
    _fund_history_days,
    _stored_nav_prices,
    fetch_confirmed_nav_history,
    upsert_asset_market_daily_nav,
)


def archive_fund_nav_history(conn, *, assets, start, end, preview):
    """Fetch a bounded period and archive only missing official NAV observations."""
    import exchange_calendars

    pro = None
    if token := os.environ.get("TUSHARE_TOKEN"):
        import tushare

        pro = tushare.pro_api(token)
    start = exchange_calendars.get_calendar("XSHG").date_to_session(
        (start - timedelta(days=1)).isoformat(), direction="previous",
    ).date()
    expected = _fund_history_days(end, start=start)
    reports = []
    from psycopg2.extras import RealDictCursor

    with requests.Session() as session, conn.cursor(cursor_factory=RealDictCursor) as cur:
        for asset_id, asset in sorted(assets.items()):
            symbol = asset["primary_symbol"]
            stored = _stored_nav_prices(cur, symbol, start, end)
            missing = expected - stored.keys()
            if not missing:
                continue
            history, errors = fetch_confirmed_nav_history(
                symbol, session=session, pro=pro, start=min(missing), end=end, missing_days=missing,
            )
            found = {nav.nav_date for nav in history if nav.nav_date in missing}
            archive_count = 0
            if not preview and found:
                stored = _stored_nav_prices(cur, symbol, start, end)
                for nav in history:
                    if nav.nav_date not in found or nav.nav_date in stored:
                        continue
                    archive_count += upsert_asset_market_daily_nav(
                        cur, holding={"asset_id": asset_id, "symbol": symbol, "name": asset["canonical_name"]},
                        as_of=nav.nav_date, source=nav.source, unit_nav=nav.unit_nav, preserve_existing=True,
                        raw={"nav_date": nav.nav_date.isoformat(), "ts_code": nav.ts_code, "unit_nav": str(nav.unit_nav)},
                    )
            reports.append({"symbol": symbol, "available": len(found), "archived": archive_count,
                            "missing_dates": [day.isoformat() for day in sorted(missing - found)], "errors": errors})
    return {"status": "partial" if any(row["missing_dates"] or row["errors"] for row in reports) else "ok",
            "preview": preview, "rows": reports}
