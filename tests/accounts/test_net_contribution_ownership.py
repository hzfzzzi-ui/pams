from decimal import Decimal

import pytest

import main
from domain.accounts.account_ownership import resolve_account_ownership
from domain.investments import portfolio_state
from domain.ledger import ledger_view
from endpoints import accounts


def test_config_readers_use_net_capital_ratio_for_assets_and_ledger(tmp_path, monkeypatch):
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "accounts:\n"
        "  shared:\n"
        "    ownership_basis: net_contributions\n"
        "    capital_base: 1200\n"
        "    personal_capital: 700\n"
        "    owner_ratio: 1\n"
        "  ordinary:\n"
        "    owner_ratio: 0.8\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(portfolio_state, "CONFIG_PATH", path)
    monkeypatch.setattr(ledger_view, "ACCOUNTS_CONFIG_PATH", path)
    monkeypatch.setattr(accounts, "CONFIG_PATH", path)
    monkeypatch.setattr(main, "ACCOUNTS_CONFIG_PATH", path)

    expected_ratio = Decimal(700) / Decimal(1200)
    portfolio_config = portfolio_state._load_accounts_config()
    assert portfolio_state._owner_ratio(portfolio_config, "shared") == expected_ratio
    assert portfolio_state._owner_ratio(portfolio_config, "ordinary") == Decimal("0.8")
    assert accounts._load_accounts_config()["accounts"]["shared"]["owner_ratio"] == expected_ratio
    assert main._account_owner_ratio("shared") == expected_ratio

    capital = ledger_view._account_capital_config(ledger_view._load_accounts_config(), "shared")
    assert capital == {"owner_ratio": 700 / 1200, "personal_capital": 700, "capital_base": 1200}


@pytest.mark.parametrize(("personal", "total"), [(1300, 1200), (None, 1200), (0, 0)])
def test_invalid_net_capital_cannot_fall_back_to_full_ownership(personal, total):
    with pytest.raises(ValueError, match="shared: invalid net contributions"):
        resolve_account_ownership({
            "accounts": {"shared": {
                "ownership_basis": "net_contributions",
                "personal_capital": personal,
                "capital_base": total,
                "owner_ratio": 1,
            }},
        })


def test_zero_net_contribution_has_zero_ledger_ownership():
    config = resolve_account_ownership({
        "accounts": {"shared": {"ownership_basis": "net_contributions", "personal_capital": 0, "capital_base": 100}},
    })
    assert ledger_view._account_capital_config(config, "shared")["owner_ratio"] == 0
