"""Redeemed funds with source-linked transactions remain in analysis history."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_fund_curve_reads_verified_cn_market_trades_and_explicit_nav_archive(empty_source_db, monkeypatch):
    import main
    from domain.imports import source_records_v2 as source
    from domain.market import fund_nav_archive as archive
    from domain.market.fund_nav_estimates import ConfirmedNav

    opened, priced, exited = [date(2026, 11, number) for number in (9, 10, 11)]
    account, symbol, asset_id = "tencent_licaitong", "123456", "synthetic_fund"
    source_id = uuid4()
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_asset_master (asset_id,canonical_name,primary_symbol,asset_type,currency)
                            VALUES (%s,'Synthetic Fund',%s,'fund','CNY')""", (asset_id, symbol))
            source.create_source(cur, source_id=source_id, account_code=account, source_name="synthetic",
                                 acquisition_method="manual", document_type="trade")
            revision = 1
            for day, action, net_cash, execution_status in [
                (opened, "fund_subscription", "-100", "confirmed"),
                (exited, "fund_redemption", "120", None),
            ]:
                fields = {"business_date": day.isoformat(), "trade_type": action, "asset_id": asset_id,
                          "symbol": symbol, "name": "Synthetic Fund", "market": "CN", "currency": "CNY",
                          "quantity": "100", "net_cash": net_cash, "execution_status": execution_status,
                          "status": "confirmed"}
                item_id = uuid4()
                revision = source.add_manual_item(cur, source_id=source_id, item_id=item_id,
                                                  expected_revision=revision, parsed_fields=fields)
                revision = source.review_item(cur, source_id=source_id, item_id=item_id,
                                              expected_revision=revision, parsed_fields=fields, review_status="confirmed")
                cur.execute("""INSERT INTO pams_investment_trade_fact
                    (account_code,trade_date,asset_id,symbol,name,market,currency,trade_type,
                     quantity,net_cash,execution_status,source_item_id,fact_role)
                    VALUES (%s,%s,%s,%s,'Synthetic Fund','CN','CNY',%s,100,%s,%s,%s,'trade')""",
                            (account, day, asset_id, symbol, action, net_cash, execution_status, str(item_id)))
            cur.execute("UPDATE source_records SET status='published' WHERE source_id=%s", (str(source_id),))

    def fetch_prices(*args, **kwargs):
        return [ConfirmedNav(opened, Decimal("1"), None, None, "eastmoney_f10"),
                ConfirmedNav(priced, Decimal("1.2"), None, None, "eastmoney_f10")], []

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(archive, "fetch_confirmed_nav_history", fetch_prices)
    assets = {asset_id: {"canonical_name": "Synthetic Fund", "primary_symbol": symbol}}
    with psycopg2.connect(**empty_source_db) as conn:
        conn.set_session(readonly=True)
        result = archive.archive_fund_nav_history(conn, assets=assets, start=opened, end=exited, preview=True)
        assert result["rows"][0]["available"] == 2 and result["rows"][0]["archived"] == 0
    for expected in (2, 0):
        with psycopg2.connect(**empty_source_db) as conn:
            result = archive.archive_fund_nav_history(conn, assets=assets, start=opened, end=exited, preview=False)
            assert result["rows"][0]["archived"] == expected

    monkeypatch.setattr(main, "get_conn", lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, "_investment_return_view_as_of", lambda cur: exited)
    with TestClient(main.app) as web:
        response = web.get("/investment/section-return-series", params={"section": "a_fund", "start": str(opened)})
        assert response.status_code == 200, response.text
        payload = response.json()
    assert payload["method"] == "fund_verified_trade_nav_replay"
    assert payload["summary"]["total_return_rate"] is None
    assert payload["history_issues"] == []
    rows = payload["estimated_series"]["rows"]
    assert [Decimal(row["market_value"]) for row in rows] == [100, 120, 0]
    assert [Decimal(row["net_flow"]) for row in rows] == [100, 0, -120]
    assert Decimal(rows[-1]["display_return_rate"]) == Decimal("0.2")
