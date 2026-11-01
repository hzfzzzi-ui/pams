"""Opt-in, read-only consumption of separately published daily-close evidence.

The supplemental statement never enters the trade/funding import pipeline.
No reader downloads, publishes, repairs facts, or silently activates evidence.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import date
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any

from domain.brokers.ibkr import ibkr_report_state as report_state
from domain.brokers.ibkr.ibkr_daily_close import MAX_XML_BYTES, TOLERANCE, parse_daily_close_evidence, parse_daily_close_series

MANIFEST_ENV = "PAMS_IBKR_DAILY_CLOSE_MANIFEST"
DEV_EVIDENCE_DIR_ENV = "PAMS_DEV_EVIDENCE_DIR"
DEV_EVIDENCE_MARKER = ".pams-development-sandbox"
MAX_MANIFEST_BYTES = 16384
CACHE_MAX_REPORTS = 2
CACHE_MAX_PERIODS = 4
_CACHE: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_CACHE_LOCK = RLock()


def configured() -> bool:
    return bool(os.environ.get(MANIFEST_ENV))


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reasons": [reason]}


def validate_close_day(as_of: date) -> None:
    """Reuse the publication calendar gate without requiring the latest day."""
    report_state.validate_candidate(as_of, as_of, None)


def read_bounded(path: Path, limit: int) -> bytes:
    """Bound the actual read, not just a racy stat before read_bytes()."""
    with path.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError("file_size_limit")
    return content


def _date(value: Any) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid_date")
    return date.fromisoformat(value)


def _verified(evidence: dict[str, Any]) -> tuple[str, str, str]:
    text, account, digest = evidence["xml_text"], evidence["account_id"], evidence["sha256"]
    if (not isinstance(text, str) or len(text) > MAX_XML_BYTES
            or not isinstance(account, str) or not account or account != account.strip()
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise ValueError("invalid_evidence")
    content = text.encode("utf-8")
    if len(content) > MAX_XML_BYTES or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("source_sha256_mismatch")
    as_of = _date(evidence["as_of"])
    if as_of > date.today():
        raise ValueError("future_evidence")
    validate_close_day(as_of)
    return text, account, digest


def _cached_series(text: str, account: str, digest: str) -> dict[str, Any]:
    # No XML text in cache keys or values. Two reports and four small period
    # results per report are retained; cold concurrent reads parse only once.
    key = (digest, account)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = {"series": parse_daily_close_series(text, account_id=account, expected_sha256=digest),
                           "periods": OrderedDict()}
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX_REPORTS:
            _CACHE.popitem(last=False)
        return _CACHE[key]["series"]


def _parse(text: str, account: str, as_of: date, start: date | None, digest: str) -> dict[str, Any]:
    with _CACHE_LOCK:
        series = _cached_series(text, account, digest)
        if start is None:
            return series["daily_closes"].get(as_of.isoformat(), _unavailable("missing_closing_date"))
        periods = _CACHE[(digest, account)]["periods"]
        key = (as_of, start)
        if key not in periods:
            periods[key] = parse_daily_close_evidence(text, account_id=account, as_of=as_of,
                period_start=start, expected_sha256=digest)
        periods.move_to_end(key)
        while len(periods) > CACHE_MAX_PERIODS:
            periods.popitem(last=False)
        return periods[key]


def _path_is_link(path: Path) -> bool:
    """Reject symlinks and Windows directory junctions in development paths."""
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _development_manifest(setting: str) -> Path:
    """Restrict development evidence to a directory bound to the dev database."""
    from domain.imports.isolation import validate_target
    target = {"host": os.environ.get("PGHOST"), "dbname": os.environ.get("PGDATABASE")}
    validate_target(target, mode="development")
    directory_setting = os.environ.get(DEV_EVIDENCE_DIR_ENV)
    if not directory_setting:
        raise ValueError("development evidence directory is required")
    root = Path(directory_setting).absolute()
    if any(_path_is_link(path) for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError("invalid development evidence directory")
    root = root.resolve(strict=True)
    marker = root / DEV_EVIDENCE_MARKER
    if _path_is_link(marker) or read_bounded(marker, 128).decode("utf-8").strip() != target["dbname"]:
        raise ValueError("development evidence marker mismatch")
    manifest = Path(setting).absolute()
    if _path_is_link(manifest) or manifest.resolve(strict=True).parent != root:
        raise ValueError("development manifest outside evidence directory")
    return manifest.resolve(strict=True)


def load_evidence() -> dict[str, Any]:
    """Verify the exact bytes on every read; cached parsing is hash-addressed."""
    setting = os.environ.get(MANIFEST_ENV)
    if not setting:
        return _unavailable("not_configured")
    try:
        from domain.imports.isolation import source_mode
        development = source_mode() == "development"
        manifest_path = _development_manifest(setting) if development else Path(setting).resolve(strict=True)
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            return _unavailable("manifest_size_limit")
        manifest = json.loads(read_bounded(manifest_path, MAX_MANIFEST_BYTES).decode("utf-8"))
        if (type(manifest.get("version")) is not int or manifest["version"] != 1
                or manifest.get("purpose") != "ibkr_daily_close"
                or manifest.get("account_code") != "ibkr"):
            return _unavailable("manifest_scope_mismatch")
        name = manifest["source_file"]
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            return _unavailable("invalid_source_path")
        source_path = manifest_path.parent / name
        if development and _path_is_link(source_path):
            return _unavailable("invalid_source_path")
        path = source_path.resolve(strict=True)
        if path.parent != manifest_path.parent or path.stat().st_size > MAX_XML_BYTES:
            return _unavailable("invalid_source_path")
        content = read_bounded(path, MAX_XML_BYTES)
        digest = hashlib.sha256(content).hexdigest()
        if digest != manifest["source_sha256"]:
            return _unavailable("source_sha256_mismatch")
        account = manifest["source_account_id"]
        as_of = _date(manifest["as_of"])
        start = _date(manifest["period_start"])
        if as_of > date.today():
            return _unavailable("future_evidence")
        if start > as_of or name != digest + ".xml":
            return _unavailable("manifest_scope_mismatch")
        text = content.decode("utf-8")
        text, account, digest = _verified({"xml_text": text, "account_id": account, "sha256": digest, "as_of": as_of})
        series = _cached_series(text, account, digest)
        if not series["daily_closes"] or max(series["daily_closes"]) != as_of.isoformat():
            return _unavailable("manifest_closing_date_mismatch")
        close = _parse(text, account, as_of, start, digest)
        if close["status"] != "ok":
            return copy.deepcopy(close)
        return {"status": "ok", "xml_text": text, "account_id": account,
                "as_of": as_of, "period_start": start, "sha256": digest, "source_file": name}
    except report_state.ReportRejected as exc:
        return _unavailable(str(exc))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return _unavailable("evidence_read_failed")


def read_close(*, as_of: date, account_id: str | None = None,
               period_start: date | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = evidence if evidence is not None else load_evidence()
    if evidence.get("status") != "ok":
        return evidence
    try:
        if type(as_of) is not date or (period_start is not None and type(period_start) is not date):
            return _unavailable("invalid_requested_date")
        text, account, digest = _verified(evidence)
        if account_id is not None and account_id != account:
            return _unavailable("account_mismatch")
        if as_of > _date(evidence["as_of"]):
            return _unavailable("missing_closing_date")
        return copy.deepcopy(_parse(text, account, as_of, period_start, digest))
    except report_state.ReportRejected as exc:
        return _unavailable(str(exc))
    except (KeyError, TypeError, ValueError, UnicodeError):
        return _unavailable("invalid_evidence")


def read_series(*, account_id: str | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return strict date results with one hash-addressed bulk parse, never fill gaps."""
    evidence = evidence if evidence is not None else load_evidence()
    if evidence.get("status") != "ok":
        return {**evidence, "daily_closes": {}}
    try:
        text, account, digest = _verified(evidence)
        if account_id is not None and account != account_id:
            return {**_unavailable("account_mismatch"), "daily_closes": {}}
        result = copy.deepcopy(_cached_series(text, account, digest))
        cutoff = _date(evidence["as_of"]).isoformat()
        if not result["daily_closes"] or max(result["daily_closes"]) != cutoff:
            return {**_unavailable("manifest_closing_date_mismatch"), "daily_closes": {}}
        return {**result, "account_id": account, "as_of": cutoff}
    except report_state.ReportRejected as exc:
        return {**_unavailable(str(exc)), "daily_closes": {}}
    except (KeyError, TypeError, ValueError, UnicodeError):
        return {**_unavailable("invalid_evidence"), "daily_closes": {}}


