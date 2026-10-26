"""Read-only ownership join for the opt-in, full-account IBKR close."""
from __future__ import annotations

from contextlib import closing
from datetime import date
from decimal import Context, Decimal, localcontext
import math
import re
from typing import Any, Callable

from psycopg2.extras import RealDictCursor

from services import portfolio_state


def _amount(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("missing_amount")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("invalid_amount")
    return number


def _unconfirmed(reason: str) -> dict[str, Any]:
    return {"status": "unconfirmed", "account_code": "ibkr", "reasons": [reason]}


def bind_portfolio_account(performance: dict[str, Any], balances: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind existing canonical NAV and scale other broker amounts exactly once."""
    try:
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            meta = performance["daily_close"]
            rows = [row for row in balances if row.get("account_code") == "ibkr"]
            if len(rows) != 1:
                raise ValueError("missing_or_ambiguous_canonical_account")
            row = rows[0]
            proof = row["daily_close"]
            day = performance["as_of"]
            if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                    or any(str(value) != day for value in (meta["as_of"], row["as_of"], proof["as_of"],
                                                           proof["requested_as_of"]))
                    or proof["carried"] is not False):
                raise ValueError("closing_date_mismatch")
            if (performance.get("status") != "ok" or meta["status"] != "ok"
                    or meta["fx_status"] != "available" or meta["nav_basis"] != "official_nav_total"
                    or proof["status"] != "ok" or not row.get("include_in_nav", True)
                    or row["truth_source"] != "ibkr_flex"
                    or proof["nav_basis"] != "official_base_nav_at_internal_historical_fx"):
                raise ValueError("unproven_canonical_account")
            if (not isinstance(meta["source_account_id"], str) or not meta["source_account_id"].strip()
                    or re.fullmatch(r"[0-9a-f]{64}", meta["source_sha256"]) is None
                    or any(proof[field] != meta[field] for field in ("source_account_id", "source_sha256"))
                    or proof["identity_evidence"]["status"] != "ok"
                    or proof["identity_evidence"]["as_of"] != day):
                raise ValueError("source_identity_mismatch")
            base = performance["base_currency"]
            fx = meta["fx_evidence"]
            rate = _amount(proof["fx_rate"])
            if (not isinstance(base, str) or re.fullmatch(r"[A-Z]{3}", base) is None
                    or row["currency"] != "CNY" or proof["valuation_currency"] != "CNY"
                    or row["base_currency"] != base or proof["source_base_currency"] != base
                    or fx != proof["fx_evidence"] or fx["status"] != "ok"
                    or fx["as_of"] != day or fx["requested_as_of"] != day
                    or fx["currency"] != base or fx["base_currency"] != "CNY"
                    or not isinstance(fx["source"], str) or not fx["source"].strip()
                    or rate <= 0 or rate != _amount(fx["rate"])
                    or rate != _amount(performance["fx_rate_to_cny"])):
                raise ValueError("historical_fx_mismatch")
            owner = _amount(proof["owner_ratio"])
            nav = _amount(row["amount_cny"])
            if owner < 0 or abs(nav - _amount(performance["net_liquidation_cny"]) * owner) > Decimal("1e-8"):
                raise ValueError("owned_nav_mismatch")
            amounts = {field: float(_amount(performance[field]) * owner) for field in
                       ("total_pnl_cny", "return_capital_cny", "cash_cny", "position_value_cny")}
            amounts.update(net_liquidation_cny=float(nav), owner_ratio=float(owner))
            ratio = performance["return_ratio"]
            ratio = float(_amount(ratio)) if ratio is not None else None
            if not all(math.isfinite(value) for value in amounts.values()) or (ratio is not None and not math.isfinite(ratio)):
                raise ValueError("invalid_amount")
            return {"status": "ok", "account_code": "ibkr", "ownership_basis": "owner_adjusted",
                    "currency": "CNY", "as_of": day, "source_account_id": meta["source_account_id"],
                    "source_sha256": meta["source_sha256"], **amounts,
                    "return_ratio": ratio if owner > 0 else None}
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return _unconfirmed(str(exc) if isinstance(exc, ValueError) else "incomplete_portfolio_proof")


def read_bound_performance(performance: dict[str, Any], *, enabled: bool = False,
                           conn_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Default-off public adapter; never alter full-account amounts or ratios."""
    meta = performance.get("daily_close")
    if not enabled or not isinstance(meta, dict) or meta.get("status") != "ok":
        return performance
    try:
        cfg = portfolio_state._load_accounts_config()
        account = (cfg.get("accounts") or {}).get("ibkr") or {}
        if account.get("truth_source") != "ibkr_flex":
            raise ValueError("missing_portfolio_account_configuration")
        with closing((conn_factory or portfolio_state.get_conn)()) as conn:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                balances = portfolio_state.load_account_balances(cur, "CNY", cfg)
                binding = bind_portfolio_account(performance, balances)
    except Exception:
        binding = _unconfirmed("portfolio_binding_unavailable")
    return {**performance, "portfolio_account": binding,
            "status": performance.get("status") if binding["status"] == "ok" else "unavailable"}
