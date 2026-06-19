from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


NUMERIC_METRICS = [
    "total_return",
    "max_drawdown",
    "cagr",
    "sharpe",
    "calmar",
    "trades",
    "turnover",
    "top10_profit_concentration",
]

NUMERIC_FUNNEL = ["raw_signal", "blocked", "orders", "skipped", "closed_trades"]

NUMERIC_TRIGGERS = [
    "gate_on_rate",
    "mf_rank_applied_rate",
    "trend_slope_block_count",
    "atr_stop_exit_count",
    "atr_trail_exit_count",
    "atr_trail_update_count",
]


def _safe_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except Exception:
        return None
    if math.isnan(n) or math.isinf(n):
        return None
    return n


def _safe_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _as_status(v: Any) -> str:
    s = str(v or "").strip().upper()
    return s if s in {"OK", "FAILED", "CANCELED"} else ("FAILED" if s else "OK")


def sanitize_json(obj: Any) -> Any:
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, list):
        return [sanitize_json(x) for x in obj]
    if isinstance(obj, tuple):
        return [sanitize_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): sanitize_json(v) for k, v in obj.items()}
    return str(obj)


def _parse_ps_row(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not (text.startswith("@{") and text.endswith("}")):
        return None
    out: dict[str, Any] = {}
    for part in text[2:-1].split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[str(k).strip()] = str(v).strip()
    return out


def _jsonify_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, str):
        parsed = _parse_ps_row(row)
        if parsed is not None:
            return parsed
        return {"group_id": row, "status": "FAILED", "error_message": "ROW_PARSE_FAILED"}
    return {"group_id": str(row), "status": "FAILED", "error_message": "ROW_TYPE_INVALID"}


def infer_tier(start_date: str | None, end_date: str | None, universe_size: int | None, trades: int | None) -> str:
    years = 0.0
    try:
        if start_date and end_date:
            s = date.fromisoformat(str(start_date))
            e = date.fromisoformat(str(end_date))
            years = max((e - s).days / 365.25, 0.0)
    except Exception:
        years = 0.0
    u = int(universe_size or 0)
    t = int(trades or 0)
    if years >= 5.0 and u >= 700 and t >= 1000:
        return "T3"
    if years >= 3.0 and u >= 300 and t >= 300:
        return "T2"
    if (1.0 <= years < 3.0) or (50 <= u < 300) or (50 <= t < 300):
        return "T1"
    return "T0"


def _to_rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _resolve_runtime_path(raw: str | None, bt_root: Path) -> Path | None:
    if not raw:
        return None
    p = Path(str(raw))
    if p.exists():
        return p
    text = str(raw).strip()
    if text.startswith("/app/backtest_V2"):
        mapped = bt_root / text[len("/app/backtest_V2") :].lstrip("/\\")
        if mapped.exists():
            return mapped
    return p if p.exists() else None


def _discover_candidates_by_scan(results_dir: Path, group_id: str) -> list[Path]:
    gid = str(group_id or "").strip().lower()
    if not gid:
        return []
    candidates: list[Path] = []
    for pattern in ("*/", "*/*/"):
        for p in results_dir.glob(pattern):
            if not p.is_dir():
                continue
            has_marker = (p / "perf.json").exists() or (p / "report_summary.json").exists()
            if not has_marker:
                continue
            tag = p.name.lower()
            if gid in tag or tag in gid:
                candidates.append(p)
    # deterministic order
    return sorted(set(candidates), key=lambda x: str(x).lower())


