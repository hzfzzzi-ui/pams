from __future__ import annotations

from pathlib import Path


def test_fund_nav_estimates_has_formal_schema_before_api_join() -> None:
    sql = Path("database/postgres/schema/067_fund_nav_estimates.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS public.pams_fund_nav_estimates" in sql
    for column in [
        "as_of DATE NOT NULL",
        "account_code VARCHAR(50) NOT NULL",
        "symbol VARCHAR(100) NOT NULL",
        "name VARCHAR(200) NOT NULL",
        "confirmed_nav_date DATE",
        "confirmed_unit_nav NUMERIC(20, 8)",
        "estimated_unit_nav NUMERIC(20, 8)",
        "estimated_market_value NUMERIC(20, 4)",
        "estimated_delta NUMERIC(20, 4)",
        "status VARCHAR(40) NOT NULL",
        "raw_json JSONB NOT NULL DEFAULT '{}'::jsonb",
    ]:
        assert column in sql
    assert (
        "UNIQUE (as_of, account_code, symbol, name)" in sql
        or "UNIQUE(as_of, account_code, symbol, name)" in sql
    )
    assert "idx_pams_fund_nav_estimates_latest" in sql
