from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def test_esop_reconciles_net_bank_dividends_once_and_by_year(empty_source_db, monkeypatch):
    import main
    from domain.imports import source_records_v2 as source
    from domain.investments.esop_dividends import reconcile_bank_dividend

    days = [date(2025, 3, 31), date(2026, 3, 31), date(2026, 10, 1)]
    snapshot_date = date(2026, 7, 31)
    amounts = [Decimal(25), Decimal(120), Decimal(40)]
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO pams_accounts (account_code,display_name,base_currency)
                VALUES ('test_bank','Test bank','CNY'), ('huawei_stock','Test ESOP','CNY')
                ON CONFLICT (account_code) DO NOTHING""")
            cur.execute("""INSERT INTO pams_holdings_snapshot
                (account_code,name,symbol,currency,asset_liability,major_category,minor_category,as_of,quantity,cost,market_value)
                VALUES ('huawei_stock','Test ESOP','ESOP1','CNY','资产','ESOP','ESOP',%s,100,700,700)""",
                        (snapshot_date,))
            for day, amount in zip(days, amounts, strict=True):
                source_id, item = uuid4(), uuid4()
                fields = {"business_date": day.isoformat(), "currency": "CNY", "net_cash": str(amount)}
                source.create_source(cur, source_id=source_id, account_code="test_bank", source_name="test",
                                     acquisition_method="manual", document_type="cash")
                revision = source.add_manual_item(cur, source_id=source_id, item_id=item, expected_revision=1,
                                                  parsed_fields=fields)
                source.review_item(cur, source_id=source_id, item_id=item, expected_revision=revision,
                                   parsed_fields=fields, review_status="confirmed")
                cur.execute("UPDATE source_records SET status='published' WHERE source_id=%s", (str(source_id),))
                cur.execute("""INSERT INTO pams_cashflow_fact
                    (account_code,event_date,amount,currency,source_item_id,fact_role)
                    VALUES ('test_bank',%s,%s,'CNY',%s,'statement_cashflow') RETURNING cashflow_id""",
                            (day, amount, str(item)))
                cashflow = cur.fetchone()["cashflow_id"]
                cur.execute("""INSERT INTO cashflow_classification
                    (cashflow_id,cashflow_type,category,subcategory,status,classification_source)
                    VALUES (%s,'operating_income','收入','投资分红','confirmed','manual')""", (cashflow,))
                args = dict(cashflow_id=cashflow, event_date=day, expected_amount=amount,
                            account="huawei_stock", symbol="ESOP1")
                assert reconcile_bank_dividend(cur, **args)["status"] == "ready"
                applied = reconcile_bank_dividend(cur, **args, apply=True)
                assert applied["status"] == "linked"
                repeated = reconcile_bank_dividend(cur, **args, apply=True)
                assert repeated["status"] == "already_linked"
                assert repeated["trade_id"] == applied["trade_id"]
            with pytest.raises(ValueError, match="does not match"):
                reconcile_bank_dividend(cur, **{**args, "expected_amount": Decimal(41)}, apply=True)
            cur.execute("SELECT count(*) AS n, sum(amount) AS total FROM pams_cashflow_fact")
            assert cur.fetchone() == {"n": 3, "total": sum(amounts)}

    monkeypatch.setattr(main, "get_conn", lambda: psycopg2.connect(**empty_source_db))
    monkeypatch.setattr(main, "_holdings_market_view_as_of", lambda cur: snapshot_date)
    with TestClient(main.app) as client:
        response = client.get("/holdings/by-category/ESOP")
        assert response.status_code == 200, response.text
        payload = response.json()
    row = next(row for row in payload["rows"] if row["symbol"] == "ESOP1")
    assert Decimal(row["cost_cny"]) == Decimal(row["market_value_cny"]) == 700
    assert Decimal(row["holding_pnl_cny"]) == 145
    assert Decimal(payload["esop_performance"]["annual_pnl_cny"]) == 120
