"""Recover evidenced precious-metal return history before the first holding snapshot."""
# ruff: noqa: F811
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.imports import source_records_v2 as source
from domain.investments.investment_returns import build_section_return_series_payload
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_gold_purchase_reconstructs_holding_history_without_earlier_snapshot(empty_source_db):
    buy_day, next_day, snapshot_day = date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 12)
    account = "synthetic_gold"
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES (%s,'Synthetic','CNY')", (account,))
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,asset_liability,
                major_category,minor_category,market_value,quantity,cost,as_of) VALUES
                (%s,'XAU','Synthetic gold','CNY','资产','贵金属','黄金',149,10,200,%s)""", (account, snapshot_day))
            for day, close in ((buy_day, 22), (next_day, 19), (snapshot_day, 15)):
                cur.execute("""INSERT INTO market_observations(observation_type,as_of,source,asset_id,close_price,source_key)
                    VALUES ('daily',%s,'akshare_sge_daily','BENCH_GOLD_SPOT_AU9999',%s,'synthetic_close')""", (day, close))

            def chart(*, start=date(2026, 1, 1), interval="daily"):
                return build_section_return_series_payload(
                    cur, section="precious_metal", start_date=start, interval=interval,
                    base_currency="CNY", view_as_of=snapshot_day, reported_returns_cfg={}, overview_payload={},
                    owner_ratio_case_sql=lambda _: ("1", []), account_owner_ratio=lambda _: Decimal(1),
                    excluded_holding_accounts=set(),
                )

            snapshot_only = chart()
            assert [w["date"] for w in snapshot_only["rows"]] == [snapshot_day.isoformat()]
            assert "chart_zero_before" not in snapshot_only
            transaction = {
                "business_date": buy_day.isoformat(), "trade_type": "buy", "symbol": "XAU",
                "market": "贵金属", "quantity": "10", "net_cash": "-200", "currency": "CNY",
            }
            sid, iid = uuid4(), uuid4()
            source.create_source(cur, source_id=sid, account_code=account, source_name="test",
                                 acquisition_method="manual", document_type="trade")
            source.add_item(cur, source_id=sid, item_id=iid, parse_method="manual",
                            raw_fields=transaction, parsed_fields=transaction)
            source.review_item(cur, source_id=sid, item_id=iid, expected_revision=2,
                               parsed_fields=transaction, review_status="confirmed")
            assert source.publish_trade_source(cur, source_id=sid, expected_revision=3, preview_only=True)["can_publish"]
            source.publish_trade_source(cur, source_id=sid, expected_revision=3)
            history = chart()
            assert history["chart_zero_before"] == buy_day.isoformat()
            rows = history["rows"]
            by_date = {w["date"]: w for w in rows}
            assert rows[0]["date"] == buy_day.isoformat()
            assert Decimal(rows[0]["display_return_rate"]) == Decimal("0.10")
            assert Decimal(by_date[next_day.isoformat()]["market_value"]) == Decimal("190")
            assert Decimal(rows[-1]["market_value"]) == Decimal("150")
            assert Decimal(rows[-1]["display_return_rate"]).quantize(Decimal("0.0001")) == Decimal("-0.2500")
            assert [Decimal(w["net_flow"]) for w in rows] == [Decimal("200")] + [Decimal("0")] * (len(rows)-1)
            later_window = chart(start=next_day)
            assert "chart_zero_before" not in later_window
            assert later_window["rows"][0]["date"] == next_day.isoformat()
            assert Decimal(later_window["rows"][0]["display_return_rate"]) == 0
            assert chart(interval="weekly")["rows"][0]["date"] == buy_day.isoformat()

            # If the amount no longer agrees with the verified source, the earlier
            # values must not be extrapolated from the current position.
            cur.execute("UPDATE pams_investment_trade_fact SET quantity=9 WHERE account_code=%s", (account,))
            assert [w["date"] for w in chart()["rows"]] == [snapshot_day.isoformat()]
