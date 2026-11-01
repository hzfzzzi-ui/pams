from __future__ import annotations

from pathlib import Path


def test_fund_nav_estimates_has_formal_schema_before_api_join() -> None:
    sql = Path("database/postgres/schema/025a_asset_snapshots.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS public.market_observations" in sql
    for column in [
        "as_of DATE NOT NULL",
        "account_code VARCHAR(50)",
        "confirmed_nav_date DATE",
        "confirmed_unit_nav NUMERIC(20,8)",
        "estimated_unit_nav NUMERIC(28,10)",
        "estimated_market_value NUMERIC(28,10)",
        "estimated_delta NUMERIC(20,4)",
        "status VARCHAR(40)",
        "raw_json JSONB NOT NULL DEFAULT '{}'::jsonb",
    ]:
        assert column in sql
    assert "(as_of, account_code, symbol, name) WHERE observation_type='fund'" in sql
    assert "account_code IS NOT NULL AND symbol IS NOT NULL AND name IS NOT NULL AND status IS NOT NULL" in sql
    assert "market_observations_account_fund" in sql
