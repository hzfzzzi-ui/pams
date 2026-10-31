from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import yaml

from domain.risk.core.risk_engine import ensure_positions


REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CONCENTRATION_MAP_PATH = REPO_ROOT / "config" / "risk_concentration.yaml"

ClassifierLookup = Mapping[str, str] | Callable[[str], str | None] | None


def normalize_symbol(symbol: Any) -> str:
    """Return a stable uppercase symbol key for concentration configuration lookup."""
    text = str(symbol or "").strip()
    if not text:
        return ""
    return text.replace(" ", "").upper()


def load_concentration_map(path: str | Path = DEFAULT_CONCENTRATION_MAP_PATH) -> dict[str, Any]:
    """Load industry and style concentration mappings from YAML."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def _symbol_candidates(row: Mapping[str, Any]) -> list[str]:
    values = [
        row.get("symbol"),
        row.get("asset_id"),
        row.get("asset_name"),
    ]
    candidates: list[str] = []
    for value in values:
        key = normalize_symbol(value)
        if not key:
            continue
        candidates.append(key)
        if "." in key:
            candidates.append(key.split(".", 1)[0])
        if key.startswith(("SH", "SZ")) and len(key) == 8:
            candidates.append(key[2:])
    return list(dict.fromkeys(candidates))


def _lookup_db_industry(candidates: list[str], lookup: ClassifierLookup) -> str | None:
    if lookup is None:
        return None
    if callable(lookup):
        for candidate in candidates:
            value = lookup(candidate)
            if value:
                return str(value)
        return None
    for candidate in candidates:
        value = lookup.get(candidate) or lookup.get(candidate.lower())
        if value:
            return str(value)
    return None


def _coerce_exposure_map(value: Any, *, normalize: bool) -> list[tuple[str, float]]:
    if not value:
        return []
    items: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        for label, weight in value.items():
            text = str(label or "").strip()
            if not text:
                continue
            try:
                weight_float = float(weight)
            except (TypeError, ValueError):
                weight_float = 0.0
            if weight_float > 0:
                items.append((text, weight_float))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                text = str(item.get("label") or item.get("name") or "").strip()
                weight_float = float(item.get("weight") or 0.0)
            else:
                text = str(item or "").strip()
                weight_float = 1.0
            if text and weight_float > 0:
                items.append((text, weight_float))
    else:
        text = str(value or "").strip()
        if text:
            items.append((text, 1.0))

    if normalize:
        total = sum(weight for _, weight in items)
        if total > 0:
            items = [(label, weight / total) for label, weight in items]
    return items


def classify_position(
    row: Mapping[str, Any],
    concentration_map: dict[str, Any],
    db_industry_lookup: ClassifierLookup = None,
) -> dict[str, str]:
    """Classify one position into industry and style buckets.

    Symbol-level YAML mappings are preferred because funds and overseas holdings
    usually need manual risk buckets. A-share database industry labels are used
    as a fallback for industry only; style falls back to asset-type defaults.
    """
    symbols = concentration_map.get("symbols", {}) or {}
    asset_types = concentration_map.get("asset_types", {}) or {}
    default = concentration_map.get("default", {}) or {}

    selected: dict[str, Any] = {}
    candidates = _symbol_candidates(row)
    for candidate in candidates:
        selected = symbols.get(candidate) or symbols.get(candidate.lower()) or {}
        if selected:
            break

    asset_type = str(row.get("asset_type") or "").lower()
    type_default = asset_types.get(asset_type, {}) or {}

    industry_exposures = _coerce_exposure_map(selected.get("industry_exposures"), normalize=True)
    style_exposures = _coerce_exposure_map(selected.get("style_exposures"), normalize=False)
    industry = selected.get("industry") or (industry_exposures[0][0] if industry_exposures else None) or _lookup_db_industry(candidates, db_industry_lookup)
    style = selected.get("style") or (style_exposures[0][0] if style_exposures else None)
    return {
        "industry": str(industry or type_default.get("industry") or default.get("industry") or "未分类"),
        "style": str(style or type_default.get("style") or default.get("style") or "未分类"),
    }


def classify_position_exposures(
    row: Mapping[str, Any],
    concentration_map: dict[str, Any],
    db_industry_lookup: ClassifierLookup = None,
) -> dict[str, list[tuple[str, float]]]:
    """Classify one position into weighted industry and style exposures.

    Industry is treated as a single axis, so explicit fund/ETF look-through
    weights are normalized to 100%. Style is a multi-tag exposure surface:
    a holding can simultaneously contribute to growth, size, region, and theme
    buckets, so style weights are not normalized.
    """

    symbols = concentration_map.get("symbols", {}) or {}
    asset_types = concentration_map.get("asset_types", {}) or {}
    default = concentration_map.get("default", {}) or {}

    selected: dict[str, Any] = {}
    candidates = _symbol_candidates(row)
    for candidate in candidates:
        selected = symbols.get(candidate) or symbols.get(candidate.lower()) or {}
        if selected:
            break

    asset_type = str(row.get("asset_type") or "").lower()
    type_default = asset_types.get(asset_type, {}) or {}
    primary = classify_position(row, concentration_map, db_industry_lookup)

    industry = _coerce_exposure_map(selected.get("industry_exposures"), normalize=True)
    if not industry:
        industry = _coerce_exposure_map(selected.get("industry"), normalize=True)
    if not industry:
        db_industry = _lookup_db_industry(candidates, db_industry_lookup)
        industry = _coerce_exposure_map(db_industry, normalize=True)
    if not industry:
        industry = _coerce_exposure_map(type_default.get("industry_exposures"), normalize=True)
    if not industry:
        industry = [(primary.get("industry") or default.get("industry") or "未分类", 1.0)]

    style = _coerce_exposure_map(selected.get("style_exposures"), normalize=False)
    if not style:
        style = _coerce_exposure_map(selected.get("style"), normalize=False)
    if not style:
        style = _coerce_exposure_map(type_default.get("style_exposures"), normalize=False)
    if not style:
        style = [(primary.get("style") or default.get("style") or "未分类", 1.0)]

    return {"industry": industry, "style": style}


def _included_positions(df: pd.DataFrame) -> pd.DataFrame:
    market_like = ~(
        (df["source_type"].astype(str) == "account_center")
        & (df["asset_type"].astype(str).str.lower() == "manual_asset")
    )
    return df.loc[
        ~df["is_cash"]
        & ~df["is_liability"]
        & (df["market_value_cny"] > 0)
        & market_like
    ].copy()


def _aggregate(rows: list[dict[str, Any]], group_key: str, nav: float) -> list[dict[str, Any]]:
    if not rows:
        return []
    df = pd.DataFrame(rows)
    out: list[dict[str, Any]] = []
    for label, group in df.groupby(group_key, dropna=False):
        group = group.sort_values("exposure_cny", ascending=False)
        exposure = float(group["exposure_cny"].sum())
        top_positions = [
            {
                "asset_id": str(r["asset_id"]),
                "asset_name": str(r["asset_name"]),
                "exposure_cny": float(r["exposure_cny"]),
                "exposure_ratio": float(r["exposure_cny"]) / nav if nav > 0 else 0.0,
                "weight": float(r.get("classification_weight") or 1.0),
            }
            for r in group.head(4).to_dict("records")
        ]
        out.append(
            {
                "label": str(label),
                "exposure_cny": exposure,
                "exposure_ratio": exposure / nav if nav > 0 else 0.0,
                "position_count": int(group["asset_id"].nunique()),
                "top_positions": top_positions,
                "top_position_names": "、".join(p["asset_name"] for p in top_positions),
            }
        )
    return sorted(out, key=lambda r: abs(r["exposure_cny"]), reverse=True)


def concentration_exposure(
    positions: pd.DataFrame,
    nav: float,
    concentration_map: dict[str, Any],
    db_industry_lookup: ClassifierLookup = None,
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Calculate industry and style concentration exposure from current holdings.

    The calculation excludes cash, liabilities, and synthetic account-center
    manual balances so that the result reflects investable risk concentration.
    Ratios are measured against total NAV, matching the rest of the risk page.
    """
    df = _included_positions(ensure_positions(positions, risk_limits))
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        record = row.to_dict()
        classified = classify_position_exposures(record, concentration_map, db_industry_lookup)
        asset_id = record.get("asset_id") or record.get("symbol") or record.get("asset_name") or ""
        asset_name = record.get("asset_name") or record.get("asset_id") or record.get("symbol") or ""
        base_exposure = float(record.get("market_value_cny") or 0.0)
        for label, weight in classified["industry"]:
            rows.append(
                {
                    "kind": "industry",
                    "industry": label,
                    "style": "",
                    "asset_id": asset_id,
                    "asset_name": asset_name,
                    "classification_weight": weight,
                    "exposure_cny": base_exposure * weight,
                }
            )
        for label, weight in classified["style"]:
            rows.append(
                {
                    "kind": "style",
                    "industry": "",
                    "style": label,
                    "asset_id": asset_id,
                    "asset_name": asset_name,
                    "classification_weight": weight,
                    "exposure_cny": base_exposure * weight,
                }
            )

    return {
        "industry": _aggregate([r for r in rows if r["kind"] == "industry"], "industry", nav),
        "style": _aggregate([r for r in rows if r["kind"] == "style"], "style", nav),
    }