def historical_rate(cur, *, currency: str, base_currency: str, as_of: date) -> Decimal | None:
    """Compatibility wrapper; only an exact-day, finite, proven rate is usable."""
    result = historical_fx(cur, currency=currency, base_currency=base_currency, as_of=as_of)
    return result.get("rate") if result["status"] == "ok" else None


def historical_fx(cur, *, currency: str, base_currency: str, as_of: date) -> dict[str, Any]:
    """Expose the observed date/source; no undocumented holiday/staleness fallback."""
    if type(as_of) is not date or any(not isinstance(c, str) or re.fullmatch(r"[A-Z]{3}", c) is None
                                      for c in (currency, base_currency)):
        return _unavailable("invalid_fx_scope")
    if currency == base_currency:
        return {"status": "ok", "rate": Decimal(1), "as_of": as_of.isoformat(), "source": "currency_identity",
                "requested_as_of": as_of.isoformat(), "age_days": 0,
                "currency": currency, "base_currency": base_currency}
    cur.execute("""SELECT rate, as_of, source FROM pams_fx_rates_daily
        WHERE base_currency = %s AND quote_currency = %s AND as_of <= %s
        ORDER BY as_of DESC LIMIT 1""", (base_currency, currency, as_of))
    row = cur.fetchone()
    if not row:
        return _unavailable("missing_historical_fx")
    try:
        value = Decimal(str(row["rate"] if isinstance(row, dict) else row[0]))
        observed = _date(row["as_of"] if isinstance(row, dict) else row[1])
        source = row["source"] if isinstance(row, dict) else row[2]
        metadata = {"as_of": observed.isoformat(), "requested_as_of": as_of.isoformat(),
                    "source": source, "age_days": (as_of - observed).days,
                    "currency": currency, "base_currency": base_currency}
        if not value.is_finite() or value <= 0 or not isinstance(source, str) or not source:
            return {**_unavailable("invalid_historical_fx"), **metadata}
        if observed != as_of:
            return {**_unavailable("expired_historical_fx" if observed < as_of else "future_historical_fx"), **metadata}
        return {"status": "ok", "rate": value, **metadata}
    except (InvalidOperation, KeyError, IndexError, TypeError, ValueError):
        return _unavailable("invalid_historical_fx")


