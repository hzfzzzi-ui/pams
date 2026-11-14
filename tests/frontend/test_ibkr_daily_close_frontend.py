"""Execute the actual field adapter and selected bindings without a browser shim."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
from .holdings_source import read_holdings_source
SOURCE = read_holdings_source(ROOT / "frontend/public", "holdings_categories.js")


def function(name):
    return re.search(r"function " + name + r"\([^\n]*[\s\S]*?(?=\nfunction )", SOURCE).group()


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required; mandatory in final CI")
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("amount", [693, 0, "693"])
def test_overview_uses_backend_cny_instead_of_current_browser_fx(amount):
    extra = function("fxRateForCurrency") + function("accountPrimaryBalanceCny")
    extra += "\nconst overview=" + json.dumps({"accounts": [{"account_code": "ibkr", "base_currency": "USD",
        "daily_close": {"status": "ok"}, "primary_balance": {"amount": 99, "currency": "USD", "amount_cny": amount}}]}) + ";"
    extra += "console.log(JSON.stringify(accountPrimaryBalanceCny(overview,'ibkr',{USD:99})));"
    assert run(extra)["amountCny"] == float(amount)


def test_supplemental_close_configuration_is_explicit_and_disabled_by_default():
    import yaml
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = config["services"]["pams-api"]["environment"]
    assert environment["PAMS_IBKR_DAILY_CLOSE_MANIFEST"] == "${PAMS_IBKR_DAILY_CLOSE_MANIFEST:-}"
    assert environment["IBKR_FLEX_POSITIONS_QUERY_ID"] == "${IBKR_FLEX_POSITIONS_QUERY_ID:-}"
    assert environment["IBKR_FLEX_TOKEN"] == "${IBKR_FLEX_TOKEN:-}"
