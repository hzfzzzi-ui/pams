from datetime import date

import psycopg2
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from domain.investments import page_projection
from scripts import bootstrap_isolated_sources as bootstrap
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def connect(params, *, read_only=False):
    conn = psycopg2.connect(**params)
    conn.set_session(isolation_level="REPEATABLE READ", readonly=read_only)
    return conn


def test_projection_does_not_consume_a_concurrent_change(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
    today = date(2026, 11, 17)
    with connect(empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert page_projection.read_page(cur, "holdings", business_date=today) is None

        def build(snapshot):
            snapshot.execute("SELECT count(*) AS n FROM pams_accounts WHERE account_code = 'late'")
            count = snapshot.fetchone()["n"]
            with connect(empty_source_db) as writer, writer.cursor() as write:
                write.execute("INSERT INTO pams_accounts (account_code, display_name) VALUES ('late', 'Late')")
            return {"holdings": {"count": count}, "analysis": {"count": count}}

        assert page_projection.refresh(cur, business_date=today, build=build)["status"] == "published"
    with connect(empty_source_db, read_only=True) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        old = page_projection.read_page(cur, "holdings", business_date=today)
        assert old["projection"]["stale"]
        assert old["data"]["count"] == 0
    with connect(empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        def rebuild(snapshot):
            snapshot.execute("SELECT count(*) AS n FROM pams_accounts WHERE account_code = 'late'")
            count = snapshot.fetchone()["n"]
            return {"holdings": {"count": count}, "analysis": {"count": count}}

        assert page_projection.refresh(cur, business_date=today, build=rebuild)["revision"] == 2
    with connect(empty_source_db) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        current = page_projection.read_page(cur, "analysis", business_date=today)
        assert current["data"]["count"] == 1
        assert not current["projection"]["stale"]
        assert page_projection.refresh(cur, business_date=today, build=None)["status"] == "current"


def test_http_projection_matches_existing_calculations(empty_source_db, monkeypatch):
    from main import app

    with psycopg2.connect(**empty_source_db) as conn:
        bootstrap.initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO pams_accounts (account_code, display_name) VALUES ('sample', 'Sample')")
            cur.execute("""
                INSERT INTO pams_holdings_snapshot
                  (account_code, name, symbol, asset_liability, currency, major_category, minor_category,
                   as_of, market_value, cost, quantity, current_price)
                VALUES ('sample', 'Sample position', 'SAMPLE', '资产', 'CNY', 'A股', 'A股',
                        '2026-01-02', 1220, 1000, 10, 122)
            """)
    for key, pg_key in (("host", "PGHOST"), ("port", "PGPORT"), ("dbname", "PGDATABASE"),
                        ("user", "PGUSER"), ("password", "PGPASSWORD")):
        monkeypatch.setenv(pg_key, str(empty_source_db[key]))
    with TestClient(app) as client:
        holdings = "/api/investment/holdings-view"
        analysis = "/api/investment/analysis-view"
        nav = "/nav/weekly?latest=true&interval=daily"
        returns = "/investment/return-series"
        assert client.get(analysis).status_code == 404
        expected = {name: client.get(url).json() for name, url in
                    (("holdings", holdings), ("nav", nav), ("total", returns))}
        result = client.post("/api/investment/page-projection/refresh")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "published"
        after = client.get(holdings).json()
        charts = client.get(analysis).json()
        assert after.pop("projection")["revision"] == charts["projection"]["revision"]
        after.pop("valuation_state")
        after.pop("fx_rates")
        assert charts["data"]["nav"] == expected["nav"]
        assert charts["data"]["total"] == expected["total"]
        assert after.keys() == expected["holdings"].keys()
        assert after["holdings"]["rows"] == expected["holdings"]["holdings"]["rows"]
        assert after["account_cards"] == expected["holdings"]["account_cards"]
        assert set(charts["data"]["sections"]) == {"a_stock", "a_fund", "us_stock", "precious_metal"}

        with connect(empty_source_db) as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO market_observations
                  (observation_type, as_of, symbol, market, source, source_key, close_price)
                VALUES ('daily', '2026-01-03', 'SAMPLE', 'CN_A', 'tencent_cn_unadjusted_day_v1', 'SAMPLE', 128)
            """)
        stale = client.get(analysis).json()
        assert stale["data"] == charts["data"]
        assert stale["projection"]["stale"]
        assert client.post("/api/investment/page-projection/refresh").json()["revision"] == 2
        latest = client.get(analysis).json()
        assert not latest["projection"]["stale"]
        assert latest["data"]["total"]["rows"] == client.get(returns).json()["rows"]
        assert latest["data"]["total"]["rows"][-1]["market_value"] != charts["data"]["total"]["rows"][-1]["market_value"]
        with connect(empty_source_db, read_only=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT as_of, quantity FROM pams_holdings_snapshot WHERE account_code='sample'")
            assert cur.fetchall() == [(date(2026, 1, 2), 10)]