def discover_group_dirs(
    *,
    results_dir: Path,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    group_ids: list[str],
    bt_root: Path | None = None,
) -> dict[str, Path]:
    bt_root = bt_root or results_dir
    row_by_gid = {str((r or {}).get("group_id") or ""): dict(r or {}) for r in rows}
    run_by_gid = {str((r or {}).get("group_id") or ""): dict(r or {}) for r in runs}
    mapping: dict[str, Path] = {}

    for gid in group_ids:
        # 1) row.group_dir
        row = row_by_gid.get(gid, {})
        row_group_dir = _resolve_runtime_path(str(row.get("group_dir") or ""), bt_root)
        if row_group_dir and row_group_dir.exists():
            mapping[gid] = row_group_dir
            continue

        # 2) runs[i].results_dir
        run = run_by_gid.get(gid, {})
        run_dir = _resolve_runtime_path(str(run.get("results_dir") or ""), bt_root)
        if run_dir and run_dir.exists():
            mapping[gid] = run_dir
            continue

        # 3) {results_dir}/groups/{group_id}
        p3 = results_dir / "groups" / gid
        if p3.exists():
            mapping[gid] = p3
            continue

        # 4) {results_dir}/{group_id}
        p4 = results_dir / gid
        if p4.exists():
            mapping[gid] = p4
            continue

        # 5) deterministic fallback scan depth 1/2
        fallback = _discover_candidates_by_scan(results_dir, gid)
        if len(fallback) == 1:
            mapping[gid] = fallback[0]
            continue
        if len(fallback) > 1:
            raise ValueError(
                f"ambiguous group_dir for {gid}; candidates={ [str(x) for x in fallback] }"
            )

        # final default path for output
        mapping[gid] = p3
    return mapping


def normalize_row(
    row: dict[str, Any],
    *,
    start_date: str | None,
    end_date: str | None,
    universe_size: int | None,
    universe_limit: int | None,
) -> dict[str, Any]:
    out = dict(row)
    out["group_id"] = str(out.get("group_id") or "")
    out["status"] = _as_status(out.get("status"))

    # metrics
    out["total_return"] = _safe_float(out.get("total_return"))
    out["max_drawdown"] = _safe_float(out.get("max_drawdown"))
    out["cagr"] = _safe_float(out.get("cagr"))
    out["sharpe"] = _safe_float(out.get("sharpe"))
    out["calmar"] = _safe_float(out.get("calmar"))
    out["trades"] = _safe_int(out.get("trades") or out.get("trades_count"))
    out["turnover"] = _safe_float(out.get("turnover") if out.get("turnover") is not None else out.get("turnover_avg"))
    out["top10_profit_concentration"] = _safe_float(
        out.get("top10_profit_concentration")
        if out.get("top10_profit_concentration") is not None
        else out.get("top10_profit_concentration_pct")
    )

    # funnel
    out["raw_signal"] = _safe_int(out.get("raw_signal") if out.get("raw_signal") is not None else out.get("raw_signals_total"))
    out["blocked"] = _safe_int(out.get("blocked") if out.get("blocked") is not None else out.get("blocked_signals_total"))
    out["orders"] = _safe_int(
        out.get("orders")
        if out.get("orders") is not None
        else out.get("tradable_orders_total")
        if out.get("tradable_orders_total") is not None
        else out.get("orders_total")
    )
    out["skipped"] = _safe_int(
        out.get("skipped")
        if out.get("skipped") is not None
        else out.get("skipped_orders_total")
        if out.get("skipped_orders_total") is not None
        else out.get("skipped_orders")
    )
    out["closed_trades"] = _safe_int(out.get("closed_trades"))

    # triggers
    out["gate_on_rate"] = _safe_float(out.get("gate_on_rate"))
    out["mf_rank_applied_rate"] = _safe_float(out.get("mf_rank_applied_rate"))
    out["trend_slope_block_count"] = _safe_int(out.get("trend_slope_block_count"))
    out["atr_stop_exit_count"] = _safe_int(out.get("atr_stop_exit_count"))
    out["atr_trail_exit_count"] = _safe_int(out.get("atr_trail_exit_count"))
    out["atr_trail_update_count"] = _safe_int(out.get("atr_trail_update_count"))

    inferred_universe = _safe_int(out.get("universe_filtered_count"))
    final_universe = inferred_universe if inferred_universe is not None else (universe_size if universe_size is not None else universe_limit)
    out["tier"] = infer_tier(start_date, end_date, final_universe, out.get("trades"))
    out["group_dir"] = str(out.get("group_dir") or "")
    out["group_files"] = dict(out.get("group_files") or {})

    # enforce mandatory numeric keys
    for key in NUMERIC_METRICS + NUMERIC_FUNNEL + NUMERIC_TRIGGERS:
        out.setdefault(key, None)
    return sanitize_json(out)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _extract_perf(row: dict[str, Any], group_dir: Path, missing_fields: list[str]) -> dict[str, Any]:
    summary = _load_json(group_dir / "report_summary.json")
    report = _load_json(group_dir / "report.json")

    if isinstance(summary.get("kpis"), dict):
        k = summary["kpis"]
        return {
            "total_return": _safe_float(k.get("total_return")),
            "max_drawdown": _safe_float(k.get("max_drawdown")),
            "cagr": _safe_float(k.get("cagr")),
            "sharpe": _safe_float(k.get("sharpe")),
            "trades": _safe_int(k.get("trades") if k.get("trades") is not None else k.get("trades_count")),
        }

    if isinstance(report.get("summary"), dict):
        s = report["summary"]
        return {
            "total_return": _safe_float(s.get("total_return")),
            "max_drawdown": _safe_float(s.get("max_drawdown")),
            "cagr": _safe_float(s.get("cagr")),
            "sharpe": _safe_float(s.get("sharpe")),
            "trades": _safe_int(s.get("trades")),
        }

    missing_fields.append("missing_perf_source")
    return {
        "total_return": _safe_float(row.get("total_return")),
        "max_drawdown": _safe_float(row.get("max_drawdown")),
        "cagr": _safe_float(row.get("cagr")),
        "sharpe": _safe_float(row.get("sharpe")),
        "trades": _safe_int(row.get("trades")),
    }