def _amount(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("missing_or_invalid_amount")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number.adjusted()) > 64:
            raise ValueError("invalid_amount")
        return number
    except InvalidOperation as exc:
        raise ValueError("invalid_amount") from exc


def _failed_overlay(result: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    out = dict(result)
    out["daily_close"] = {"status": "unavailable", "reasons": reasons or ["unproven_daily_close"],
                          "value_basis": "legacy_cash_plus_positions_unconfirmed"}
    return out


def overlay_performance(result: dict[str, Any], *, account_id: str,
                        close: dict[str, Any], fx_rate: Decimal | None = None,
                        fx_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replace only proven account-close/period fields, keeping position semantics."""
    if (close.get("status") != "ok" or close.get("account_id") != account_id
            or close.get("as_of") != result.get("as_of")
            or close.get("base_currency") != result.get("base_currency")):
        return _failed_overlay(result, close.get("reasons") or ["scope_mismatch"])
    try:
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            return _overlay_proven(result, close, fx_rate, fx_evidence)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return _failed_overlay(result, ["invalid_close_or_subject_amounts"])


def _overlay_proven(result: dict[str, Any], close: dict[str, Any], fx_rate: Decimal | None,
                    fx_evidence: dict[str, Any] | None) -> dict[str, Any]:
    nav = _amount(close["nav_base"])
    day = _date(close["as_of"])
    if day > date.today():
        return _failed_overlay(result, ["future_evidence"])
    for field in ("cash_base", "position_value_base"):
        if abs(_amount(close[field]) - _amount(result[field])) > TOLERANCE:
            return _failed_overlay(result, ["production_close_components_mismatch"])
    component_fields = ("cash_base", "position_value_base", "interest_accrual_base",
                        "dividend_accrual_base", "other_nav_components_base")
    if abs(sum((_amount(close[f]) for f in component_fields), Decimal(0)) - nav) > TOLERANCE:
        return _failed_overlay(result, ["invalid_close_component_sum"])
    if (not isinstance(close.get("source_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", close["source_sha256"]) is None):
        return _failed_overlay(result, ["invalid_close_provenance"])
    period = close.get("period")
    if not isinstance(period, dict):
        return _failed_overlay(result, ["missing_period_evidence"])
    if (period.get("basis") != "nav_change_less_external_flows"
            or _date(period["to_date"]) != day or _date(period["from_date"]) > day):
        return _failed_overlay(result, ["invalid_period_scope"])
    opening, closing = _amount(period["opening_nav_base"]), _amount(period["closing_nav_base"])
    flow, pnl = _amount(period["net_external_flow_base"]), _amount(period["pnl_base"])
    if abs(closing - nav) > TOLERANCE or abs(closing - opening - flow - pnl) > TOLERANCE:
        return _failed_overlay(result, ["invalid_period_bridge"])
    unrealized, realized = _amount(result["unrealized_pnl_base"]), _amount(result["realized_pnl_base"])
    gross, negative_cash = _amount(result["gross_exposure_base"]), _amount(result["negative_cash_base"])
    if gross < 0 or negative_cash < 0:
        return _failed_overlay(result, ["invalid_subject_exposure"])
    out = dict(result)
    out["status"] = "ok"
    out["net_liquidation_base"] = float(nav)
    out["interest_accrual_base"] = float(close["interest_accrual_base"])
    out["dividend_accrual_base"] = float(close["dividend_accrual_base"])
    out["other_nav_components_base"] = float(close["other_nav_components_base"])
    capital = opening + flow
    out["return_capital_base"] = float(capital)
    out["net_deposit_base"] = float(flow)
    out["total_pnl_base"] = float(pnl)
    out["return_ratio"] = float(pnl / capital) if capital > 0 else None
    out["other_pnl_base"] = float(pnl - unrealized - realized)
    out["leverage_ratio"] = float(gross / nav) if nav > 0 else None
    out["negative_cash_ratio"] = float(negative_cash / nav) if nav > 0 else None
    # Old CNY numbers must not survive a USD correction or a missing historical FX.
    for key in list(out):
        if key.endswith("_cny") or key == "fx_rate_to_cny":
            out.pop(key)
    fx = dict(fx_evidence) if fx_evidence is not None else _unavailable("missing_fx_metadata")
    usable_fx = None
    if fx.get("status") == "ok":
        try:
            rate = _amount(fx["rate"])
            if (rate <= 0 or _date(fx["as_of"]) != day
                    or not isinstance(fx.get("source"), str) or not fx["source"].strip()
                    or _date(fx["requested_as_of"]) != day or fx.get("currency") != close["base_currency"]
                    or fx.get("base_currency") != "CNY"):
                fx = {**fx, **_unavailable("invalid_or_expired_fx_metadata")}
            elif fx_rate is not None and _amount(fx_rate) != rate:
                fx = {**fx, **_unavailable("conflicting_fx_rate")}
            else:
                usable_fx = rate
        except (KeyError, TypeError, ValueError):
            fx = _unavailable("invalid_fx_metadata")
    if usable_fx is not None:
        out["fx_rate_to_cny"] = float(usable_fx)
        for key, value in list(out.items()):
            if key.endswith("_base") and isinstance(value, (float, int, Decimal)):
                out[key[:-5] + "_cny"] = float(_amount(value) * usable_fx)
    out["daily_close"] = {"status": "ok", "nav_basis": "official_nav_total",
        "source_account_id": close["account_id"],
        "value_basis": "official_nav_total_and_proven_period",
        "return_ratio_basis": "period_pnl_over_opening_nav_plus_net_external_flows_not_twr_or_mwr",
        "return_ratio_status": "defined" if capital > 0 else "undefined",
        "source_sha256": close["source_sha256"], "as_of": close["as_of"],
        "period": {k: str(v) if isinstance(v, Decimal) else v for k, v in period.items()} if period else None,
        "cny_basis": "closing_base_value_at_internal_historical_fx_not_cny_period_return",
        "fx_status": "available" if usable_fx is not None else "unavailable",
        "fx_evidence": {k: str(v) if isinstance(v, Decimal) else v for k, v in fx.items()}}
    return out
