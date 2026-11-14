"""Apply the agreed net-contribution basis to the existing account projection."""

from decimal import Decimal, InvalidOperation
from typing import Any


def resolve_account_ownership(config: dict[str, Any]) -> dict[str, Any]:
    accounts = config.get("accounts") or {}
    projected = dict(accounts)
    for account_code, profile in accounts.items():
        if not profile or profile.get("ownership_basis") != "net_contributions":
            continue
        try:
            total = Decimal(str(profile.get("capital_base")))
            personal = Decimal(str(profile.get("personal_capital")))
        except InvalidOperation as exc:
            raise ValueError(f"{account_code}: invalid net contributions") from exc
        if not (total.is_finite() and personal.is_finite() and total > 0 and 0 <= personal <= total):
            raise ValueError(f"{account_code}: invalid net contributions")
        projected[account_code] = {**profile, "owner_ratio": personal / total}
    return {**config, "accounts": projected} if projected != accounts else config