def _write_nav_csv(group_dir: Path, missing_fields: list[str]) -> Path:
    nav_path = group_dir / "nav.csv"
    if nav_path.exists():
        return nav_path

    source = group_dir / "equity_curve.csv"
    if source.exists():
        try:
            df = pd.read_csv(source)
            value_col = None
            for col in ["nav", "strategy_nav", "equity", "equity_curve", "portfolio_value"]:
                if col in df.columns:
                    value_col = col
                    break
            date_col = "date" if "date" in df.columns else ("dt" if "dt" in df.columns else None)
            if value_col is not None:
                if date_col is None:
                    dates = pd.to_datetime(df.index, errors="coerce").strftime("%Y-%m-%d")
                    out = pd.DataFrame({"date": dates, "nav": pd.to_numeric(df[value_col], errors="coerce")})
                else:
                    dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
                    out = pd.DataFrame({"date": dates, "nav": pd.to_numeric(df[value_col], errors="coerce")})
                out = out.dropna(subset=["date", "nav"])
                if not out.empty:
                    out.to_csv(nav_path, index=False)
                    return nav_path
        except Exception:
            pass

    missing_fields.append("missing_nav_source")
    nav_path.write_text("date,nav\n", encoding="utf-8")
    return nav_path


def _write_funnel_csv(group_dir: Path, row: dict[str, Any], missing_fields: list[str]) -> Path:
    path = group_dir / "funnel.csv"
    if path.exists():
        return path
    if any(row.get(k) is not None for k in ["raw_signal", "blocked", "orders", "skipped", "closed_trades"]):
        data = {
            "raw_signal": _safe_int(row.get("raw_signal")) or 0,
            "blocked": _safe_int(row.get("blocked")) or 0,
            "orders": _safe_int(row.get("orders")) or 0,
            "skipped": _safe_int(row.get("skipped")) or 0,
            "closed_trades": _safe_int(row.get("closed_trades")) or 0,
        }
    else:
        missing_fields.append("missing_funnel_source")
        data = {"raw_signal": 0, "blocked": 0, "orders": 0, "skipped": 0, "closed_trades": 0}
    pd.DataFrame([data]).to_csv(path, index=False)
    return path


def _write_reason_codes_csv(group_dir: Path, missing_fields: list[str]) -> Path:
    path = group_dir / "reason_code_counts.csv"
    if path.exists():
        return path
    src = group_dir / "reason_code_summary.csv"
    if src.exists():
        try:
            df = pd.read_csv(src)
            if "orders" in df.columns and "count" not in df.columns:
                df = df.rename(columns={"orders": "count"})
            cols = [c for c in ["reason_code", "count"] if c in df.columns]
            if cols:
                df[cols].to_csv(path, index=False)
                return path
        except Exception:
            pass
    missing_fields.append("missing_reason_code_source")
    pd.DataFrame(columns=["reason_code", "count"]).to_csv(path, index=False)
    return path


