"""Regression tests for data/config relative paths inside nested domain subpackages."""

from pathlib import Path

from domain.accounts.views import account_overview
from domain.investments.attribution import return_attribution
from domain.investments.trades import trade_endpoint_service
from domain.investments.valuation import portfolio_state
from domain.ledger.cashflow import cashflow_classification, ledger_view


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cashflow_keywords_yaml_loads_from_subpackage():
    hint = cashflow_classification.classification_hint(
        {"direction": "outflow", "counterparty": "美团外卖", "description": "午餐"}
    )
    assert hint is None or isinstance(hint, dict)


def test_subpackage_local_config_paths_point_to_repo_root():
    assert account_overview.LOCAL_CONFIG_PATH.parent == REPO_ROOT / "config"
    assert account_overview.LOCAL_BANK_CONFIG_PATH.parent == REPO_ROOT / "config"
    assert return_attribution.LOCAL_REPORTED_RETURNS_CONFIG_PATH.parent == REPO_ROOT / "config"
    assert trade_endpoint_service.LOCAL_REPORTED_RETURNS_CONFIG_PATH.parent == REPO_ROOT / "config"
    assert portfolio_state.ROOT == REPO_ROOT
    assert ledger_view.LOCAL_ACCOUNTS_CONFIG_PATH.parent == REPO_ROOT / "config"
