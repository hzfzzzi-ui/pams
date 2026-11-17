"""Manual account estimates use documented business dates and verified cash."""
# ruff: noqa: F811
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources

from domain.investments.manual_nav_projection import load_manual_nav_estimates
from domain.investments.valuation_nav import build_nav_weekly_payload
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_manual_nav_uses_prices_and_latest_members_and_recalculates_after_import(empty_source_db, monkeypatch):
    first = date(2026, 11, 11)
    revised = first + timedelta(days=1)
    end = revised + timedelta(days=1)
    code = "manual_fund_test"
    policy = {"accounts": {code: {"nav_position_policy": "carry_last_observed"}}}
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES (%s,'Synthetic','CNY')", (code,))
            insert = """INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,asset_liability,
                major_category,minor_category,market_value,quantity,as_of)
                VALUES (%s,%s,%s,'CNY','资产',%s,%s,%s,%s,%s) RETURNING id"""
            cur.execute(insert, (code,"000001","Fund","A股","A股基金",100,10,first))
            cur.execute(insert, (code,"000002","Other fund","A股","A股基金",40,4,first))
            cur.execute(insert, (code,"CASH_CNY","Cash","现金","现金",20,None,first))
            cash_id = cur.fetchone()["id"]
            cur.execute(insert, (code,"CASH_CNY","Cash","现金","现金",99,None,revised))
            cur.execute("""INSERT INTO market_observations
                (observation_type,as_of,source,source_key,symbol,market,unit_nav)
                VALUES ('daily',%s,'eastmoney_f10','test_nav','000001','基金',12)""", (end,))

            monkeypatch.setattr(
                "domain.investments.manual_nav_projection.load_account_snapshot_coverage",
                lambda _cur, pairs: {pair: {"member_ids": {cash_id}} for pair in pairs},
            )
            args = dict(owner_ratio_case_sql=lambda _: ("1", []), account_owner_ratio=lambda _: Decimal(1))
            def chart():
                return build_nav_weekly_payload(
                    cur, start_date=first-timedelta(days=1), terminal_date=end, interval="daily",
                    base_currency="CNY", include_ibkr=False, account_config=policy,
                    overview_payload={"accounts":[{"account_code":code,"account_type":"fund_platform","include_in_nav":True}]},
                    **args,
                )["rows"]

            before = chart()
            assert before[0].get("availableNetWorth") is None
            assert Decimal(before[1]["availableNetWorth"]) == 160
            assert Decimal(before[3]["availableNetWorth"]) == 180
            assert before[3]["netWorth"] is None
            assert before[3]["estimated_accounts"] == [code]
            assert before[3]["latest_known_components"][code]["through"] == first.isoformat()

            # A later import gives a past business date and covers only one fund.
            cur.execute(insert, (code,"000001","Fund","A股","A股基金",150,15,revised))
            after = chart()
            assert before[:2] == after[:2]
            assert Decimal(after[2]["availableNetWorth"]) == 210
            assert Decimal(after[3]["availableNetWorth"]) == 240
            assert after[3]["latest_known_components"][code]["through"] == revised.isoformat()
            assert after[3]["netWorth"] is None
            assert chart() == after

            monkeypatch.setattr("domain.investments.manual_nav_projection.load_account_snapshot_coverage", lambda *_: {})
            values = load_manual_nav_estimates(cur, accounts=policy["accounts"], dates=[end], **args)
            assert values[end][code][0] == 220


def test_manual_nav_marks_stock_and_verified_gold_at_the_sample_date(empty_source_db, monkeypatch):
    first = date(2026, 11, 11)
    end = first + timedelta(days=2)
    code = "manual_assets_test"
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES (%s,'Synthetic','CNY')", (code,))
            cur.execute("""INSERT INTO pams_holdings_snapshot(account_code,symbol,name,currency,asset_liability,
                major_category,minor_category,market_value,quantity,as_of) VALUES
                (%s,'600000','Synthetic share','CNY','资产','A股','A股',100,10,%s),
                (%s,'CCB_GOLD_ACCUM','Synthetic gold','CNY','资产','贵金属','黄金',800,1,%s)
                RETURNING id, symbol""", (code, first, code, first))
            gold_id = next(row["id"] for row in cur.fetchall() if row["symbol"] == "CCB_GOLD_ACCUM")
            cur.execute("""INSERT INTO pams_ohlcv_daily
                (symbol,market,trade_date,close) VALUES ('sh600000','CN_A',%s,12)""", (end,))
            cur.execute("""INSERT INTO market_observations
                (observation_type,as_of,source,source_key,asset_id,close_price)
                VALUES ('daily',%s,'akshare_sge_daily','test_close','BENCH_GOLD_SPOT_AU9999',829.49)""", (end,))
            monkeypatch.setattr(
                "domain.investments.manual_nav_projection.load_account_snapshot_coverage",
                lambda _cur, pairs: {pair: {"member_ids": {gold_id}} for pair in pairs},
            )
            estimates = load_manual_nav_estimates(
                cur, accounts={code: {}}, dates=[first, end],
                owner_ratio_case_sql=lambda _: ("1", []), account_owner_ratio=lambda _: Decimal(1),
            )
            assert estimates[first][code] == (Decimal(100), first, first)
            assert estimates[end][code] == (Decimal("949.49"), first, first)