def _build_pipeline_stages(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "Universe", "enabled": True, "params": {}, "counters": {}},
        {"name": "Signal", "enabled": True, "params": {}, "counters": {"raw_signal_count": _safe_int(row.get("raw_signal")) or 0}},
        {"name": "Filters/Gates", "enabled": True, "params": {}, "counters": {"blocked_count": _safe_int(row.get("blocked")) or 0}},
        {"name": "Rank", "enabled": True, "params": {}, "counters": {"mf_rank_applied_rate": _safe_float(row.get("mf_rank_applied_rate")) or 0.0}},
        {"name": "Sizing", "enabled": True, "params": {}, "counters": {}},
        {"name": "Execution", "enabled": True, "params": {}, "counters": {"orders_count": _safe_int(row.get("orders")) or 0, "skipped_count": _safe_int(row.get("skipped")) or 0}},
        {"name": "Risk/Exit", "enabled": True, "params": {}, "counters": {"atr_stop_exit_count": _safe_int(row.get("atr_stop_exit_count")) or 0, "atr_trail_exit_count": _safe_int(row.get("atr_trail_exit_count")) or 0}},
        {"name": "Portfolio", "enabled": True, "params": {}, "counters": {"closed_trades_count": _safe_int(row.get("closed_trades")) or 0}},
    ]


def ensure_standard_artifacts(
    *,
    bt_root: Path,
    results_dir: Path,
    row: dict[str, Any],
    group_dir: Path,
    config_obj: dict[str, Any],
    meta_obj: dict[str, Any],
) -> dict[str, Any]:
    group_dir.mkdir(parents=True, exist_ok=True)
    missing_fields: list[str] = []

    # perf
    perf_obj = _extract_perf(row, group_dir, missing_fields)
    perf_path = group_dir / "perf.json"
    perf_path.write_text(json.dumps(sanitize_json(perf_obj), ensure_ascii=False, indent=2), encoding="utf-8")

    # nav / funnel / reason / config
    nav_path = _write_nav_csv(group_dir, missing_fields)
    funnel_path = _write_funnel_csv(group_dir, row, missing_fields)
    reason_path = _write_reason_codes_csv(group_dir, missing_fields)
    config_path = group_dir / "config.json"
    config_path.write_text(json.dumps(sanitize_json(config_obj), ensure_ascii=False, indent=2), encoding="utf-8")

    # counters
    atr_missing = not any(row.get(k) is not None for k in ["atr_stop_exit_count", "atr_trail_exit_count", "atr_trail_update_count"])
    if atr_missing:
        missing_fields.append("missing_atr_trigger_counters")

    counters = {
        "raw_signal_count": _safe_int(row.get("raw_signal")) or 0,
        "blocked_count": _safe_int(row.get("blocked")) or 0,
        "orders_count": _safe_int(row.get("orders")) or 0,
        "skipped_count": _safe_int(row.get("skipped")) or 0,
        "closed_trades_count": _safe_int(row.get("closed_trades")) or 0,
        "gate_on_rate": _safe_float(row.get("gate_on_rate")) or 0.0,
        "mf_rank_applied_rate": _safe_float(row.get("mf_rank_applied_rate")) or 0.0,
        "trend_slope_block_count": _safe_int(row.get("trend_slope_block_count")) or 0,
        "atr_stop_exit_count": _safe_int(row.get("atr_stop_exit_count")) or 0,
        "atr_trail_exit_count": _safe_int(row.get("atr_trail_exit_count")) or 0,
        "atr_trail_update_count": _safe_int(row.get("atr_trail_update_count")) or 0,
    }

    artifacts = {
        "run_meta": _to_rel(group_dir / "run_meta.json", results_dir),
        "perf": _to_rel(perf_path, results_dir),
        "nav": _to_rel(nav_path, results_dir),
        "funnel": _to_rel(funnel_path, results_dir),
        "reason_codes": _to_rel(reason_path, results_dir),
        "config": _to_rel(config_path, results_dir),
    }

    run_meta = {
        "run_id": str(row.get("run_id") or meta_obj.get("run_id") or ""),
        "exp_id": str(meta_obj.get("exp_id") or ""),
        "group_id": str(row.get("group_id") or ""),
        "preset": str(meta_obj.get("preset") or ""),
        "data": {
            "start": meta_obj.get("start_date"),
            "end": meta_obj.get("end_date"),
            "benchmark": meta_obj.get("benchmark"),
            "compare_symbol": meta_obj.get("compare_symbol"),
            "universe_mode": meta_obj.get("universe_mode", "db_index_constituents"),
            "universe_size": _safe_int(row.get("universe_filtered_count")),
            "universe_limit": _safe_int(meta_obj.get("universe_limit")),
            "filters": dict(meta_obj.get("filters") or {}),
        },
        "code": {
            "git_commit": meta_obj.get("git_commit"),
            "engine_mode": meta_obj.get("engine_mode", "VECTOR_FAST_STRICT"),
            "execution_profile": meta_obj.get("execution_profile", "tradable"),
            "fast_mode": bool(meta_obj.get("fast_mode", True)),
        },
        "pipeline": _build_pipeline_stages(row),
        "counters": counters,
        "artifacts": artifacts,
        "missing_fields": sorted(set(missing_fields)),
    }
    run_meta_path = group_dir / "run_meta.json"
    run_meta_path.write_text(json.dumps(sanitize_json(run_meta), ensure_ascii=False, indent=2), encoding="utf-8")

    row_out = dict(row)
    row_out["group_dir"] = _to_rel(group_dir, results_dir)
    row_out["group_files"] = artifacts
    row_out["run_meta"] = artifacts["run_meta"]
    if all((counters[k] == 0 for k in ["gate_on_rate", "mf_rank_applied_rate", "trend_slope_block_count", "atr_stop_exit_count", "atr_trail_exit_count", "atr_trail_update_count"])):
        flags = list(row_out.get("red_flags") or [])
        if "NOT_EFFECTIVE_OR_NOT_INSTRUMENTED" not in flags:
            flags.append("NOT_EFFECTIVE_OR_NOT_INSTRUMENTED")
        row_out["red_flags"] = flags
    return row_out


