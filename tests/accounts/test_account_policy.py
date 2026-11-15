"""Account ownership is identical for loaded holdings and SQL aggregates."""

from decimal import Decimal

from domain.accounts.account_policy import excluded_accounts, owner_ratio_case_sql, owner_ratio_lookup


def test_ownership_preserves_zero_and_uses_the_same_fallback_in_python_and_sql():
    config = {"defaults": {"owner_ratio": "0.75", "historical_owner_ratios": {"original": "0.5"}},
              "accounts": {"published": {"owner_ratio": 0}, "fallback": {"owner_ratio": None}}}
    ratio = owner_ratio_lookup(config)
    assert ratio("published") == Decimal(0)
    assert ratio("original") == Decimal("0.5")
    assert ratio("fallback") == ratio("unknown") == Decimal("0.75")
    sql, params = owner_ratio_case_sql(config)("h")
    assert sql.count("WHEN h.account_code") == 2
    assert params == ["original", 0.5, "published", 0.0, 0.75]


def test_exclusions_can_be_read_repeatedly_without_mutating_config():
    config = {"excluded_accounts": ["alpha"], "defaults": {"excluded_accounts": [" beta ", "alpha"]}}
    assert excluded_accounts(config) == excluded_accounts(config) == {"alpha", "beta"}
    assert config["excluded_accounts"] == ["alpha"]