def write_manifest(results_dir: Path, result: dict[str, Any], rows_norm: list[dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    groups = []
    for row in rows_norm:
        groups.append(
            {
                "group_id": str(row.get("group_id") or ""),
                "status": str(row.get("status") or ""),
                "tier": str(row.get("tier") or ""),
                "group_dir": str(row.get("group_dir") or ""),
                "group_files": dict(row.get("group_files") or {}),
            }
        )
    matrix_head = result.get("matrix_head")
    dims = {"matrix_head": matrix_head if isinstance(matrix_head, list) else []}
    manifest = {
        "exp_id": str(result.get("exp_id") or ""),
        "preset": str(result.get("preset") or ""),
        "created_at": datetime.now().isoformat(),
        "dims": dims,
        "variant_count": len(rows_norm),
        "groups_count": len(rows_norm),
        "baseline_suggested": groups[0]["group_id"] if groups else "",
        "groups": groups,
    }
    path = results_dir / "manifest.json"
    path.write_text(json.dumps(sanitize_json(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    return path, manifest


def stabilize_experiment_result(
    *,
    bt_root: Path,
    result: dict[str, Any],
    base_cfg: dict[str, Any],
    overrides: dict[str, Any],
    run_profile_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    result_in = dict(result or {})
    rows_any = result_in.get("rows") if isinstance(result_in.get("rows"), list) else []
    rows_raw = [_jsonify_row(x) for x in rows_any]
    runs = list(result_in.get("runs") or [])

    results_dir = _resolve_runtime_path(str(result_in.get("results_dir") or ""), bt_root)
    if results_dir is None:
        results_dir = (bt_root / "results" / "experiments" / str(result_in.get("exp_id") or "exp_unknown")).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    start_date = str(overrides.get("start_date") or base_cfg.get("universe", {}).get("start") or "")
    end_date = str(overrides.get("end_date") or base_cfg.get("universe", {}).get("end") or "")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "")
    compare_symbol = str(overrides.get("compare_symbol") or base_cfg.get("universe", {}).get("benchmark_compare_symbol") or "")
    universe_limit = _safe_int(overrides.get("universe_limit"))
    symbols = overrides.get("symbols") or base_cfg.get("universe", {}).get("symbols") or []

    group_ids = [str((r or {}).get("group_id") or "") for r in rows_raw if str((r or {}).get("group_id") or "")]
    group_map = discover_group_dirs(
        results_dir=results_dir,
        rows=rows_raw,
        runs=runs,
        group_ids=group_ids,
        bt_root=bt_root,
    )

    meta_obj = {
        "exp_id": str(result_in.get("exp_id") or ""),
        "preset": str(result_in.get("preset") or ""),
        "start_date": start_date,
        "end_date": end_date,
        "benchmark": benchmark,
        "compare_symbol": compare_symbol,
        "universe_limit": universe_limit,
        "universe_mode": "db_index_constituents",
        "engine_mode": str(overrides.get("engine_mode") or "VECTOR_FAST_STRICT"),
        "execution_profile": str(overrides.get("execution_profile") or "tradable"),
        "fast_mode": bool(overrides.get("fast_mode", True)),
        "git_commit": None,
        "filters": {
            "exclude_st": base_cfg.get("universe", {}).get("exclude_st"),
            "exclude_suspended": base_cfg.get("universe", {}).get("exclude_suspended"),
            "min_list_days": base_cfg.get("universe", {}).get("min_list_days"),
            "min_avg_turnover_20d": base_cfg.get("universe", {}).get("min_avg_turnover_20d"),
        },
    }

    rows_norm: list[dict[str, Any]] = []
    for row in rows_raw:
        gid = str(row.get("group_id") or "")
        group_dir = group_map.get(gid, results_dir / "groups" / (gid or "UNKNOWN"))

        row0 = normalize_row(
            row,
            start_date=start_date,
            end_date=end_date,
            universe_size=_safe_int(row.get("universe_filtered_count")),
            universe_limit=universe_limit,
        )
        row1 = ensure_standard_artifacts(
            bt_root=bt_root,
            results_dir=results_dir,
            row=row0,
            group_dir=group_dir,
            config_obj={"base_config": base_cfg, "overrides": overrides, "group_id": gid},
            meta_obj=meta_obj,
        )
        # normalize again to lock final schema/types
        row2 = normalize_row(
            row1,
            start_date=start_date,
            end_date=end_date,
            universe_size=_safe_int(row1.get("universe_filtered_count")),
            universe_limit=universe_limit,
        )
        row2["group_dir"] = str(row1.get("group_dir") or "")
        row2["group_files"] = dict(row1.get("group_files") or {})
        rows_norm.append(row2)

    manifest_path, manifest_obj = write_manifest(results_dir, result_in, rows_norm)
    files = dict(result_in.get("files") or {})
    files["manifest"] = str(manifest_path)
    rp = results_dir / "run_profile.json"
    if rp.exists():
        files["run_profile"] = str(rp)
    if result_in.get("click_report_md"):
        files["click_report_md"] = str(result_in.get("click_report_md"))
    if result_in.get("single_report_path"):
        files["single_report_md"] = str(result_in.get("single_report_path"))

    out = dict(result_in)
    out["exp_id"] = str(out.get("exp_id") or "")
    out["preset"] = str(out.get("preset") or "")
    out["results_dir"] = str(results_dir)
    out["rows"] = rows_norm
    out["matrix_head"] = list(out.get("matrix_head") or rows_norm[: min(4, len(rows_norm))])
    out["runs"] = list(out.get("runs") or [])
    out["files"] = files
    out["manifest"] = {
        "dims": manifest_obj.get("dims", {}),
        "variant_count": int(manifest_obj.get("variant_count", 0) or 0),
        "baseline_suggested": str(manifest_obj.get("baseline_suggested") or ""),
        "groups_count": int(manifest_obj.get("groups_count", len(rows_norm)) or len(rows_norm)),
    }
    out["start_date"] = start_date or None
    out["end_date"] = end_date or None
    out["universe_limit"] = universe_limit
    out["symbols_count"] = len(symbols) if isinstance(symbols, list) else None
    if run_profile_summary:
        out["run_profile_summary"] = run_profile_summary
    return sanitize_json(out)
