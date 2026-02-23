from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from backtest_v2.analytics.experiment_matrix import (
    MATRIX_COLUMNS,
    build_matrix_row,
    compute_top10_from_trade_pnl,
)
from backtest_v2.analytics.schema import normalize_order_status
from backtest_v2.cli.run import run_command
from backtest_v2.config.loader import load_config
from backtest_v2.config.models import StrategyConfig
from backtest_v2.data.pg_provider import PostgresDataProvider
from backtest_v2.portfolio.portfolio_builder import run_portfolio_backtest
from backtest_v2.strategies.registry import build_strategy
from backtest_v2.tools.reporting import write_report_to_local
from backtest_v2.tools.run_context import RunContext
from backtest_v2.tools.walkforward import split_wf3
from backtest_v2.tools.validate_scan import validate_matrix
from backtest_v2.tools.variants import preset_variants
from backtest_v2.tools.univote_v2 import (
    UnivoteV2Config,
    build_universe_too_small_rows,
    run_univote_v2,
    write_universe_load_failed_report,
)
from backtest_v2.universe.hs300_csi500 import (
    UniverseFilterConfig,
    UniverseLoadError,
    apply_universe_filters,
    build_candidates_from_ohlcv,
    load_constituents_from_db,
    resolve_universe_symbols,
)

DEFAULT_EXPERIMENT_SYMBOLS = ["600000.SH", "000001.SZ", "600519.SH"]
STRENGTH_RANK_V2_REPORT_FILE = "STRENGTH_RANK_VALIDATE_v2.md"
STRENGTH_RANK_V2A_REPORT_FILE = "STRENGTH_RANK_VALIDATE_v2A.md"
STRENGTH_RANK_V2B_REPORT_FILE = "STRENGTH_RANK_VALIDATE_v2B.md"
ENGINE_PERF_COMPARE_REPORT_FILE = "ENGINE_PERF_COMPARE_v1.md"
ENGINE_PERF_COMPARE_V2_REPORT_FILE = "ENGINE_PERF_COMPARE_v2.md"
FULL_RANGE_VALIDATE_REPORT_FILE = "FULL_RANGE_VALIDATE_v1.md"
CANDIDATE_DIAG_REPORT_FILE = "CANDIDATE_DIAG_VALIDATE_v1.md"
SLOT_DEBUG_REPORT_FILE = "SLOT_DEBUG_VALIDATE_v1.md"
TREND_TIER_REPORT_FILE = "TREND_TIER_VALIDATE_v1.md"
TREND_ACCEL_BRAKE_REPORT_FILE = "TREND_ACCEL_BRAKE_VALIDATE_v1.md"
TREND_BRAKE_CONFIRM_REPORT_FILE = "TREND_BRAKE_CONFIRM_VALIDATE_v1.md"
MAX_POSITION_EXPAND_REPORT_FILE = "MAX_POSITION_EXPAND_VALIDATE_v1.md"
STRONG_ONLY_REPORT_FILE = "STRONG_ONLY_VALIDATE_v1.md"
MULTIFACTOR_RANKING_REPORT_FILE = "MULTIFACTOR_RANKING_VALIDATE_v1.md"
MULTIFACTOR_RANKING_WF_REPORT_FILE = "MULTIFACTOR_RANKING_WALKFORWARD_v1.md"
MF_RANK_REGIME_GATE_REPORT_FILE = "MF_RANK_REGIME_GATE_VALIDATE_v1.md"
MF_RANK_STRONG_GATE_REPORT_FILE = "MF_RANK_STRONG_GATE_VALIDATE_v1.md"
TREND_QUALITY_REPORT_FILE = "TREND_QUALITY_VALIDATE_v1.md"
MULTIFACTOR_BALANCED_REPORT_FILE = "MULTIFACTOR_BALANCED_v1.md"
MULTIFACTOR_FUSION_COMPARE_REPORT_FILE = "MULTIFACTOR_FUSION_COMPARE_v1.md"


def _emit_progress(
    progress_hook: Callable[[int, str], None] | None,
    pct: int,
    message: str,
) -> None:
    if progress_hook is None:
        return
    try:
        progress_hook(int(max(0, min(100, pct))), str(message))
    except Exception:
        pass


def _emit_stage_progress(
    progress_hook: Callable[[int, str], None] | None,
    ctx: RunContext | None,
    *,
    stage: str,
    pct: int,
    message: str,
) -> None:
    if ctx is not None:
        ctx.start_stage(stage)
    _emit_progress(progress_hook, pct, message)


def _close_stage(ctx: RunContext | None, stage: str | None = None) -> None:
    if ctx is not None:
        ctx.end_stage(stage)


def _apply_fast_experiment_template(preset_key: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
    ov = dict(overrides or {})
    key = str(preset_key or "").strip().upper()
    if key in {
        "FULL_RANGE_VALIDATE_V1",
        "MULTIFACTOR_RANKING_WALKFORWARD_V1",
        "MF_RANK_REGIME_GATE_VALIDATE_V1",
        "MF_RANK_STRONG_GATE_VALIDATE_V1",
        "TREND_QUALITY_VALIDATE_V1",
        "MULTIFACTOR_INCREMENTAL_V1_6_1A",
        "EXPERIMENT_6_1A",
        "6.1A",
        "MULTIFACTOR_INCREMENTAL_V1_6_1B",
        "EXPERIMENT_6_1B",
        "6.1B",
        "MULTIFACTOR_INCREMENTAL_V1_6_1_LIMIT",
        "EXPERIMENT_6_1_LIMIT",
        "6.1L",
    }:
        ov.setdefault("fast_mode", True)
        ov.setdefault("write_trades_artifacts", False)
        if "history_lookback_days" not in ov:
            ov["history_lookback_days"] = 120 if key in {"MULTIFACTOR_INCREMENTAL_V1_6_1A", "EXPERIMENT_6_1A", "6.1A"} else 180
    return ov


def _split_wf_folds_for_range(
    full_start: str,
    full_end: str,
    wf_folds: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    start_d = date.fromisoformat(full_start)
    end_d = date.fromisoformat(full_end)
    executable: list[dict[str, Any]] = []
    skipped: list[str] = []
    for fold in wf_folds:
        name = str(fold.get("name", ""))
        ts = str(fold.get("test_start", ""))
        te = str(fold.get("test_end", ""))
        if not ts or not te:
            skipped.append(name or "UNKNOWN")
            continue
        test_start = date.fromisoformat(ts)
        test_end = date.fromisoformat(te)
        if test_end < start_d or test_start > end_d:
            skipped.append(name or "UNKNOWN")
            continue
        executable.append(fold)
    return executable, skipped


def _load_ohlcv_with_cache(
    provider: PostgresDataProvider,
    cache: dict[tuple[Any, ...], pd.DataFrame],
    symbols: list[str],
    start: date,
    end: date,
    stats: dict[str, Any],
) -> pd.DataFrame:
    key = ("ohlcv", tuple(sorted([str(s) for s in symbols])), str(start), str(end))
    stats["cache_total"] = int(stats.get("cache_total", 0)) + 1
    if key in cache:
        stats["cache_hit"] = int(stats.get("cache_hit", 0)) + 1
        return cache[key]
    stats["db_queries"] = int(stats.get("db_queries", 0)) + 1
    df = provider.load_ohlcv_daily(symbols, start, end, source="stocks")
    cache[key] = df
    return df


def apply_cfg_diff(cfg: dict[str, Any], diff_dict: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    strategy = diff_dict.get("strategy", {})
    out.setdefault("strategy", {})
    if "name" in strategy:
        out["strategy"]["name"] = strategy["name"]
    if "params" in strategy:
        out["strategy"]["params"] = dict(strategy.get("params", {}) or {})

    out.setdefault("execution", {})
    out["execution"]["profile"] = str(diff_dict.get("execution_profile", "tradable"))

    symbols = diff_dict.get("symbols")
    if symbols is not None:
        out.setdefault("universe", {})
        out["universe"]["symbols"] = list(symbols)

    if diff_dict.get("start_date"):
        out.setdefault("universe", {})
        out["universe"]["start"] = str(diff_dict["start_date"])
    if diff_dict.get("end_date"):
        out.setdefault("universe", {})
        out["universe"]["end"] = str(diff_dict["end_date"])
    if diff_dict.get("benchmark"):
        out.setdefault("universe", {})
        out["universe"]["benchmark"] = str(diff_dict["benchmark"])
    if diff_dict.get("compare_symbol"):
        out.setdefault("universe", {})
        out["universe"]["benchmark_compare_symbol"] = str(diff_dict["compare_symbol"])
    if diff_dict.get("initial_capital") is not None:
        out["initial_capital"] = float(diff_dict["initial_capital"])
    if diff_dict.get("sizing_mode"):
        out.setdefault("sizing", {})
        out["sizing"]["mode"] = str(diff_dict["sizing_mode"])
    if diff_dict.get("order_value") is not None:
        out.setdefault("sizing", {})
        out["sizing"]["order_value"] = float(diff_dict["order_value"])
    if diff_dict.get("reserve_cash_ratio") is not None:
        out.setdefault("sizing", {})
        out["sizing"]["reserve_cash_ratio"] = float(diff_dict["reserve_cash_ratio"])
    if diff_dict.get("lot_size") is not None:
        out.setdefault("sizing", {})
        out["sizing"]["lot_size"] = int(diff_dict["lot_size"])
    if diff_dict.get("univote_lot_mode"):
        out.setdefault("sizing", {})
        out["sizing"]["univote_lot_mode"] = str(diff_dict["univote_lot_mode"])
    _inject_db_credentials_from_env(out)
    return out


def run_experiment(
    base_cfg_path: str,
    preset: str = "G1-G4",
    overrides: dict[str, Any] | None = None,
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    overrides = _apply_fast_experiment_template(str(preset or ""), overrides)
    base_path = Path(base_cfg_path).resolve()
    base_cfg = json.loads(base_path.read_text(encoding="utf-8-sig"))
    preset_key = str(preset or "").strip().upper()

    if preset_key == "ATR_PARAM_VALIDATE_WF3":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（Walk-Forward）")
        return _run_walkforward_validate(base_path, base_cfg, preset, overrides)
    if preset_key == "ATR_PARAM_VALIDATE_UNIVOTE":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（Universe Vote）")
        return _run_universe_vote_validate(base_path, base_cfg, preset, overrides)
    if preset_key == "ATR_PARAM_VALIDATE_UNIVOTE_V2":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（Universe Vote v2）")
        return _run_universe_vote_validate_v2(base_path, base_cfg, preset, overrides)
    if preset_key == "ATR_PORTFOLIO_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（组合对照）")
        return _run_portfolio_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "PORTFOLIO_FILL_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（持仓密度）")
        return _run_portfolio_fill_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "TREND_FILTER_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（趋势过滤）")
        return _run_trend_filter_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "TREND_TIER_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（趋势分档）")
        return _run_trend_tier_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "TREND_ACCEL_BRAKE_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（加速档位+趋势刹车）")
        return _run_trend_accel_brake_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "TREND_BRAKE_CONFIRM_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（确认型趋势刹车）")
        return _run_trend_brake_confirm_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "STRENGTH_RANK_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（强度排序）")
        return _run_strength_rank_validate_v1(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "STRENGTH_RANK_VALIDATE_V2":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多阶段+3折）")
        return _run_strength_rank_validate_v2(base_path, base_cfg, preset, overrides, progress_hook=progress_hook)
    if preset_key == "STRENGTH_RANK_VALIDATE_V2A":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（触发率修复版）")
        ov = dict(overrides or {})
        ov.setdefault("max_positions", 3)
        return _run_strength_rank_validate_v2(
            base_path,
            base_cfg,
            preset,
            ov,
            progress_hook=progress_hook,
            report_filename=STRENGTH_RANK_V2A_REPORT_FILE,
            warning_label="RANK_RARELY_TRIGGERED_V2A",
        )
    if preset_key == "STRENGTH_RANK_VALIDATE_V2B":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（universe=1000 优化版）")
        ov = dict(overrides or {})
        ov.setdefault("fast_mode", True)
        ov.setdefault("start_date", "2024-01-01")
        ov.setdefault("end_date", "2026-02-01")
        ov.setdefault("universe_limit", 1000)
        return _run_strength_rank_validate_v2(
            base_path,
            base_cfg,
            preset,
            ov,
            progress_hook=progress_hook,
            report_filename=STRENGTH_RANK_V2B_REPORT_FILE,
            warning_label="RANK_RARELY_TRIGGERED_V2B",
        )
    if preset_key == "ENGINE_PERF_COMPARE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（慢引擎 vs 向量引擎）")
        return _run_engine_perf_compare_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=ENGINE_PERF_COMPARE_REPORT_FILE,
        )
    if preset_key == "ENGINE_PERF_COMPARE_V2":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（慢引擎 vs 向量引擎，1000标的）")
        ov = dict(overrides or {})
        ov.setdefault("start_date", "2025-02-01")
        ov.setdefault("end_date", "2026-02-01")
        ov.setdefault("universe_limit", 1000)
        return _run_engine_perf_compare_v1(
            base_path,
            base_cfg,
            preset,
            ov,
            progress_hook=progress_hook,
            report_filename=ENGINE_PERF_COMPARE_V2_REPORT_FILE,
        )
    if preset_key == "FULL_RANGE_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（全区间验证）")
        return _run_full_range_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=FULL_RANGE_VALIDATE_REPORT_FILE,
        )
    if preset_key == "CANDIDATE_DIAG_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（候选不足诊断）")
        return _run_candidate_diag_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=CANDIDATE_DIAG_REPORT_FILE,
        )
    if preset_key == "SLOT_DEBUG_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（空位计算调试）")
        return _run_slot_debug_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=SLOT_DEBUG_REPORT_FILE,
        )
    if preset_key == "MAX_POSITION_EXPAND_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（max_positions扩容验证）")
        return _run_max_position_expand_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MAX_POSITION_EXPAND_REPORT_FILE,
        )
    if preset_key == "STRONG_ONLY_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（强趋势集中火力）")
        return _run_strong_only_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=STRONG_ONLY_REPORT_FILE,
        )
    if preset_key == "MULTIFACTOR_RANKING_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子排名裁剪）")
        return _run_multifactor_ranking_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MULTIFACTOR_RANKING_REPORT_FILE,
        )
    if preset_key == "MULTIFACTOR_RANKING_WALKFORWARD_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子排名WF稳健性）")
        return _run_multifactor_ranking_walkforward_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MULTIFACTOR_RANKING_WF_REPORT_FILE,
        )
    if preset_key == "MF_RANK_REGIME_GATE_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子排名Regime Gate）")
        return _run_mf_rank_regime_gate_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MF_RANK_REGIME_GATE_REPORT_FILE,
        )
    if preset_key == "MF_RANK_STRONG_GATE_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子排名Strong Gate）")
        return _run_mf_rank_regime_gate_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MF_RANK_STRONG_GATE_REPORT_FILE,
        )
    if preset_key == "TREND_QUALITY_VALIDATE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（趋势质量 M1+M2）")
        return _run_trend_quality_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=TREND_QUALITY_REPORT_FILE,
        )
    if preset_key == "MULTIFACTOR_BALANCED_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子 Balanced）")
        return _run_multifactor_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MULTIFACTOR_BALANCED_REPORT_FILE,
        )
    if preset_key == "MULTIFACTOR_FUSION_COMPARE_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子融合模式对比）")
        return _run_multifactor_validate_v1(
            base_path,
            base_cfg,
            preset,
            overrides,
            progress_hook=progress_hook,
            report_filename=MULTIFACTOR_FUSION_COMPARE_REPORT_FILE,
        )
    if preset_key == "MULTIFACTOR_INCREMENTAL_V1":
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子增量实验）")
        ov = dict(overrides or {})
        ov.setdefault("universe_limit", 1000)
        ov.setdefault("fast_mode", True)
        ov.setdefault("history_lookback_days", 180)
        return _run_multifactor_validate_v1(
            base_path,
            base_cfg,
            preset,
            ov,
            progress_hook=progress_hook,
            report_filename="multifactor_incremental_v1.json",
        )
    if preset_key in {"MULTIFACTOR_INCREMENTAL_V1_6_1B", "EXPERIMENT_6_1B", "6.1B", "MULTIFACTOR_INCREMENTAL_V1_6_1_LIMIT", "EXPERIMENT_6_1_LIMIT", "6.1L"}:
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子增量实验 6.1B 限速版）")
        ov = dict(overrides or {})
        ov.setdefault("start_date", "2023-01-01")
        ov.setdefault("end_date", "2026-02-01")
        ov.setdefault("universe_limit", 300)
        ov.setdefault("fast_mode", True)
        ov.setdefault("history_lookback_days", 120)
        ov.setdefault("enable_multifactor_fast_gate", True)
        return _run_multifactor_validate_v1(
            base_path,
            base_cfg,
            "MULTIFACTOR_INCREMENTAL_V1_6_1B",
            ov,
            progress_hook=progress_hook,
            report_filename="multifactor_incremental_v1_6_1b.json",
        )
    if preset_key in {"MULTIFACTOR_INCREMENTAL_V1_6_1A", "EXPERIMENT_6_1A", "6.1A", "MULTIFACTOR_INCREMENTAL_V1_6_4", "EXPERIMENT_6_4", "6.4"}:
        _emit_progress(progress_hook, 8, f"开始实验：{preset}（多因子增量实验 6.1A 快速版）")
        # 相比 6.1（默认 2021-01-01~2026-02-01, limit=1000），
        # 6.1A 默认缩短约 10 倍时间窗并将 limit 缩短到 100。
        ov = dict(overrides or {})
        ov.setdefault("start_date", "2025-08-01")
        ov.setdefault("end_date", "2026-02-01")
        ov.setdefault("universe_limit", 100)
        ov.setdefault("fast_mode", True)
        ov.setdefault("enable_multifactor_fast_gate", True)
        return _run_multifactor_validate_v1(
            base_path,
            base_cfg,
            "MULTIFACTOR_INCREMENTAL_V1_6_1A",
            ov,
            progress_hook=progress_hook,
            report_filename="multifactor_incremental_v1_6_1a.json",
        )

    variants = preset_variants(preset)

    symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []

    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        base_pct = 10 + int((idx - 1) * 75 / total_variants)
        _emit_progress(progress_hook, base_pct, f"[{idx}/{total_variants}] 运行分组 {group_id}")
        diff = {
            "strategy": variant["strategy"],
            "execution_profile": "tradable",
            "symbols": symbols,
            "start_date": overrides.get("start_date"),
            "end_date": overrides.get("end_date"),
            "benchmark": overrides.get("benchmark"),
            "compare_symbol": overrides.get("compare_symbol"),
            "initial_capital": overrides.get("initial_capital"),
            "sizing_mode": overrides.get("sizing_mode"),
            "order_value": overrides.get("order_value"),
            "reserve_cash_ratio": overrides.get("reserve_cash_ratio"),
            "lot_size": overrides.get("lot_size"),
            "univote_lot_mode": overrides.get("univote_lot_mode"),
        }
        run_id = f"run_{group_id.lower()}_{exp_id[:8]}"
        run_dir = _results_root() / run_id
        temp_cfg_path = exp_dir / f"{group_id}_config.json"
        cfg = apply_cfg_diff(base_cfg, diff)
        temp_cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        started = time.perf_counter()
        try:
            run_command(str(temp_cfg_path), str(run_dir))
            duration_ms = int((time.perf_counter() - started) * 1000)
            summary = _load_json(run_dir / "report_summary.json")
            run_meta = _load_json(run_dir / "run_meta.json")
            conclusions = _load_json(run_dir / "conclusions_10.json")
            warnings_list = _load_list(run_dir / "warnings.json")
            if warnings_list:
                summary.setdefault("audit", {}).setdefault("validation", {}).setdefault("warnings", warnings_list)
            orders_df = _load_csv(run_dir / "orders.csv")
            signals_df = _load_csv(run_dir / "signals.csv")
            trades_df = _load_csv(run_dir / "trades.csv")
            conc = summary.get("trade_concentration") or {}
            trade_top10 = {}
            if (not conc) or (conc.get("top10_contribution_pct") is None and int(conc.get("closed_trades", 0) or 0) > 0):
                trade_top10 = compute_top10_from_trade_pnl(run_dir / "trade_pnl.csv")
                if trade_top10.get("closed_trades", 0) > 0:
                    (run_dir / "trade_concentration.json").write_text(
                        json.dumps(trade_top10, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            signal_stats = _resolve_signal_stats(summary, signals_df)
            execution_stats = _resolve_execution_stats(summary, orders_df)
            scan_fields = _resolve_scan_fields(variant, orders_df, trades_df)
            row = build_matrix_row(
                group_id=group_id,
                run_id=run_id,
                report_summary=summary,
                conclusions=conclusions,
                duration_ms=duration_ms,
                strategy_name=str(run_meta.get("strategy_name") or variant["strategy"]["name"]),
                strategy_params=dict(variant["strategy"].get("params", {}) or {}),
                strategy_name_full=str(run_meta.get("strategy_name") or variant["strategy"]["name"]),
                strategy_params_full=dict(run_meta.get("strategy_params", {}) or variant["strategy"].get("params", {}) or {}),
                status="OK",
                error_message=None,
                top10_fallback=trade_top10,
                signal_stats=signal_stats,
                execution_stats=execution_stats,
                scan_fields=scan_fields,
                warning_notes=str(trade_top10.get("warning_notes", "")),
            )
            if str(row.get("strategy_name_full", "")).lower() == "sma_cross_regime_atr":
                stop_n = int(row.get("atr_stop_exit_count", 0) or 0)
                trail_n = int(row.get("atr_trail_exit_count", 0) or 0)
                if stop_n == 0 and trail_n == 0:
                    note = str(row.get("warning_notes") or "")
                    tip = "ATR not triggered in sample; check debug_atr.csv"
                    row["warning_notes"] = f"{note}; {tip}".strip("; ")
            rows.append(row)
            runs.append(
                {
                    "group_id": group_id,
                    "run_id": run_id,
                    "diff": diff,
                    "artifacts": _artifact_paths(run_dir),
                    "status": "OK",
                    "error_message": None,
                }
            )
            done_pct = 10 + int(idx * 75 / total_variants)
            _emit_progress(progress_hook, done_pct, f"[{idx}/{total_variants}] 分组 {group_id} 完成")
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            err = f"{type(exc).__name__}: {exc}"
            rows.append(
                build_matrix_row(
                    group_id=group_id,
                    run_id=run_id,
                    report_summary={},
                    conclusions={},
                    duration_ms=duration_ms,
                    strategy_name=str(variant["strategy"]["name"]),
                    strategy_params=dict(variant["strategy"].get("params", {}) or {}),
                    strategy_name_full=str(variant["strategy"]["name"]),
                    strategy_params_full=dict(variant["strategy"].get("params", {}) or {}),
                    status="FAILED",
                    error_message=err,
                    top10_fallback={},
                    signal_stats={},
                    execution_stats={},
                    scan_fields={},
                    warning_notes=err,
                )
            )
            runs.append(
                {
                    "group_id": group_id,
                    "run_id": run_id,
                    "diff": diff,
                    "artifacts": _artifact_paths(run_dir),
                    "status": "FAILED",
                    "error_message": err,
                }
            )
            done_pct = 10 + int(idx * 75 / total_variants)
            _emit_progress(progress_hook, done_pct, f"[{idx}/{total_variants}] 分组 {group_id} 失败")

    _emit_progress(progress_hook, 90, "写出实验矩阵与报告")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    baseline_ref = _write_baseline_ref(exp_dir, str(preset))
    if str(preset).upper() == "ATR_PARAM_SCAN_V1":
        _write_scan_artifacts(exp_dir, rows)
    if str(preset).upper() == "ATR_PARAM_VALIDATE_V1":
        _write_validate_v1_artifacts(exp_dir, rows, baseline_ref)
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "实验产物打包完成")
    matrix_head = rows[: min(4, len(rows))]
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": matrix_head,
        "runs": runs,
    }


def _results_root() -> Path:
    return Path(__file__).resolve().parents[2] / "results"


def _inject_db_credentials_from_env(cfg: dict[str, Any]) -> None:
    cfg.setdefault("db", {})
    db = cfg["db"]
    host = os.environ.get("PAMS_PG_HOST") or os.environ.get("PGHOST")
    port = os.environ.get("PAMS_PG_PORT") or os.environ.get("PGPORT")
    dbname = os.environ.get("PAMS_PG_DBNAME") or os.environ.get("PGDATABASE")
    user = os.environ.get("PAMS_PG_USER") or os.environ.get("PGUSER")
    password = os.environ.get("PAMS_PG_PASSWORD") or os.environ.get("PGPASSWORD")
    if host:
        db["host"] = host
    if port:
        try:
            db["port"] = int(port)
        except Exception:
            db["port"] = db.get("port", 5432)
    if dbname:
        db["dbname"] = dbname
    if user:
        db["user"] = user
    if password is not None:
        db["password"] = password


def _build_exp_id(
    base_cfg: dict[str, Any],
    variants: list[dict[str, Any]],
    preset: str,
    symbols: list[str],
) -> str:
    payload = {
        "base_cfg": base_cfg,
        "variants": variants,
        "preset": preset,
        "symbols": symbols,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nonce = uuid.uuid4().hex[:6]
    return f"exp_{stamp}_{digest[:8]}_{nonce}"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _artifact_paths(run_dir: Path) -> dict[str, str]:
    return {
        "report_summary_json": str(run_dir / "report_summary.json"),
        "report_json": str(run_dir / "report.json"),
        "run_meta_json": str(run_dir / "run_meta.json"),
        "conclusions_10_json": str(run_dir / "conclusions_10.json"),
        "trade_pnl_csv": str(run_dir / "trade_pnl.csv"),
        "signals_csv": str(run_dir / "signals.csv"),
        "reason_code_summary_csv": str(run_dir / "reason_code_summary.csv"),
        "debug_atr_csv": str(run_dir / "debug_atr.csv"),
    }


def _load_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _resolve_signal_stats(summary: dict[str, Any], signals_df: pd.DataFrame) -> dict[str, Any]:
    stats = dict(summary.get("signal_stats", {}) or {})
    if stats:
        return {
            "raw_signals_total": int(stats.get("raw_signals_total", 0) or 0),
            "raw_buy_signals": int(stats.get("raw_buy_signals", 0) or 0),
            "raw_sell_signals": int(stats.get("raw_sell_signals", 0) or 0),
            "blocked_signals_total": int(stats.get("blocked_signals_total", 0) or 0),
            "blocked_buy_signals": int(stats.get("blocked_buy_signals", 0) or 0),
            "blocked_reasons_top3": dict(stats.get("blocked_reasons_top3", {}) or {}),
        }

    if signals_df.empty:
        return {
            "raw_signals_total": int(summary.get("raw_signals_total", 0) or 0),
            "raw_buy_signals": int(summary.get("raw_buy_signals", 0) or 0),
            "raw_sell_signals": int(summary.get("raw_sell_signals", 0) or 0),
            "blocked_signals_total": int(summary.get("blocked_signals_total", 0) or 0),
            "blocked_buy_signals": int(summary.get("blocked_buy_signals", 0) or 0),
            "blocked_reasons_top3": dict(summary.get("blocked_reasons_top3", {}) or {}),
        }

    action = signals_df.get("action", pd.Series([""] * len(signals_df))).astype(str).str.upper()
    side = signals_df.get("side", pd.Series([""] * len(signals_df))).astype(str).str.upper()
    reason = signals_df.get("reason_code", pd.Series([""] * len(signals_df))).astype(str).str.strip()
    blocked_mask = action.eq("BLOCKED")
    blocked_reasons = (
        reason[blocked_mask]
        .replace({"": "BLOCKED"})
        .value_counts()
        .head(3)
        .to_dict()
    )
    return {
        "raw_signals_total": int(len(signals_df)),
        "raw_buy_signals": int(action.eq("BUY").sum()),
        "raw_sell_signals": int(action.eq("SELL").sum()),
        "blocked_signals_total": int(blocked_mask.sum()),
        "blocked_buy_signals": int((blocked_mask & side.eq("BUY")).sum()),
        "blocked_reasons_top3": blocked_reasons,
    }


def _resolve_execution_stats(summary: dict[str, Any], orders_df: pd.DataFrame) -> dict[str, Any]:
    stats = dict(summary.get("execution_stats", {}) or {})
    if stats:
        atr_counts = _resolve_atr_exit_counts(summary, orders_df)
        return {
            "tradable_orders_total": int(stats.get("tradable_orders_total", 0) or 0),
            "skipped_orders_total": int(stats.get("skipped_orders_total", 0) or 0),
            "skipped_reasons_top3": dict(stats.get("skipped_reasons_top3", {}) or {}),
            "rejected_orders_total": int(stats.get("rejected_orders_total", 0) or 0),
            "rejected_reasons_top3": dict(stats.get("rejected_reasons_top3", {}) or {}),
            "atr_stop_exit_count": int(atr_counts.get("atr_stop_exit_count", 0) or 0),
            "atr_trail_exit_count": int(atr_counts.get("atr_trail_exit_count", 0) or 0),
        }

    if orders_df.empty:
        kpi = summary.get("kpis", {}) or {}
        return {
            "tradable_orders_total": int(summary.get("tradable_orders_total", kpi.get("orders_total", 0)) or 0),
            "skipped_orders_total": int(kpi.get("skipped_orders", 0) or 0),
            "skipped_reasons_top3": dict(summary.get("top_skip_reason", {}) or {}),
            "rejected_orders_total": int(kpi.get("rejected_orders", 0) or 0),
            "rejected_reasons_top3": {},
            "atr_stop_exit_count": 0,
            "atr_trail_exit_count": 0,
        }

    raw_status = orders_df.get("status", pd.Series([""] * len(orders_df))).astype(str).str.upper()
    status = raw_status.map(normalize_order_status)
    skip_reasons = (
        orders_df.loc[raw_status.eq("SKIPPED"), "skip_reason"]
        .astype(str)
        .str.strip()
        .replace({"": "UNKNOWN"})
        .value_counts()
        .head(3)
        .to_dict()
        if "skip_reason" in orders_df.columns
        else {}
    )
    rejected_reasons = (
        orders_df.loc[status.isin(["REJECTED", "CANCELLED"]), "reject_reason"]
        .astype(str)
        .str.strip()
        .replace({"": "UNKNOWN"})
        .value_counts()
        .head(3)
        .to_dict()
        if "reject_reason" in orders_df.columns
        else {}
    )
    atr_counts = _resolve_atr_exit_counts(summary, orders_df)
    return {
        "tradable_orders_total": int(len(orders_df)),
        "skipped_orders_total": int(raw_status.eq("SKIPPED").sum()),
        "skipped_reasons_top3": skip_reasons,
        "rejected_orders_total": int(status.isin(["REJECTED", "CANCELLED"]).sum()),
        "rejected_reasons_top3": rejected_reasons,
        "atr_stop_exit_count": int(atr_counts.get("atr_stop_exit_count", 0) or 0),
        "atr_trail_exit_count": int(atr_counts.get("atr_trail_exit_count", 0) or 0),
    }


def _resolve_atr_exit_counts(summary: dict[str, Any], orders_df: pd.DataFrame) -> dict[str, int]:
    exit_map = summary.get("exit_reason_breakdown", {})
    if isinstance(exit_map, dict) and exit_map:
        return {
            "atr_stop_exit_count": int(exit_map.get("ATR_STOP_LOSS", 0) or 0),
            "atr_trail_exit_count": int(exit_map.get("ATR_TRAIL_EXIT", 0) or 0),
        }

    reasons = summary.get("reasons", [])
    if isinstance(reasons, list) and reasons:
        out = {"atr_stop_exit_count": 0, "atr_trail_exit_count": 0}
        for item in reasons:
            code = str((item or {}).get("reason_code", "")).strip().upper()
            orders = int((item or {}).get("orders", 0) or 0)
            if code == "ATR_STOP_LOSS":
                out["atr_stop_exit_count"] = orders
            if code == "ATR_TRAIL_EXIT":
                out["atr_trail_exit_count"] = orders
        if out["atr_stop_exit_count"] > 0 or out["atr_trail_exit_count"] > 0:
            return out

    if orders_df.empty or "reason_code" not in orders_df.columns:
        return {"atr_stop_exit_count": 0, "atr_trail_exit_count": 0}
    reason = orders_df["reason_code"].astype(str).str.upper()
    return {
        "atr_stop_exit_count": int(reason.eq("ATR_STOP_LOSS").sum()),
        "atr_trail_exit_count": int(reason.eq("ATR_TRAIL_EXIT").sum()),
    }


def _resolve_scan_fields(variant: dict[str, Any], orders_df: pd.DataFrame, trades_df: pd.DataFrame) -> dict[str, Any]:
    params = dict(variant.get("strategy", {}).get("params", {}) or {})
    mode = "M0"
    if bool(params.get("enable_atr_stop")) and not bool(params.get("enable_atr_trail")):
        mode = "M1"
    elif (not bool(params.get("enable_atr_stop"))) and bool(params.get("enable_atr_trail")):
        mode = "M2"
    elif bool(params.get("enable_atr_stop")) and bool(params.get("enable_atr_trail")):
        mode = "M3"

    exit_counts: dict[str, int] = {}
    exit_pcts: dict[str, float] = {}
    atr_trade_ids: list[int] = []
    if not orders_df.empty and "reason_code" in orders_df.columns:
        reason = orders_df["reason_code"].astype(str).str.strip()
        status = orders_df["status"].map(normalize_order_status) if "status" in orders_df.columns else pd.Series([""] * len(orders_df))
        side = orders_df["side"].astype(str).str.upper() if "side" in orders_df.columns else pd.Series([""] * len(orders_df))
        part = orders_df[status.isin(["FILLED", "PARTIAL_FILLED"]) & side.eq("SELL")]
        if not part.empty:
            vc = reason.loc[part.index].replace({"": "UNKNOWN"}).value_counts()
            exit_counts = {str(k): int(v) for k, v in vc.to_dict().items()}
            total = int(vc.sum())
            if total > 0:
                exit_pcts = {str(k): float(v) / float(total) for k, v in vc.to_dict().items()}
            atr_orders = part[reason.loc[part.index].isin(["ATR_STOP_LOSS", "ATR_TRAIL_EXIT"])]
            if not atr_orders.empty and "order_id" in atr_orders.columns and "order_id" in trades_df.columns and "trade_id" in trades_df.columns:
                ids = trades_df[trades_df["order_id"].isin(atr_orders["order_id"].tolist())]["trade_id"].dropna().tolist()
                try:
                    atr_trade_ids = sorted({int(x) for x in ids})
                except Exception:
                    atr_trade_ids = []

    def _k_to_str(v: Any, on: bool) -> str:
        if not on:
            return "NA"
        try:
            return f"{float(v):.1f}"
        except Exception:
            return "NA"

    return {
        "mode": mode,
        "enable_atr_stop": bool(params.get("enable_atr_stop", False)),
        "atr_stop_k": _k_to_str(params.get("atr_stop_k", 2.0), bool(params.get("enable_atr_stop", False))),
        "enable_atr_trail": bool(params.get("enable_atr_trail", False)),
        "atr_trail_k": _k_to_str(params.get("atr_trail_k", 3.0), bool(params.get("enable_atr_trail", False))),
        "exit_reason_counts": exit_counts,
        "exit_reason_pcts": exit_pcts,
        "atr_triggered_trade_ids": atr_trade_ids,
    }


def _run_variant_once(
    *,
    base_cfg: dict[str, Any],
    base_path: Path,
    exp_id: str,
    exp_dir: Path,
    variant: dict[str, Any],
    overrides: dict[str, Any],
    run_suffix: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    group_id = str(variant["group_id"])
    diff = {
        "strategy": variant["strategy"],
        "execution_profile": "tradable",
        "symbols": list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS),
        "start_date": overrides.get("start_date"),
        "end_date": overrides.get("end_date"),
        "benchmark": overrides.get("benchmark"),
        "compare_symbol": overrides.get("compare_symbol"),
        "initial_capital": overrides.get("initial_capital"),
        "sizing_mode": overrides.get("sizing_mode"),
        "order_value": overrides.get("order_value"),
        "reserve_cash_ratio": overrides.get("reserve_cash_ratio"),
        "lot_size": overrides.get("lot_size"),
        "univote_lot_mode": overrides.get("univote_lot_mode"),
    }
    run_id = f"run_{group_id.lower()}{run_suffix}_{exp_id[:8]}"
    run_dir = _results_root() / run_id
    temp_cfg_path = exp_dir / f"{group_id}{run_suffix}_config.json"
    cfg = apply_cfg_diff(base_cfg, diff)
    temp_cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    started = time.perf_counter()
    try:
        run_command(str(temp_cfg_path), str(run_dir))
        duration_ms = int((time.perf_counter() - started) * 1000)
        summary = _load_json(run_dir / "report_summary.json")
        run_meta = _load_json(run_dir / "run_meta.json")
        conclusions = _load_json(run_dir / "conclusions_10.json")
        warnings_list = _load_list(run_dir / "warnings.json")
        if warnings_list:
            summary.setdefault("audit", {}).setdefault("validation", {}).setdefault("warnings", warnings_list)
        orders_df = _load_csv(run_dir / "orders.csv")
        signals_df = _load_csv(run_dir / "signals.csv")
        trades_df = _load_csv(run_dir / "trades.csv")
        conc = summary.get("trade_concentration") or {}
        trade_top10 = {}
        if (not conc) or (conc.get("top10_contribution_pct") is None and int(conc.get("closed_trades", 0) or 0) > 0):
            trade_top10 = compute_top10_from_trade_pnl(run_dir / "trade_pnl.csv")
            if trade_top10.get("closed_trades", 0) > 0:
                (run_dir / "trade_concentration.json").write_text(
                    json.dumps(trade_top10, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        signal_stats = _resolve_signal_stats(summary, signals_df)
        execution_stats = _resolve_execution_stats(summary, orders_df)
        scan_fields = _resolve_scan_fields(variant, orders_df, trades_df)
        row = build_matrix_row(
            group_id=group_id,
            run_id=run_id,
            report_summary=summary,
            conclusions=conclusions,
            duration_ms=duration_ms,
            strategy_name=str(run_meta.get("strategy_name") or variant["strategy"]["name"]),
            strategy_params=dict(variant["strategy"].get("params", {}) or {}),
            strategy_name_full=str(run_meta.get("strategy_name") or variant["strategy"]["name"]),
            strategy_params_full=dict(run_meta.get("strategy_params", {}) or variant["strategy"].get("params", {}) or {}),
            status="OK",
            error_message=None,
            top10_fallback=trade_top10,
            signal_stats=signal_stats,
            execution_stats=execution_stats,
            scan_fields=scan_fields,
            warning_notes=str(trade_top10.get("warning_notes", "")),
        )
        run_rec = {
            "group_id": group_id,
            "run_id": run_id,
            "diff": diff,
            "artifacts": _artifact_paths(run_dir),
            "status": "OK",
            "error_message": None,
        }
        return row, run_rec
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        row = build_matrix_row(
            group_id=group_id,
            run_id=run_id,
            report_summary={},
            conclusions={},
            duration_ms=duration_ms,
            strategy_name=str(variant["strategy"]["name"]),
            strategy_params=dict(variant["strategy"].get("params", {}) or {}),
            strategy_name_full=str(variant["strategy"]["name"]),
            strategy_params_full=dict(variant["strategy"].get("params", {}) or {}),
            status="FAILED",
            error_message=err,
            top10_fallback={},
            signal_stats={},
            execution_stats={},
            scan_fields={},
            warning_notes=err,
        )
        run_rec = {
            "group_id": group_id,
            "run_id": run_id,
            "diff": diff,
            "artifacts": _artifact_paths(run_dir),
            "status": "FAILED",
            "error_message": err,
        }
        return row, run_rec


def _run_walkforward_validate(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    variants = preset_variants("ATR_PARAM_VALIDATE_v1")
    symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []

    start_s = str(overrides.get("start_date") or base_cfg.get("universe", {}).get("start"))
    end_s = str(overrides.get("end_date") or base_cfg.get("universe", {}).get("end"))
    folds = split_wf3(date.fromisoformat(start_s), date.fromisoformat(end_s))
    for f in folds:
        fold_overrides = dict(overrides)
        fold_overrides["start_date"] = f.test_start.isoformat()
        fold_overrides["end_date"] = f.test_end.isoformat()
        for v in variants:
            row, run_rec = _run_variant_once(
                base_cfg=base_cfg,
                base_path=base_path,
                exp_id=exp_id,
                exp_dir=exp_dir,
                variant=v,
                overrides=fold_overrides,
                run_suffix=f"_{f.fold_id.lower()}",
            )
            row["fold_id"] = f.fold_id
            row["segment"] = "test"
            row["train_start"] = f.train_start.isoformat()
            row["train_end"] = f.train_end.isoformat()
            row["test_start"] = f.test_start.isoformat()
            row["test_end"] = f.test_end.isoformat()
            rows.append(row)
            run_rec["fold_id"] = f.fold_id
            run_rec["segment"] = "test"
            runs.append(run_rec)

    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    baseline_ref = _write_baseline_ref(exp_dir, str(preset))
    _write_walkforward_report(exp_dir, rows, baseline_ref)
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
    }


def _run_universe_vote_validate(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    variants = preset_variants("ATR_PARAM_VALIDATE_v1")
    symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    vote_rows: list[dict[str, Any]] = []

    # Build simple universe snapshot from available symbols and recent ohlcv history.
    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["symbols"] = symbols
    from backtest_v2.config.loader import load_config  # lazy import to avoid cycles
    from backtest_v2.data.pg_provider import PostgresDataProvider

    tmp_cfg = exp_dir / "_universe_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "feature_calc_seconds": 0.0,
        "simulation_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }
    perf: dict[str, Any] = {
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
        "db_load_seconds": 0.0,
        "feature_calc_seconds": 0.0,
        "simulation_seconds": 0.0,
    }
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    ohlcv = provider.load_ohlcv_daily(symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig())
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)

    use_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else symbols
    baseline_ref = _write_baseline_ref(exp_dir, str(preset))
    b_ret = _safe_num((baseline_ref.get("baseline_metrics") or {}).get("total_return"))
    b_mdd = _safe_num((baseline_ref.get("baseline_metrics") or {}).get("max_drawdown"))

    for sym in use_symbols:
        sym_overrides = dict(overrides)
        sym_overrides["symbols"] = [sym]
        for v in variants:
            row, run_rec = _run_variant_once(
                base_cfg=base_cfg,
                base_path=base_path,
                exp_id=exp_id,
                exp_dir=exp_dir,
                variant=v,
                overrides=sym_overrides,
                run_suffix=f"_{sym.lower().replace('.', '')}",
            )
            row["symbol"] = sym
            rows.append(row)
            runs.append(run_rec)

            r_ret = _safe_num(row.get("total_return"))
            r_mdd = _safe_num(row.get("max_drawdown"))
            delta_ret = (r_ret - b_ret) if b_ret is not None and r_ret is not None else None
            delta_mdd = (r_mdd - b_mdd) if b_mdd is not None and r_mdd is not None else None
            passed = False
            if b_ret is not None and b_mdd is not None and r_ret is not None and r_mdd is not None:
                passed = (r_mdd <= b_mdd * 0.8) and (r_ret >= b_ret - 0.005)
            vote_rows.append(
                {
                    "symbol": sym,
                    "baseline_return": b_ret,
                    "baseline_mdd": b_mdd,
                    "group_id": row.get("group_id"),
                    "return": r_ret,
                    "mdd": r_mdd,
                    "atr_exit_rate": _safe_num(row.get("atr_exit_rate")),
                    "score": _safe_num(row.get("score")),
                    "delta_return": delta_ret,
                    "delta_mdd": delta_mdd,
                    "pass": bool(passed),
                }
            )

    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    pd.DataFrame(vote_rows).to_csv(exp_dir / "universe_vote.csv", index=False)
    _write_universe_vote_report(exp_dir, vote_rows, excluded, len(cands), len(use_symbols), baseline_ref)
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
    }


def _run_universe_vote_validate_v2(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    overrides = dict(overrides or {})
    # UNIVOTE_v2 default window: recent 12 months.
    end_s = str(overrides.get("end_date") or date.today().isoformat())
    try:
        end_d = date.fromisoformat(end_s)
    except Exception:
        end_d = date.today()
        end_s = end_d.isoformat()
    start_s = str(overrides.get("start_date") or (end_d - timedelta(days=365)).isoformat())
    overrides["start_date"] = start_s
    overrides["end_date"] = end_s

    variants = preset_variants("ATR_PARAM_VALIDATE_UNIVOTE_V2")
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["symbols"] = seed_symbols
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    from backtest_v2.config.loader import load_config  # lazy import to avoid cycles
    from backtest_v2.data.pg_provider import PostgresDataProvider

    tmp_cfg = exp_dir / "_universe_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    universe_limit_raw = overrides.get("universe_limit", 100)
    universe_limit = int(universe_limit_raw) if universe_limit_raw is not None else None
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    universe_meta: dict[str, Any] = {}
    try:
        use_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        universe_meta = dict(exc.meta or {})
        universe_meta.setdefault("symbols_source", "db_index_constituents")
        universe_meta.setdefault("constituents_load_status", exc.status)
        universe_meta.setdefault("universe_limit", universe_limit)
        universe_meta.setdefault("raw_count", 0)
        rows = []
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        for v in variants:
            rows.append(
                {
                    "group_id": v["group_id"],
                    "status": "FAILED",
                    "strategy": "sma_cross_regime_atr",
                    "strategy_name": "sma_cross_regime_atr",
                    "strategy_name_full": "sma_cross_regime_atr",
                    "params_json": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                    "params_json_full": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                    "run_id": "",
                    "error_message": err,
                    "error": err,
                    "warning_notes": f"status={exc.status}",
                    "note": (
                        f"source={universe_meta.get('symbols_source')}; "
                        f"status={universe_meta.get('constituents_load_status')}; "
                        f"universe_limit={universe_meta.get('universe_limit')}"
                    ),
                    "universe_raw_count": int(universe_meta.get("raw_count", 0) or 0),
                    "universe_filtered_count": 0,
                }
            )
        pd.DataFrame(
            [{"date": str(cfg_obj.universe.end), "symbol": "", "in_hs300": False, "in_csi500": False, "tradable": False, "excluded_reason": "UNIVERSE_LOAD_FAILED"}]
        ).to_csv(exp_dir / "universe_snapshot.csv", index=False)
        report_path = write_universe_load_failed_report(
            universe_meta=universe_meta,
            run_overrides=overrides,
            message=str(exc),
        )
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        zip_path = _write_experiment_zip(exp_id, exp_dir, [])
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "zip_path": str(zip_path),
            "rows": rows,
            "matrix_head": rows[: min(4, len(rows))],
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(use_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig())
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    use_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []

    lot_mode = str(overrides.get("univote_lot_mode", "LOT100_MIN1LOT"))
    initial_cash = float(overrides.get("initial_capital", 1_000_000.0))
    base_order_value = float(overrides.get("base_order_value", 10_000.0))
    lot_size = int(overrides.get("lot_size", 100))
    v2cfg = UnivoteV2Config(
        initial_cash=initial_cash,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        lot_size=lot_size,
    )

    def _run_one(symbol: str, variant: dict[str, Any], ov: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return _run_variant_once(
            base_cfg=base_cfg,
            base_path=base_path,
            exp_id=exp_id,
            exp_dir=exp_dir,
            variant=variant,
            overrides=ov,
            run_suffix=f"_{symbol.lower().replace('.', '')}",
        )

    run_overrides = dict(overrides)
    run_overrides.setdefault("start_date", overrides.get("start_date"))
    run_overrides.setdefault("end_date", overrides.get("end_date"))
    run_overrides.setdefault("benchmark", overrides.get("benchmark"))
    run_overrides.setdefault("compare_symbol", overrides.get("compare_symbol"))
    run_overrides["sizing_mode"] = "fixed_value"
    run_overrides["order_value"] = base_order_value
    run_overrides["initial_capital"] = initial_cash
    run_overrides["lot_size"] = 1 if lot_mode.upper() == "ODDLOT_1" else lot_size
    run_overrides["univote_lot_mode"] = lot_mode
    run_overrides["universe_limit"] = universe_limit
    run_overrides["symbols_source"] = universe_meta.get("symbols_source", "db_index_constituents")
    run_overrides["constituents_load_status"] = universe_meta.get("constituents_load_status", "OK")

    raw_count = int(universe_meta.get("raw_count", len(use_symbols)) or 0)
    if raw_count < 100:
        rows = build_universe_too_small_rows(
            raw_count=raw_count,
            status=str(universe_meta.get("constituents_load_status", "OK")),
            source=str(universe_meta.get("symbols_source", "db_index_constituents")),
            universe_limit=universe_meta.get("universe_limit"),
        )
        detail_rows: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        report_path = write_universe_load_failed_report(
            universe_meta=universe_meta,
            run_overrides=run_overrides,
            message="UNIVERSE_TOO_SMALL",
            raw_count=raw_count,
            filtered_count=len(use_symbols),
            universe_incomplete=True,
        )
    else:
        rows, detail_rows, runs, report_path = run_univote_v2(
            exp_dir=exp_dir,
            symbols=use_symbols,
            universe_raw_count=raw_count,
            universe_filtered_count=len(use_symbols),
            excluded_reason_counts=excluded,
            run_variant_fn=_run_one,
            run_overrides=run_overrides,
            cfg=v2cfg,
            universe_meta=universe_meta,
            universe_incomplete=False,
        )

    pd.DataFrame(detail_rows).to_csv(exp_dir / "univote_details.csv", index=False)
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_portfolio_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("ATR_PORTFOLIO_VALIDATE_V1")
    overrides = dict(overrides or {})
    symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    # defaults: shorter range for faster portfolio validate runs
    start_s = str(overrides.get("start_date") or "2025-02-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit_raw = overrides.get("universe_limit", 100)
    universe_limit = int(universe_limit_raw) if universe_limit_raw is not None else None
    allow_user_override = bool(overrides.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = symbols

    tmp_cfg = exp_dir / "_portfolio_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "feature_calc_seconds": 0.0,
        "simulation_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v["group_id"]),
                "status": "FAILED",
                "strategy_name": str(v["strategy"]["name"]),
                "strategy_name_full": str(v["strategy"]["name"]),
                "params_json": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "params_json_full": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
                "universe_filtered_count": 0,
                "max_positions": max_positions,
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            "ATR_PORTFOLIO_VALIDATE_v1.md",
            "\n".join(
                [
                    "# ATR Portfolio Validate v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- max_positions: {max_positions}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig())
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    if filtered_count <= 0:
        err = "UNIVERSE_FILTERED_EMPTY"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": "no symbols after filters",
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_full_range_validate_report(
                rows=rows,
                runs=[],
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                raw_count=raw_count,
                filtered_count=filtered_count,
                excluded=excluded,
                initial_capital=initial_capital,
                max_positions=max_positions,
                universe_limit=universe_limit,
                perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    if raw_count < 100:
        err = (
            f"UNIVERSE_TOO_SMALL(raw_count={raw_count}, "
            f"status={universe_meta.get('constituents_load_status')}, "
            f"source={universe_meta.get('symbols_source')}, "
            f"limit={universe_meta.get('universe_limit')})"
        )
        rows = [
            {
                "group_id": str(v["group_id"]),
                "status": "FAILED",
                "strategy_name": str(v["strategy"]["name"]),
                "strategy_name_full": str(v["strategy"]["name"]),
                "params_json": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "params_json_full": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "error_message": err,
                "error": err,
                "warning_notes": "universe_incomplete=true",
                "universe_raw_count": raw_count,
                "universe_filtered_count": filtered_count,
                "max_positions": max_positions,
            }
            for v in variants
        ]
        report_path = write_report_to_local(
            "ATR_PORTFOLIO_VALIDATE_v1.md",
            _build_portfolio_report_error(
                start_s=start_s,
                end_s=end_s,
                universe_limit=universe_limit,
                initial_capital=initial_capital,
                max_positions=max_positions,
                raw_count=raw_count,
                filtered_count=filtered_count,
                error=err,
            ),
        )
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": runs,
            "single_report_path": str(report_path),
        }

    # Load benchmark + calendar once for both groups.
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    portfolio_run_id = f"run_portfolio_{exp_id[:12]}"

    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] 组合分组 {group_id} 运行中")
        run_id = portfolio_run_id
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)

        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": variant["strategy"],
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_portfolio_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None)
        if strategy_cfg is None:
            strategy_def = dict(variant.get("strategy", {}) or {})
            strategy_cfg = StrategyConfig(
                name=str(strategy_def.get("name", "sma_cross")),
                params=dict(strategy_def.get("params", {}) or {}),
            )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(variant["strategy"]["name"]),
            "strategy_name_full": str(variant["strategy"]["name"]),
            "params_json": json.dumps(variant["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(variant["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "signal_too_sparse" if float(m.get("avg_positions", 0.0)) < 3.0 else "",
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "diff": {
                    "strategy": variant["strategy"],
                    "universe_filtered_count": filtered_count,
                    "max_positions": max_positions,
                    "initial_capital": initial_capital,
                },
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] 组合分组 {group_id} 完成")

    for row in rows:
        run_rec = next((x for x in runs if str(x.get("group_id")) == str(row.get("group_id"))), None)
        if run_rec is None:
            continue
        daily_path = Path(str((run_rec.get("artifacts") or {}).get("portfolio_daily_csv", "")))
        if (not daily_path.exists()) or daily_path.stat().st_size == 0:
            row["status"] = "FAILED"
            row["error_message"] = "DAILY_SERIES_MISSING"
            row["error"] = "DAILY_SERIES_MISSING"
            row["warning_notes"] = "MISSING_DAILY_SERIES"
            run_rec["status"] = "FAILED"
            run_rec["error_message"] = "DAILY_SERIES_MISSING"

    _emit_progress(progress_hook, 90, "写出组合实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_path = write_report_to_local(
        "ATR_PORTFOLIO_VALIDATE_v1.md",
        _build_portfolio_validate_report(
            rows=rows,
            runs=runs,
            start_s=start_s,
            end_s=end_s,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_limit=universe_limit,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "组合实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_portfolio_fill_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("PORTFOLIO_FILL_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2023-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    lot_size = int(overrides.get("lot_size", 100))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    # Speed-first default for this experiment; can still be overridden by request.
    forced_universe_limit_raw = overrides.get("universe_limit", 50)
    forced_universe_limit = int(forced_universe_limit_raw) if forced_universe_limit_raw is not None else None

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_portfolio_fill_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    report_universe_meta: dict[str, Any] = {}
    report_excluded: dict[str, int] = {}

    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_cfg = dict(variant.get("portfolio", {}) or {})
        universe_cfg = dict(variant.get("universe", {}) or {})
        max_positions = int(portfolio_cfg.get("max_positions", 10))
        universe_limit = forced_universe_limit if forced_universe_limit is not None else int(universe_cfg.get("limit", 100))
        min_turnover = float(universe_cfg.get("min_avg_turnover_20d", 1e8))
        _emit_progress(
            progress_hook,
            10 + int((idx - 1) * 75 / total_variants),
            f"[{idx}/{total_variants}] {group_id} 运行中（max_positions={max_positions}, limit={universe_limit}, turnover>={int(min_turnover)}）",
        )

        try:
            raw_symbols, universe_meta = resolve_universe_symbols(
                as_of=cfg_obj.universe.end,
                db_loader=lambda as_of, lim: load_constituents_from_db(
                    db=cfg_obj.db,
                    runtime=cfg_obj.db_runtime,
                    as_of=as_of,
                    universe_limit=lim,
                ),
                user_symbols=seed_symbols,
                allow_user_override=allow_user_override,
                universe_limit=universe_limit,
            )
            report_universe_meta = dict(universe_meta)
        except UniverseLoadError as exc:
            err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
            row = {
                "group_id": group_id,
                "status": "FAILED",
                "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
                "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
                "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
                "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
                "run_id": "",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
                "universe_filtered_count": 0,
                "max_positions": max_positions,
                "trades_count": 0,
                "avg_positions": 0.0,
                "turnover_avg": 0.0,
            }
            rows.append(row)
            runs.append({"group_id": group_id, "run_id": "", "status": "FAILED", "error_message": err, "artifacts": {}})
            _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 失败：{err}")
            continue

        ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
        cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
        filtered, snapshot, excluded = apply_universe_filters(
            cands,
            UniverseFilterConfig(min_avg_turnover_20d=min_turnover),
        )
        report_excluded.update(excluded)
        filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
        (exp_dir / f"{group_id}_universe_snapshot.csv").write_text(snapshot.to_csv(index=False), encoding="utf-8")
        raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
        filtered_count = int(len(filtered_symbols))

        if raw_count < 100:
            err = (
                f"UNIVERSE_TOO_SMALL(raw_count={raw_count}, status={universe_meta.get('constituents_load_status')}, "
                f"source={universe_meta.get('symbols_source')}, limit={universe_meta.get('universe_limit')})"
            )
            rows.append(
                {
                    "group_id": group_id,
                    "status": "FAILED",
                    "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
                    "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
                    "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
                    "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
                    "run_id": "",
                    "error_message": err,
                    "error": err,
                    "warning_notes": "universe_incomplete=true",
                    "universe_filtered_count": filtered_count,
                    "max_positions": max_positions,
                    "trades_count": 0,
                    "avg_positions": 0.0,
                    "turnover_avg": 0.0,
                }
            )
            runs.append({"group_id": group_id, "run_id": "", "status": "FAILED", "error_message": err, "artifacts": {}})
            _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 失败：Universe 太小")
            continue

        benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
        calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
        day_list = [d.date() for d in calendar]

        run_id = f"run_portfolio_fill_{exp_id[:10]}"
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_portfolio_fill_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None)
        if strategy_cfg is None:
            strategy_cfg = StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)

        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "max_positions": max_positions,
                    "universe_limit": universe_limit,
                    "min_avg_turnover_20d": min_turnover,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "universe_limit": universe_limit,
            "min_avg_turnover_20d": min_turnover,
            "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "UNDERFILLED_PORTFOLIO" if float(m.get("avg_positions", 0.0)) < (max_positions * 0.5) else "",
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "diff": {
                    "strategy": strategy_def,
                    "max_positions": max_positions,
                    "universe_limit": universe_limit,
                    "min_avg_turnover_20d": min_turnover,
                    "initial_capital": initial_capital,
                },
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv"),
                    "universe_snapshot_csv": str(exp_dir / f"{group_id}_universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出持仓密度实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_path = write_report_to_local(
        "PORTFOLIO_FILL_VALIDATE_v1.md",
        _build_portfolio_fill_validate_report(
            rows=rows,
            start_s=start_s,
            end_s=end_s,
            initial_capital=initial_capital,
            universe_meta=report_universe_meta,
            excluded=report_excluded,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "持仓密度实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_trend_filter_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("TREND_FILTER_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2023-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", 1000))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_trend_filter_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v["group_id"]),
                "status": "FAILED",
                "strategy_name": str(v["strategy"]["name"]),
                "strategy_name_full": str(v["strategy"]["name"]),
                "params_json": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "params_json_full": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
                "universe_filtered_count": 0,
                "max_positions": max_positions,
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            "TREND_FILTER_VALIDATE_v1.md",
            "\n".join(
                [
                    "# TREND_FILTER_VALIDATE_v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))

    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_trend_filter_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_def = dict(variant.get("strategy", {}) or {})
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_trend_filter_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None)
        if strategy_cfg is None:
            strategy_cfg = StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        m = dict(result.metrics)
        blocked_cnt = int(m.get("blocked_by_trend_count", 0))
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "blocked_by_trend_count": blocked_cnt,
            "trend_filter_block_rate": float(m.get("trend_filter_block_rate", 0.0)),
            "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "OVER_FILTERED_SYSTEM" if (group_id == "TREND_FILTER_ON" and float(m.get("avg_positions", 0.0)) < 3.0) else "",
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "diff": {
                    "strategy": strategy_def,
                    "universe_limit": universe_limit,
                    "min_avg_turnover_20d": 1e8,
                    "max_positions": max_positions,
                    "initial_capital": initial_capital,
                },
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出趋势过滤实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_path = write_report_to_local(
        "TREND_FILTER_VALIDATE_v1.md",
        _build_trend_filter_validate_report(
            rows=rows,
            start_s=start_s,
            end_s=end_s,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_limit=universe_limit,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "趋势过滤实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_trend_tier_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("TREND_TIER_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2021-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", 1000))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    fast_mode = bool(overrides.get("fast_mode", True))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_trend_tier_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [{"group_id": str(v["group_id"]), "status": "FAILED", "error": err, "error_message": err, "warning_notes": str(exc)} for v in variants]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            TREND_TIER_REPORT_FILE,
            "\n".join(
                [
                    "# TREND_TIER_VALIDATE_v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))

    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_trend_tier_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_def = dict(variant.get("portfolio", {}) or {})
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_trend_tier_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None) or StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(portfolio_def.get("engine_mode", "VECTOR_FAST_STRICT")),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "",
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出趋势分档实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_md = _build_trend_tier_validate_report(
        rows=rows,
        start_s=start_s,
        end_s=end_s,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
    )
    report_path = write_report_to_local(TREND_TIER_REPORT_FILE, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    (host_reports / TREND_TIER_REPORT_FILE).write_text(report_md, encoding="utf-8")
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "趋势分档实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(host_reports / TREND_TIER_REPORT_FILE),
    }


def _run_trend_accel_brake_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("TREND_ACCEL_BRAKE_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2021-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", 1000))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    fast_mode = bool(overrides.get("fast_mode", True))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_trend_accel_brake_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [{"group_id": str(v["group_id"]), "status": "FAILED", "error": err, "error_message": err, "warning_notes": str(exc)} for v in variants]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            TREND_ACCEL_BRAKE_REPORT_FILE,
            "\n".join(
                [
                    "# TREND_ACCEL_BRAKE_VALIDATE_v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))

    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_trend_accel_brake_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_def = dict(variant.get("portfolio", {}) or {})
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_trend_accel_brake_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None) or StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(portfolio_def.get("engine_mode", "VECTOR_FAST_STRICT")),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "STRONG_TREND_BUY_RATE": float(m.get("STRONG_TREND_BUY_RATE", 0.0)),
            "TREND_BREAK_EXIT_RATE": float(m.get("TREND_BREAK_EXIT_RATE", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "",
            "perf_db_load_seconds": float(m.get("db_load_seconds", 0.0)),
            "perf_precompute_seconds": float(m.get("precompute_seconds", 0.0)),
            "perf_simulation_seconds": float(m.get("simulation_seconds", 0.0)),
            "perf_io_write_seconds": float(m.get("io_write_seconds", 0.0)),
            "perf_db_queries": int(m.get("db_queries", 0)),
            "perf_cache_hit_rate": float(m.get("cache_hit_rate", 0.0)),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出加速刹车实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_md = _build_trend_accel_brake_validate_report(
        rows=rows,
        run_root=_results_root() / run_id,
        start_s=start_s,
        end_s=end_s,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
    )
    report_path = write_report_to_local(TREND_ACCEL_BRAKE_REPORT_FILE, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    (host_reports / TREND_ACCEL_BRAKE_REPORT_FILE).write_text(report_md, encoding="utf-8")
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "加速刹车实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(host_reports / TREND_ACCEL_BRAKE_REPORT_FILE),
    }


def _run_trend_brake_confirm_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("TREND_BRAKE_CONFIRM_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2021-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", 1000))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    fast_mode = bool(overrides.get("fast_mode", True))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_trend_brake_confirm_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [{"group_id": str(v["group_id"]), "status": "FAILED", "error": err, "error_message": err, "warning_notes": str(exc)} for v in variants]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            TREND_BRAKE_CONFIRM_REPORT_FILE,
            "\n".join(
                [
                    "# TREND_BRAKE_CONFIRM_VALIDATE_v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))

    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_trend_brake_confirm_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_def = dict(variant.get("portfolio", {}) or {})
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_trend_brake_confirm_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None) or StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(portfolio_def.get("engine_mode", "VECTOR_FAST_STRICT")),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "STRONG_TREND_BUY_RATE": float(m.get("STRONG_TREND_BUY_RATE", 0.0)),
            "TREND_BREAK_EXIT_RATE": float(m.get("TREND_BREAK_EXIT_RATE", 0.0)),
            "TREND_BREAK_EXIT_COUNT": int(m.get("TREND_BREAK_EXIT_COUNT", 0)),
            "mean_break_streak_at_exit": float(m.get("mean_break_streak_at_exit", 0.0)),
            "days_held_median": float(m.get("days_held_median", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
            "strength_ma": int(m.get("strength_ma", 120)),
            "warning_notes": "",
            "perf_db_load_seconds": float(m.get("db_load_seconds", 0.0)),
            "perf_precompute_seconds": float(m.get("precompute_seconds", 0.0)),
            "perf_simulation_seconds": float(m.get("simulation_seconds", 0.0)),
            "perf_io_write_seconds": float(m.get("io_write_seconds", 0.0)),
            "perf_db_queries": int(m.get("db_queries", 0)),
            "perf_cache_hit_rate": float(m.get("cache_hit_rate", 0.0)),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出确认型趋势刹车实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_md = _build_trend_brake_confirm_validate_report(
        rows=rows,
        run_root=_results_root() / run_id,
        start_s=start_s,
        end_s=end_s,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
    )
    report_path = write_report_to_local(TREND_BRAKE_CONFIRM_REPORT_FILE, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    (host_reports / TREND_BRAKE_CONFIRM_REPORT_FILE).write_text(report_md, encoding="utf-8")
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "确认型趋势刹车实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(host_reports / TREND_BRAKE_CONFIRM_REPORT_FILE),
    }


def _run_strength_rank_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    variants = preset_variants("STRENGTH_RANK_VALIDATE_V1")
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(overrides.get("start_date") or "2023-01-01")
    end_s = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    max_positions = int(overrides.get("max_positions", 10))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", 50))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_strength_rank_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v["group_id"]),
                "status": "FAILED",
                "strategy_name": str(v["strategy"]["name"]),
                "strategy_name_full": str(v["strategy"]["name"]),
                "params_json": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "params_json_full": json.dumps(v["strategy"].get("params", {}), ensure_ascii=False, sort_keys=True),
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
                "universe_filtered_count": 0,
                "max_positions": max_positions,
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            "STRENGTH_RANK_VALIDATE_v1.md",
            "\n".join(
                [
                    "# STRENGTH_RANK_VALIDATE_v1",
                    "",
                    "## 1) 运行信息",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    ohlcv = provider.load_ohlcv_daily(raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, source="stocks")
    cands = build_candidates_from_ohlcv(ohlcv, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))

    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_strength_rank_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant["group_id"])
        _emit_progress(progress_hook, 10 + int((idx - 1) * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_def = dict(variant.get("portfolio", {}) or {})
        strength_rank_enabled = bool(portfolio_def.get("enable_strength_rank", False))
        strength_ma = int(portfolio_def.get("strength_ma", 120))
        strength_top_k_mode = str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX"))
        cfg_v = apply_cfg_diff(
            bt_cfg,
            {
                "strategy": strategy_def,
                "execution_profile": "tradable",
                "symbols": filtered_symbols,
                "start_date": start_s,
                "end_date": end_s,
                "benchmark": benchmark,
                "initial_capital": initial_capital,
                "lot_size": lot_size,
            },
        )
        tmp_v = exp_dir / f"{group_id}_strength_rank_cfg.json"
        tmp_v.write_text(json.dumps(cfg_v, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg_obj_v, _ = load_config(str(tmp_v))
        strategy_cfg = getattr(cfg_obj_v, "strategy", None)
        if strategy_cfg is None:
            strategy_cfg = StrategyConfig(name=str(strategy_def.get("name", "sma_cross_regime_atr")), params=dict(strategy_def.get("params", {}) or {}))
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        result = run_portfolio_backtest(
            strategy=strategy,
            history=ohlcv,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=strength_rank_enabled,
            strength_ma=strength_ma,
            strength_top_k_mode=strength_top_k_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)
        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": strength_rank_enabled,
                    "strength_ma": strength_ma,
                    "strength_top_k_mode": strength_top_k_mode,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "volatility": float(m.get("vol_annual", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "blocked_by_trend_count": int(m.get("blocked_by_trend_count", 0)),
            "trend_filter_block_rate": float(m.get("trend_filter_block_rate", 0.0)),
            "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "strength_rank_enabled": bool(m.get("strength_rank_enabled", strength_rank_enabled)),
            "strength_ma": int(m.get("strength_ma", strength_ma)),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "diff": {
                    "strategy": strategy_def,
                    "universe_limit": universe_limit,
                    "min_avg_turnover_20d": 1e8,
                    "max_positions": max_positions,
                    "initial_capital": initial_capital,
                    "enable_strength_rank": strength_rank_enabled,
                    "strength_ma": strength_ma,
                    "strength_top_k_mode": strength_top_k_mode,
                },
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 10 + int(idx * 75 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出强度排序实验结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_path = write_report_to_local(
        "STRENGTH_RANK_VALIDATE_v1.md",
        _build_strength_rank_validate_report(
            rows=rows,
            start_s=start_s,
            end_s=end_s,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_limit=universe_limit,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "强度排序实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_strength_rank_validate_v2(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = STRENGTH_RANK_V2_REPORT_FILE,
    warning_label: str = "RANK_RARELY_TRIGGERED",
) -> dict[str, Any]:
    variants = preset_variants(str(preset))
    overrides = dict(overrides or {})
    seed_symbols = list(overrides.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    fast_mode = bool(overrides.get("fast_mode", False))
    # v2 defaults
    full_start = str(overrides.get("start_date") or ("2024-01-01" if fast_mode else "2022-01-01"))
    full_end = str(overrides.get("end_date") or "2026-02-01")
    benchmark = str(overrides.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(overrides.get("initial_capital", 1_000_000.0))
    default_max_positions = int((variants[0].get("portfolio", {}) if variants else {}).get("max_positions", 10))
    max_positions = int(overrides.get("max_positions", default_max_positions))
    lot_size = int(overrides.get("lot_size", 100))
    universe_limit = int(overrides.get("universe_limit", (200 if fast_mode else 500)))
    allow_user_override = bool(overrides.get("universe_use_user_override", False))
    debug_full_artifacts = bool(overrides.get("debug_full_artifacts", False))
    history_lookback_days = int(overrides.get("history_lookback_days", 260))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = full_start
    bt_cfg["universe"]["end"] = full_end
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_strength_rank_v2_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "feature_calc_seconds": 0.0,
        "simulation_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {"group_id": "ALL_BASE", "status": "FAILED", "error_message": err, "error": err},
            {"group_id": "ALL_RANK", "status": "FAILED", "error_message": err, "error": err},
            {"group_id": "WF_TEST_MEDIAN_DELTA", "status": "FAILED", "error_message": err, "error": err},
            {"group_id": "REGIME_SCORE", "status": "FAILED", "error_message": err, "error": err},
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            "\n".join(
                [
                    "# STRENGTH_RANK_VALIDATE_v2",
                    "",
                    "## 1) 运行信息",
                    f"- full_range: {full_start} ~ {full_end}",
                    f"- universe_limit: {universe_limit}",
                    f"- initial_cash: {initial_capital:.0f}",
                    f"- max_positions: {max_positions}",
                    f"- ERROR: {err}",
                    "",
                ]
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    universe_too_small = filtered_count < 100

    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df_all = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    ohlcv_all = ohlcv_all.copy()
    ohlcv_all["date"] = pd.to_datetime(ohlcv_all["date"])
    benchmark_df_all = benchmark_df_all.copy()
    benchmark_df_all["date"] = pd.to_datetime(benchmark_df_all["date"])
    full_calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    full_calendar_days = [d.date() for d in full_calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    regimes = list((variants[0].get("regimes") if variants else []) or [])
    wf_folds = list((variants[0].get("wf_folds") if variants else []) or [])
    wf_folds, skipped_wf = _split_wf_folds_for_range(full_start, full_end, wf_folds)
    if fast_mode:
        regimes = [x for x in regimes if str(x.get("name", "")).upper() in {"P3"}]
        wf_folds = [x for x in wf_folds if str(x.get("name", "")).upper() in {"FOLD2", "FOLD3"}]
    slices: list[dict[str, str]] = [{"name": "ALL", "start": full_start, "end": full_end}]
    slices.extend([{"name": str(x.get("name", "")), "start": str(x.get("start", "")), "end": str(x.get("end", ""))} for x in regimes])
    slices.extend(
        [
            {
                "name": str(x.get("name", "")),
                "start": str(x.get("test_start", "")),
                "end": str(x.get("test_end", "")),
                "train_start": str(x.get("train_start", "")),
                "train_end": str(x.get("train_end", "")),
            }
            for x in wf_folds
        ]
    )

    run_id = f"run_strength_rank_v2_{exp_id[:10]}"
    runs: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    # Pre-slice market data once for all groups.
    sliced_data: dict[str, tuple[pd.DataFrame, pd.DataFrame, list[date]]] = {}
    t1 = time.perf_counter()
    for sl in slices:
        s_name = str(sl.get("name", ""))
        s_start = str(sl.get("start", ""))
        s_end = str(sl.get("end", ""))
        s_start_ts = pd.Timestamp(s_start)
        s_end_ts = pd.Timestamp(s_end)
        hist_slice = ohlcv_all[(ohlcv_all["date"] >= s_start_ts) & (ohlcv_all["date"] <= s_end_ts)].copy()
        bench_slice = benchmark_df_all[(benchmark_df_all["date"] >= s_start_ts) & (benchmark_df_all["date"] <= s_end_ts)].copy()
        day_list = [d for d in full_calendar_days if (d >= s_start_ts.date() and d <= s_end_ts.date())]
        sliced_data[s_name] = (hist_slice, bench_slice, day_list)
    perf["feature_calc_seconds"] = float(time.perf_counter() - t1)

    total_jobs = max(len(slices) * len(variants), 1)
    job_idx = 0
    t2 = time.perf_counter()
    for sl in slices:
        s_name = str(sl.get("name", ""))
        s_start = str(sl.get("start", ""))
        s_end = str(sl.get("end", ""))
        hist_slice, bench_slice, day_list = sliced_data.get(s_name, (pd.DataFrame(), pd.DataFrame(), []))
        for variant in variants:
            job_idx += 1
            group_id = str(variant.get("group_id", ""))
            _emit_progress(progress_hook, 10 + int(job_idx * 75 / total_jobs), f"[{job_idx}/{total_jobs}] {s_name}-{group_id} 运行中")
            run_dir = _results_root() / run_id / s_name / group_id
            run_dir.mkdir(parents=True, exist_ok=True)
            strategy_def = dict(variant.get("strategy", {}) or {})
            portfolio_def = dict(variant.get("portfolio", {}) or {})
            strategy_cfg = StrategyConfig(
                name=str(strategy_def.get("name", "sma_cross_regime_atr")),
                params=dict(strategy_def.get("params", {}) or {}),
            )
            strategy = build_strategy(strategy_cfg)
            strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
            result = run_portfolio_backtest(
                strategy=strategy,
                history=hist_slice,
                benchmark=bench_slice,
                trading_days=day_list,
                benchmark_symbol=benchmark,
                initial_cash=initial_capital,
                max_positions=max_positions,
                lot_size=lot_size,
                enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
                strength_ma=int(portfolio_def.get("strength_ma", 120)),
                strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
                history_lookback_days=history_lookback_days,
                engine_mode=str(overrides.get("engine_mode") or portfolio_def.get("engine_mode") or "SLOW_AUDIT"),
            )
            write_artifacts = debug_full_artifacts or (s_name == "ALL")
            if write_artifacts:
                result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
                result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
                result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)
            m = dict(result.metrics)
            detail_rows.append(
                {
                    "slice_name": s_name,
                    "slice_start": s_start,
                    "slice_end": s_end,
                    "train_start": sl.get("train_start"),
                    "train_end": sl.get("train_end"),
                    "segment": "WF_TEST" if s_name.startswith("Fold") else ("REGIME" if s_name.startswith("P") else "ALL"),
                    "group_id": group_id,
                    "run_id": run_id,
                    "total_return": float(m.get("total_return", 0.0)),
                    "cagr": float(m.get("cagr", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "vol_annual": float(m.get("vol_annual", 0.0)),
                    "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
                    "trades": int(m.get("trades", 0)),
                    "avg_positions": float(m.get("avg_positions", 0.0)),
                    "turnover_avg": float(m.get("turnover_avg", 0.0)),
                    "universe_filtered_count": filtered_count,
                    "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
                    "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
                    "avg_buy_slots_per_day": float(m.get("avg_buy_slots_per_day", 0.0)),
                    "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
                    "rank_trigger_days": int(m.get("rank_trigger_days", 0)),
                    "rank_trigger_rate": float(m.get("rank_trigger_rate", 0.0)),
                    "strength_rank_enabled": bool(m.get("strength_rank_enabled", False)),
                    "strength_ma": int(m.get("strength_ma", 120)),
                }
            )
            runs.append(
                {
                    "group_id": f"{s_name}:{group_id}",
                    "run_id": run_id,
                    "status": "OK",
                    "error_message": None,
                    "artifacts": {
                        "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv") if write_artifacts else "",
                        "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv") if write_artifacts else "",
                        "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if write_artifacts else "",
                    },
                }
            )
    perf["simulation_seconds"] = float(time.perf_counter() - t2)

    details_df = pd.DataFrame(detail_rows)
    details_df.to_csv(exp_dir / "strength_rank_v2_details.csv", index=False)

    def _pick(seg: str, name: str, gid: str) -> dict[str, Any]:
        hit = details_df[
            (details_df["segment"] == seg) & (details_df["slice_name"] == name) & (details_df["group_id"] == gid)
        ]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_base = _pick("ALL", "ALL", "BASE")
    all_rank = _pick("ALL", "ALL", "RANK_ON")

    wf_delta_rows: list[dict[str, Any]] = []
    regime_delta_rows: list[dict[str, Any]] = []
    for name in [str(x.get("name", "")) for x in wf_folds]:
        b = _pick("WF_TEST", name, "BASE")
        r = _pick("WF_TEST", name, "RANK_ON")
        wf_delta_rows.append(
            {
                "fold": name,
                "delta_ret": _delta(r.get("total_return"), b.get("total_return")),
                "delta_mdd": _delta(r.get("max_drawdown"), b.get("max_drawdown")),
            }
        )
    for name in [str(x.get("name", "")) for x in regimes]:
        b = _pick("REGIME", name, "BASE")
        r = _pick("REGIME", name, "RANK_ON")
        regime_delta_rows.append(
            {
                "regime": name,
                "delta_ret": _delta(r.get("total_return"), b.get("total_return")),
                "delta_mdd": _delta(r.get("max_drawdown"), b.get("max_drawdown")),
                "rank_win": 1 if ((_delta(r.get("total_return"), b.get("total_return")) or 0.0) > 0 and (_delta(r.get("max_drawdown"), b.get("max_drawdown")) or 0.0) > 0) else 0,
            }
        )

    wf_delta_df = pd.DataFrame(wf_delta_rows)
    regime_delta_df = pd.DataFrame(regime_delta_rows)
    wf_delta_ret_med = float(wf_delta_df["delta_ret"].dropna().median()) if (not wf_delta_df.empty and "delta_ret" in wf_delta_df.columns) else 0.0
    wf_delta_mdd_med = float(wf_delta_df["delta_mdd"].dropna().median()) if (not wf_delta_df.empty and "delta_mdd" in wf_delta_df.columns) else 0.0
    regime_wins = int(regime_delta_df["rank_win"].sum()) if (not regime_delta_df.empty and "rank_win" in regime_delta_df.columns) else 0
    regime_total = int(len(regime_delta_df))
    regime_score = (float(regime_wins) / float(regime_total)) if regime_total > 0 else 0.0

    # Runner matrix summary rows only
    rows: list[dict[str, Any]] = []
    for gid, src in [("ALL_BASE", all_base), ("ALL_RANK", all_rank)]:
        rows.append(
            {
                "group_id": gid,
                "status": "FAILED" if universe_too_small else "OK",
                "error_message": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
                "error": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
                "run_id": run_id,
                "strategy_name": "sma_cross_regime_atr",
                "strategy_name_full": "sma_cross_regime_atr",
                "params_json": "{}",
                "params_json_full": "{}",
                "total_return": _safe_num(src.get("total_return")),
                "cagr": _safe_num(src.get("cagr")),
                "max_drawdown": _safe_num(src.get("max_drawdown")),
                "sharpe": _safe_num(src.get("sharpe")),
                "vol_annual": _safe_num(src.get("vol_annual")),
                "max_daily_drawdown": _safe_num(src.get("max_daily_drawdown")),
                "trades_count": int(_safe_num(src.get("trades")) or 0),
                "trades": int(_safe_num(src.get("trades")) or 0),
                "avg_positions": _safe_num(src.get("avg_positions")),
                "turnover_avg": _safe_num(src.get("turnover_avg")),
                "universe_filtered_count": filtered_count,
                "not_in_topk_reject_count": int(_safe_num(src.get("not_in_topk_reject_count")) or 0),
                "avg_candidates_per_day": _safe_num(src.get("avg_candidates_per_day")),
                "avg_buy_slots_per_day": _safe_num(src.get("avg_buy_slots_per_day")),
                "trigger_when_slots_small_rate": _safe_num(src.get("trigger_when_slots_small_rate")),
                "rank_trigger_days": int(_safe_num(src.get("rank_trigger_days")) or 0),
                "rank_trigger_rate": _safe_num(src.get("rank_trigger_rate")),
                "max_positions": max_positions,
                "warning_notes": "UNIVERSE_TOO_SMALL" if universe_too_small else "",
            }
        )
    rows.append(
        {
            "group_id": "WF_TEST_MEDIAN_DELTA",
            "status": "FAILED" if universe_too_small else "OK",
            "error_message": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
            "error": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
            "run_id": run_id,
            "total_return": wf_delta_ret_med,
            "max_drawdown": wf_delta_mdd_med,
            "note": "delta fields: total_return=median_delta_ret, max_drawdown=median_delta_mdd",
            "warning_notes": "UNIVERSE_TOO_SMALL" if universe_too_small else "",
        }
    )
    rows.append(
        {
            "group_id": "REGIME_SCORE",
            "status": "FAILED" if universe_too_small else "OK",
            "error_message": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
            "error": "UNIVERSE_TOO_SMALL" if universe_too_small else None,
            "run_id": run_id,
            "score": regime_score,
            "pass_rate": regime_score,
            "note": f"rank_win={regime_wins}/{regime_total}",
            "warning_notes": "UNIVERSE_TOO_SMALL" if universe_too_small else "",
        }
    )

    _emit_progress(progress_hook, 90, "写出 v2 多阶段验证结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_path = write_report_to_local(
        report_filename,
        _build_strength_rank_validate_v2_report(
            rows=rows,
            details_df=details_df,
            regimes=regimes,
            wf_folds=wf_folds,
            full_start=full_start,
            full_end=full_end,
            universe_limit=universe_limit,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_too_small=universe_too_small,
            fast_mode=fast_mode,
            warning_label=warning_label,
            skipped_wf=skipped_wf,
            perf=perf,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "v2 多阶段实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_full_range_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = FULL_RANGE_VALIDATE_REPORT_FILE,
) -> dict[str, Any]:
    ctx = RunContext(
        preset=str(preset),
        base_config_path=str(base_path),
        overrides=dict(overrides or {}),
    )
    variants = preset_variants("FULL_RANGE_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2021-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    fast_mode = bool(ov.get("fast_mode", True))
    write_trades_artifacts = bool(ov.get("write_trades_artifacts", False))
    write_trades_artifacts = bool(ov.get("write_trades_artifacts", False))
    enable_mf_fast_gate = bool(ov.get("enable_multifactor_fast_gate", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_full_range_validate_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        _emit_stage_progress(progress_hook, ctx, stage="universe_resolve", pct=8, message="阶段1/5：解析Universe成分")
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
        _close_stage(ctx, "universe_resolve")
    except UniverseLoadError as exc:
        _close_stage(ctx, "universe_resolve")
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_full_range_validate_report(
                rows=rows,
                runs=[],
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                raw_count=0,
                filtered_count=0,
                excluded={},
                initial_capital=initial_capital,
                max_positions=max_positions,
                universe_limit=universe_limit,
                perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        ctx.add_note(err)
        ctx.write_profile(
            results_dir=str(exp_dir),
            extras={
                "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
                "error": err,
                "variant_count": int(len(variants)),
                "row_count": int(len(rows)),
                "symbols_count": int(len(filtered_symbols) if 'filtered_symbols' in locals() else 0),
                "trading_days": 0,
            },
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_stage_progress(progress_hook, ctx, stage="data_load", pct=12, message="阶段2/5：加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)
    _close_stage(ctx, "data_load")

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_full_range_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    _emit_stage_progress(progress_hook, ctx, stage="simulation", pct=20, message="阶段3/5：执行组合仿真")
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant.get("group_id", ""))
        _emit_progress(progress_hook, 15 + int((idx - 1) * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        # 关键修复：使用deepcopy避免参数字典引用共享
        strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
        portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=copy.deepcopy(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})

        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        if write_trades_artifacts or (not fast_mode):
            result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        if not fast_mode:
            result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": bool(portfolio_def.get("enable_strength_rank", False)),
                    "strength_ma": int(portfolio_def.get("strength_ma", 120)),
                    "engine_mode": str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "rank_trigger_rate": float(m.get("rank_trigger_rate", 0.0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": max_positions,
            "not_in_topk_reject_count": int(m.get("not_in_topk_reject_count", 0)),
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                    "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 15 + int(idx * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")
    _close_stage(ctx, "simulation")

    _emit_stage_progress(progress_hook, ctx, stage="output_write", pct=90, message="阶段4/5：写出实验产物")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_path = write_report_to_local(
        report_filename,
        _build_full_range_validate_report(
            rows=rows,
            runs=runs,
            start_s=start_s,
            end_s=end_s,
            benchmark=benchmark,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_limit=universe_limit,
            perf=perf_report,
            error=None,
        ),
    )
    _close_stage(ctx, "output_write")
    _emit_stage_progress(progress_hook, ctx, stage="report_write", pct=94, message="阶段5/5：生成人类可读报告")
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _close_stage(ctx, "report_write")
    ctx.write_profile(
        results_dir=str(exp_dir),
        extras={
            "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
            "perf": {
                "db_load_seconds": float(perf_report.get("db_load_seconds", 0.0) or 0.0),
                "db_queries": int(perf_report.get("db_queries", 0) or 0),
                "cache_hit_rate": float(perf_report.get("cache_hit_rate", 0.0) or 0.0),
            },
            "variant_count": int(total_variants),
            "row_count": int(len(rows)),
            "symbols_count": int(len(filtered_symbols)),
            "trading_days": int(len(day_list)),
        },
    )
    _emit_progress(progress_hook, 96, "全区间验证打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows[: min(4, len(rows))],
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_candidate_diag_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = CANDIDATE_DIAG_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("CANDIDATE_DIAG_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2013-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 3000))
    fast_mode = bool(ov.get("fast_mode", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_candidate_diag_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [{"group_id": "BASE_ATR", "status": "FAILED", "error_message": err, "error": err, "warning_notes": str(exc)}]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_candidate_diag_report(
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                universe_limit=universe_limit,
                raw_count=0,
                filtered_count=0,
                excluded={},
                initial_capital=initial_capital,
                max_positions=max_positions,
                daily_df=pd.DataFrame(),
                reason_counts={},
                perf={"db_load_seconds": 0.0, "precompute_seconds": 0.0, "simulation_seconds": 0.0, "io_write_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_progress(progress_hook, 15, "加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    run_id = f"run_candidate_diag_{exp_id[:10]}"
    run_dir = _results_root() / run_id / "BASE_ATR"
    run_dir.mkdir(parents=True, exist_ok=True)
    variant = variants[0] if variants else {}
    strategy_def = dict(variant.get("strategy", {}) or {})
    portfolio_def = dict(variant.get("portfolio", {}) or {})
    strategy_cfg = StrategyConfig(
        name=str(strategy_def.get("name", "sma_cross_regime_atr")),
        params=dict(strategy_def.get("params", {}) or {}),
    )
    strategy = build_strategy(strategy_cfg)
    strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
    _emit_progress(progress_hook, 40, "执行诊断回测")
    result = run_portfolio_backtest(
        strategy=strategy,
        history=hist,
        benchmark=benchmark_df,
        trading_days=day_list,
        benchmark_symbol=benchmark,
        initial_cash=initial_capital,
        max_positions=max_positions,
        lot_size=lot_size,
        enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
        strength_ma=int(portfolio_def.get("strength_ma", 120)),
        strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
        engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
        fast_mode=fast_mode,
    )
    result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
    result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
    if not fast_mode:
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

    m = dict(result.metrics)
    reason_counts = dict(m.get("candidate_diag_reason_counts", {}) or {})
    row = {
        "group_id": "BASE_ATR",
        "status": "OK",
        "run_id": run_id,
        "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
        "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
        "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
        "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
        "total_return": float(m.get("total_return", 0.0)),
        "cagr": float(m.get("cagr", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "sharpe": float(m.get("sharpe", 0.0)),
        "vol_annual": float(m.get("vol_annual", 0.0)),
        "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
        "trades_count": int(m.get("trades", 0)),
        "avg_positions": float(m.get("avg_positions", 0.0)),
        "turnover_avg": float(m.get("turnover_avg", 0.0)),
        "rank_trigger_rate": float(m.get("rank_trigger_rate", 0.0)),
        "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
        "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
        "universe_filtered_count": filtered_count,
        "max_positions": max_positions,
        "warning_notes": "",
    }
    rows.append(row)
    runs.append(
        {
            "group_id": "BASE_ATR",
            "run_id": run_id,
            "status": "OK",
            "error_message": None,
            "artifacts": {
                "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
            },
        }
    )

    _emit_progress(progress_hook, 90, "写出诊断结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
        "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
        "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_path = write_report_to_local(
        report_filename,
        _build_candidate_diag_report(
            start_s=start_s,
            end_s=end_s,
            benchmark=benchmark,
            universe_limit=universe_limit,
            raw_count=raw_count,
            filtered_count=filtered_count,
            excluded=excluded,
            initial_capital=initial_capital,
            max_positions=max_positions,
            daily_df=result.daily,
            reason_counts=reason_counts,
            perf=perf_report,
            error=None,
        ),
    )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "诊断实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _run_slot_debug_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = SLOT_DEBUG_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("SLOT_DEBUG_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2013-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    fast_mode = bool(ov.get("fast_mode", True))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_slot_debug_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {"db_load_seconds": 0.0, "db_queries": 0, "cache_total": 0, "cache_hit": 0}

    _emit_progress(progress_hook, 15, "加载行情与Universe过滤")
    t0 = time.perf_counter()
    raw_symbols, universe_meta = resolve_universe_symbols(
        as_of=cfg_obj.universe.end,
        db_loader=lambda as_of, lim: load_constituents_from_db(
            db=cfg_obj.db,
            runtime=cfg_obj.db_runtime,
            as_of=as_of,
            universe_limit=lim,
        ),
        user_symbols=seed_symbols,
        allow_user_override=False,
        universe_limit=universe_limit,
    )
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    run_id = f"run_slot_debug_{exp_id[:10]}"
    run_dir = _results_root() / run_id / "BASE_ATR"
    run_dir.mkdir(parents=True, exist_ok=True)
    variant = variants[0] if variants else {}
    strategy_def = dict(variant.get("strategy", {}) or {})
    portfolio_def = dict(variant.get("portfolio", {}) or {})
    strategy_cfg = StrategyConfig(
        name=str(strategy_def.get("name", "sma_cross_regime_atr")),
        params=dict(strategy_def.get("params", {}) or {}),
    )
    strategy = build_strategy(strategy_cfg)
    strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})

    _emit_progress(progress_hook, 40, "执行空位调试回测")
    result = run_portfolio_backtest(
        strategy=strategy,
        history=hist,
        benchmark=benchmark_df,
        trading_days=day_list,
        benchmark_symbol=benchmark,
        initial_cash=initial_capital,
        max_positions=max_positions,
        lot_size=lot_size,
        enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
        strength_ma=int(portfolio_def.get("strength_ma", 120)),
        strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
        engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
        fast_mode=fast_mode,
    )
    result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
    result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)

    m = dict(result.metrics)
    row = {
        "group_id": "BASE_ATR",
        "status": "OK",
        "run_id": run_id,
        "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
        "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
        "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
        "params_json_full": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
        "total_return": float(m.get("total_return", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "trades_count": int(m.get("trades", 0)),
        "avg_positions": float(m.get("avg_positions", 0.0)),
        "turnover_avg": float(m.get("turnover_avg", 0.0)),
        "universe_filtered_count": filtered_count,
        "max_positions": max_positions,
        "warning_notes": "",
    }
    rows.append(row)
    runs.append(
        {
            "group_id": "BASE_ATR",
            "run_id": run_id,
            "status": "OK",
            "error_message": None,
            "artifacts": {
                "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
            },
        }
    )

    _emit_progress(progress_hook, 90, "写出空位调试结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
        "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
        "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_md = _build_slot_debug_report(
        start_s=start_s,
        end_s=end_s,
        benchmark=benchmark,
        universe_limit=universe_limit,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        daily_df=result.daily,
        perf=perf_report,
    )
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    _emit_progress(progress_hook, 96, "空位调试实验打包完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


def _build_slot_debug_report(
    *,
    start_s: str,
    end_s: str,
    benchmark: str,
    universe_limit: int,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    daily_df: pd.DataFrame,
    perf: dict[str, Any],
) -> str:
    def _fmt_num(v: Any, nd: int = 2) -> str:
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    def _fmt_pct(v: Any) -> str:
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    work = daily_df.copy() if daily_df is not None else pd.DataFrame()
    for col, fallback in [
        ("positions_before_sell", "positions_count"),
        ("sells_executed_count", None),
        ("positions_after_sell", "positions_count"),
        ("buy_slots_calculated", "buy_slots_after_sell"),
        ("candidates_after_held_filter_count", "after_held_filter_count"),
        ("sorting_triggered", None),
    ]:
        if col not in work.columns:
            if fallback and fallback in work.columns:
                work[col] = work[fallback]
            else:
                work[col] = 0

    if "date" not in work.columns:
        work["date"] = ""

    work["positions_after_sell"] = pd.to_numeric(work["positions_after_sell"], errors="coerce").fillna(0).astype(int)
    work["buy_slots_calculated"] = pd.to_numeric(work["buy_slots_calculated"], errors="coerce").fillna(0).astype(int)
    work["positions_before_sell"] = pd.to_numeric(work["positions_before_sell"], errors="coerce").fillna(0).astype(int)
    work["sells_executed_count"] = pd.to_numeric(work["sells_executed_count"], errors="coerce").fillna(0).astype(int)
    work["candidates_after_held_filter_count"] = pd.to_numeric(work["candidates_after_held_filter_count"], errors="coerce").fillna(0).astype(int)
    work["sorting_triggered"] = work["sorting_triggered"].astype(bool)
    work["abnormal"] = (work["buy_slots_calculated"] == 0) & (work["positions_after_sell"] < int(max_positions))

    abnormal_df = work[work["abnormal"]].copy()
    if not abnormal_df.empty:
        sample = abnormal_df.head(100)
    else:
        sample = work.head(30).copy()

    lines: list[str] = [
        "# SLOT_DEBUG_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        f"- benchmark: {benchmark}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "",
        "## 2) 每日调试样本",
        "",
        "date | pos_before | sells | pos_after | max_pos | buy_slots | candidates | sorting_triggered",
        "---|---:|---:|---:|---:|---:|---:|---",
    ]
    for r in sample.itertuples(index=False):
        lines.append(
            f"{str(r.date)} | {int(getattr(r, 'positions_before_sell', 0))} | {int(getattr(r, 'sells_executed_count', 0))} | "
            f"{int(getattr(r, 'positions_after_sell', 0))} | {int(max_positions)} | {int(getattr(r, 'buy_slots_calculated', 0))} | "
            f"{int(getattr(r, 'candidates_after_held_filter_count', 0))} | {bool(getattr(r, 'sorting_triggered', False))}"
        )
    lines.append("")

    total_days = int(len(work))
    abnormal_days = int(len(abnormal_df))
    normal_days = int(max(total_days - abnormal_days, 0))
    buy_slots_zero_ratio = float((work["buy_slots_calculated"] == 0).mean()) if total_days > 0 else 0.0
    lines.extend(
        [
            "## 3) 汇总统计",
            f"- 异常天数（pos_after < max_pos 且 buy_slots==0）: {abnormal_days}",
            f"- 正常天数: {normal_days}",
            f"- buy_slots==0 占比: {_fmt_pct(buy_slots_zero_ratio)}",
            "",
            "## 4) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(perf.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(perf.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(perf.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "",
        ]
    )
    return "\n".join(lines)


def _run_max_position_expand_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = MAX_POSITION_EXPAND_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("MAX_POSITION_EXPAND_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2021-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    fast_mode = bool(ov.get("fast_mode", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_max_position_expand_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_max_position_expand_report(
                rows=rows,
                runs=[],
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                raw_count=0,
                filtered_count=0,
                excluded={},
                initial_capital=initial_capital,
                universe_limit=universe_limit,
                base_order_value=base_order_value,
                lot_mode=lot_mode,
                perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_progress(progress_hook, 12, "加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_max_position_expand_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant.get("group_id", ""))
        _emit_progress(progress_hook, 15 + int((idx - 1) * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        strategy_def = dict(variant.get("strategy", {}) or {})
        portfolio_def = dict(variant.get("portfolio", {}) or {})
        group_max_positions = int(portfolio_def.get("max_positions", 10))
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=dict(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})

        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=group_max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        if write_trades_artifacts or (not fast_mode):
            result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        if not fast_mode:
            result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        cash_utilization_avg = 0.0
        if not result.daily.empty and {"cash", "nav"}.issubset(set(result.daily.columns)):
            nav_s = pd.to_numeric(result.daily["nav"], errors="coerce").replace(0, pd.NA)
            cash_s = pd.to_numeric(result.daily["cash"], errors="coerce")
            util = (1.0 - (cash_s / nav_s)).fillna(0.0)
            cash_utilization_avg = float(util.mean()) if not util.empty else 0.0

        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": bool(portfolio_def.get("enable_strength_rank", False)),
                    "strength_ma": int(portfolio_def.get("strength_ma", 120)),
                    "engine_mode": str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                    "max_positions": int(group_max_positions),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "universe_filtered_count": filtered_count,
            "max_positions": int(group_max_positions),
            "cash_utilization_avg": float(cash_utilization_avg),
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv") if (write_trades_artifacts or (not fast_mode)) else "",
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                    "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 15 + int(idx * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出max_positions扩容验证结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_md = _build_max_position_expand_report(
        rows=rows,
        runs=runs,
        start_s=start_s,
        end_s=end_s,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        universe_limit=universe_limit,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf_report,
        error=None,
    )
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    _emit_progress(progress_hook, 96, "max_positions扩容验证完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


def _run_strong_only_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = STRONG_ONLY_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("STRONG_ONLY_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2021-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    max_positions = int(ov.get("max_positions", 10))
    fast_mode = bool(ov.get("fast_mode", True))
    enable_mf_fast_gate = bool(ov.get("enable_multifactor_fast_gate", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_strong_only_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_strong_only_validate_report(
                rows=rows,
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                raw_count=0,
                filtered_count=0,
                excluded={},
                initial_capital=initial_capital,
                max_positions=max_positions,
                universe_limit=universe_limit,
                base_order_value=base_order_value,
                lot_mode=lot_mode,
                perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_progress(progress_hook, 12, "加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_strong_only_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant.get("group_id", ""))
        _emit_progress(progress_hook, 15 + int((idx - 1) * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        # 关键修复：使用deepcopy避免参数字典引用共享
        strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
        portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=copy.deepcopy(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})

        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
            fast_mode=fast_mode,
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        if not fast_mode:
            result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        weak_blocked = int(m.get("BLOCK_WEAK_TIER_DISABLED", 0) or 0)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": bool(portfolio_def.get("enable_strength_rank", False)),
                    "strength_ma": int(portfolio_def.get("strength_ma", 120)),
                    "engine_mode": str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                    "max_positions": int(max_positions),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "STRONG_TREND_BUY_RATE": float(m.get("STRONG_TREND_BUY_RATE", 0.0)),
            "BLOCK_WEAK_TIER_DISABLED": weak_blocked,
            "universe_filtered_count": filtered_count,
            "max_positions": int(max_positions),
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                    "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 15 + int(idx * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出强趋势集中验证结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_md = _build_strong_only_validate_report(
        rows=rows,
        start_s=start_s,
        end_s=end_s,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf_report,
        error=None,
    )
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    _emit_progress(progress_hook, 96, "强趋势集中验证完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


def _run_multifactor_ranking_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = MULTIFACTOR_RANKING_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("MULTIFACTOR_RANKING_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2023-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 300))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    max_positions = int(ov.get("max_positions", 10))
    fast_mode = bool(ov.get("fast_mode", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_mf_rank_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    except UniverseLoadError as exc:
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_md = _build_multifactor_ranking_validate_report(
            rows=rows,
            start_s=start_s,
            end_s=end_s,
            benchmark=benchmark,
            raw_count=0,
            filtered_count=0,
            excluded={},
            initial_capital=initial_capital,
            max_positions=max_positions,
            universe_limit=universe_limit,
            base_order_value=base_order_value,
            lot_mode=lot_mode,
            perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
            error=err,
        )
        report_path = write_report_to_local(report_filename, report_md)
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_progress(progress_hook, 12, "加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_mf_ranking_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant.get("group_id", ""))
        _emit_progress(progress_hook, 15 + int((idx - 1) * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
        portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=copy.deepcopy(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})

        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
            fast_mode=fast_mode,
            enable_multifactor_rank=bool(portfolio_def.get("enable_multifactor_rank", False)),
            mf_topk=int(portfolio_def.get("mf_topk", 10)),
            mf_concentrate_topn=int(portfolio_def.get("mf_concentrate_topn", 3)),
            mf_concentrate_scale=float(portfolio_def.get("mf_concentrate_scale", 1.5)),
        )
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        if not fast_mode:
            result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": bool(portfolio_def.get("enable_strength_rank", False)),
                    "enable_multifactor_rank": bool(portfolio_def.get("enable_multifactor_rank", False)),
                    "mf_topk": int(portfolio_def.get("mf_topk", 10)),
                    "mf_concentrate_topn": int(portfolio_def.get("mf_concentrate_topn", 3)),
                    "mf_concentrate_scale": float(portfolio_def.get("mf_concentrate_scale", 1.5)),
                    "engine_mode": str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                    "max_positions": int(max_positions),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "STRONG_TREND_BUY_RATE": float(m.get("STRONG_TREND_BUY_RATE", 0.0)),
            "mf_rank_trigger_rate": float(m.get("mf_rank_trigger_rate", 0.0)),
            "mf_avg_candidates_when_triggered": float(m.get("mf_avg_candidates_when_triggered", 0.0)),
            "mf_topk_reject_count": int(m.get("mf_topk_reject_count", 0)),
            "mf_concentrated_buy_rate": float(m.get("mf_concentrated_buy_rate", 0.0)),
            "top10_profit_concentration_pct": float(m.get("top10_profit_concentration_pct", 0.0)) if m.get("top10_profit_concentration_pct") is not None else None,
            "universe_filtered_count": filtered_count,
            "max_positions": int(max_positions),
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                    "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 15 + int(idx * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")

    _emit_progress(progress_hook, 90, "写出多因子排名验证结果")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_md = _build_multifactor_ranking_validate_report(
        rows=rows,
        start_s=start_s,
        end_s=end_s,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf_report,
        error=None,
    )
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    _emit_progress(progress_hook, 96, "多因子排名验证完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


def _run_multifactor_ranking_walkforward_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = MULTIFACTOR_RANKING_WF_REPORT_FILE,
) -> dict[str, Any]:
    ctx = RunContext(
        preset=str(preset),
        base_config_path=str(base_path),
        overrides=dict(overrides or {}),
    )
    variants = preset_variants("MULTIFACTOR_RANKING_WALKFORWARD_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    full_start = str(ov.get("start_date") or "2021-01-01")
    full_end = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    fast_mode = bool(ov.get("fast_mode", True))
    write_trades_artifacts = bool(ov.get("write_trades_artifacts", False))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = full_start
    bt_cfg["universe"]["end"] = full_end
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols
    tmp_cfg = exp_dir / "_mf_rank_wf_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "precompute_seconds": 0.0,
        "simulation_seconds": 0.0,
        "io_write_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
        "total_wall": 0.0,
    }
    wall_t0 = time.perf_counter()

    try:
        _emit_stage_progress(progress_hook, ctx, stage="universe_resolve", pct=8, message="阶段1/5：解析Universe成分")
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
        _close_stage(ctx, "universe_resolve")
    except UniverseLoadError as exc:
        _close_stage(ctx, "universe_resolve")
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [{"group_id": "ALL_BASE", "status": "FAILED", "error": err}, {"group_id": "ALL_MF_TOPK10", "status": "FAILED", "error": err}]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_md = _build_multifactor_ranking_walkforward_report(
            rows=rows,
            details_df=pd.DataFrame(),
            full_start=full_start,
            full_end=full_end,
            universe_limit=universe_limit,
            benchmark=benchmark,
            raw_count=0,
            filtered_count=0,
            excluded={},
            initial_capital=initial_capital,
            max_positions=max_positions,
            base_order_value=base_order_value,
            lot_mode=lot_mode,
            perf=perf,
            error=err,
        )
        report_path = write_report_to_local(report_filename, report_md)
        ctx.add_note(err)
        ctx.write_profile(
            results_dir=str(exp_dir),
            extras={
                "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
                "error": err,
                "variant_count": int(len(variants)),
                "row_count": int(len(rows)),
                "symbols_count": 0,
                "trading_days": 0,
            },
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_stage_progress(progress_hook, ctx, stage="data_load", pct=12, message="阶段2/5：加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(provider, data_cache, raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, perf)
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df_all = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    full_calendar_days = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)
    _close_stage(ctx, "data_load")

    hist_all = ohlcv_all.copy()
    hist_all["date"] = pd.to_datetime(hist_all["date"])
    hist_all["symbol"] = hist_all["symbol"].astype(str).str.lower()
    if filtered_symbols:
        hist_all = hist_all[hist_all["symbol"].isin(set(filtered_symbols))].copy()
    benchmark_df_all = benchmark_df_all.copy()
    benchmark_df_all["date"] = pd.to_datetime(benchmark_df_all["date"])

    # P1/P2/P3 with matching folds test
    slices = [
        {"name": "ALL", "segment": "ALL", "start": full_start, "end": full_end, "train_start": "", "train_end": ""},
        {"name": "P1", "segment": "REGIME", "start": "2021-01-01", "end": "2022-12-31", "train_start": "", "train_end": ""},
        {"name": "P2", "segment": "REGIME", "start": "2023-01-01", "end": "2024-12-31", "train_start": "", "train_end": ""},
        {"name": "P3", "segment": "REGIME", "start": "2025-01-01", "end": "2026-02-01", "train_start": "", "train_end": ""},
        {"name": "Fold1", "segment": "WF_TEST", "start": "2021-01-01", "end": "2022-12-31", "train_start": "N/A", "train_end": "N/A"},
        {"name": "Fold2", "segment": "WF_TEST", "start": "2023-01-01", "end": "2024-12-31", "train_start": "2021-01-01", "train_end": "2022-12-31"},
        {"name": "Fold3", "segment": "WF_TEST", "start": "2025-01-01", "end": "2026-02-01", "train_start": "2021-01-01", "train_end": "2024-12-31"},
    ]
    # clip to full range
    fs = date.fromisoformat(full_start)
    fe = date.fromisoformat(full_end)
    valid_slices: list[dict[str, str]] = []
    for sl in slices:
        ss = date.fromisoformat(str(sl["start"]))
        se = date.fromisoformat(str(sl["end"]))
        if se < fs or ss > fe:
            continue
        valid_slices.append(sl)

    _emit_stage_progress(progress_hook, ctx, stage="precompute", pct=16, message="阶段3/5：切片预计算")
    t1 = time.perf_counter()
    sliced_data: dict[str, tuple[pd.DataFrame, pd.DataFrame, list[date]]] = {}
    for sl in valid_slices:
        s_name = str(sl["name"])
        s_start = pd.Timestamp(str(sl["start"]))
        s_end = pd.Timestamp(str(sl["end"]))
        h = hist_all[(hist_all["date"] >= s_start) & (hist_all["date"] <= s_end)].copy()
        b = benchmark_df_all[(benchmark_df_all["date"] >= s_start) & (benchmark_df_all["date"] <= s_end)].copy()
        days = [d for d in full_calendar_days if d >= s_start.date() and d <= s_end.date()]
        sliced_data[s_name] = (h, b, days)
    perf["precompute_seconds"] = float(time.perf_counter() - t1)
    _close_stage(ctx, "precompute")

    run_id = f"run_mf_rank_wf_{exp_id[:10]}"
    runs: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    total_jobs = max(len(valid_slices) * len(variants), 1)
    job_idx = 0
    _emit_stage_progress(progress_hook, ctx, stage="simulation", pct=22, message="阶段4/5：执行组合仿真")
    t2 = time.perf_counter()
    for sl in valid_slices:
        s_name = str(sl["name"])
        h, b, days = sliced_data[s_name]
        for variant in variants:
            job_idx += 1
            group_id = str(variant.get("group_id", ""))
            _emit_progress(progress_hook, 15 + int(job_idx * 70 / total_jobs), f"[{job_idx}/{total_jobs}] {s_name}-{group_id}")
            strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
            portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
            run_dir = _results_root() / run_id / s_name / group_id
            run_dir.mkdir(parents=True, exist_ok=True)
            strategy_cfg = StrategyConfig(
                name=str(strategy_def.get("name", "sma_cross_regime_atr")),
                params=copy.deepcopy(strategy_def.get("params", {}) or {}),
            )
            strategy = build_strategy(strategy_cfg)
            strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
            result = run_portfolio_backtest(
                strategy=strategy,
                history=h,
                benchmark=b,
                trading_days=days,
                benchmark_symbol=benchmark,
                initial_cash=initial_capital,
                max_positions=max_positions,
                lot_size=lot_size,
                enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
                strength_ma=int(portfolio_def.get("strength_ma", 120)),
                strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
                engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                fast_mode=fast_mode,
                enable_multifactor_rank=bool(portfolio_def.get("enable_multifactor_rank", False)),
                mf_topk=int(portfolio_def.get("mf_topk", 10)),
                mf_concentrate_topn=int(portfolio_def.get("mf_concentrate_topn", 3)),
                mf_concentrate_scale=float(portfolio_def.get("mf_concentrate_scale", 1.5)),
            )
            if s_name == "ALL":
                result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
                if write_trades_artifacts or (not fast_mode):
                    result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
            m = dict(result.metrics)
            detail_rows.append(
                {
                    "slice_name": s_name,
                    "segment": str(sl.get("segment", "")),
                    "slice_start": str(sl["start"]),
                    "slice_end": str(sl["end"]),
                    "train_start": str(sl.get("train_start", "")),
                    "train_end": str(sl.get("train_end", "")),
                    "group_id": group_id,
                    "run_id": run_id,
                    "total_return": float(m.get("total_return", 0.0)),
                    "cagr": float(m.get("cagr", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "vol_annual": float(m.get("vol_annual", 0.0)),
                    "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
                    "trades": int(m.get("trades", 0)),
                    "avg_positions": float(m.get("avg_positions", 0.0)),
                    "turnover_avg": float(m.get("turnover_avg", 0.0)),
                    "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
                    "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
                    "slots_le_1_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
                    "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
                    "mf_rank_trigger_rate": float(m.get("mf_rank_trigger_rate", 0.0)),
                    "mf_avg_candidates_when_triggered": float(m.get("mf_avg_candidates_when_triggered", 0.0)),
                    "mf_topk_reject_count": int(m.get("mf_topk_reject_count", 0)),
                    "mf_concentrated_buy_rate": float(m.get("mf_concentrated_buy_rate", 0.0)),
                    "top10_profit_concentration_pct": _safe_num(m.get("top10_profit_concentration_pct")),
                }
            )
            runs.append({"group_id": f"{s_name}:{group_id}", "run_id": run_id, "status": "OK", "error_message": None})
    perf["simulation_seconds"] = float(time.perf_counter() - t2)
    _close_stage(ctx, "simulation")
    _emit_stage_progress(progress_hook, ctx, stage="output_write", pct=90, message="阶段5/5：写出产物与报告")
    io_t0 = time.perf_counter()
    details_df = pd.DataFrame(detail_rows)
    details_df.to_csv(exp_dir / "multifactor_ranking_walkforward_details.csv", index=False)
    perf["io_write_seconds"] = float(time.perf_counter() - io_t0)
    perf["total_wall"] = float(time.perf_counter() - wall_t0)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf["cache_hit_rate"] = (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_base = _pick("ALL", "BASE_SMA_ONLY")
    all_mf = _pick("ALL", "MF_RANK_TOPK10")
    wf_rows = []
    for fd in ["Fold1", "Fold2", "Fold3"]:
        b = _pick(fd, "BASE_SMA_ONLY")
        m = _pick(fd, "MF_RANK_TOPK10")
        wf_rows.append({"fold": fd, "delta_ret": _delta(m.get("total_return"), b.get("total_return")), "delta_mdd": _delta(m.get("max_drawdown"), b.get("max_drawdown"))})
    wf_df = pd.DataFrame(wf_rows)
    wf_delta_ret_med = float(wf_df["delta_ret"].dropna().median()) if not wf_df.empty else 0.0
    wf_delta_mdd_med = float(wf_df["delta_mdd"].dropna().median()) if not wf_df.empty else 0.0
    regime_rows = []
    for rg in ["P1", "P2", "P3"]:
        b = _pick(rg, "BASE_SMA_ONLY")
        m = _pick(rg, "MF_RANK_TOPK10")
        dret = _delta(m.get("total_return"), b.get("total_return")) or 0.0
        dmdd = _delta(m.get("max_drawdown"), b.get("max_drawdown")) or 0.0
        regime_rows.append({"regime": rg, "delta_ret": dret, "delta_mdd": dmdd, "win": 1 if (dret > 0 and dmdd > 0) else 0})
    regime_df = pd.DataFrame(regime_rows)
    regime_wins = int(regime_df["win"].sum()) if not regime_df.empty else 0
    regime_total = int(len(regime_df))
    wf_wins = int(sum(1 for r in wf_rows if (r.get("delta_ret") or 0.0) > 0 and (r.get("delta_mdd") or 0.0) > 0))

    rows = [
        {"group_id": "ALL_BASE", "status": "OK", "run_id": run_id, "total_return": _safe_num(all_base.get("total_return")), "max_drawdown": _safe_num(all_base.get("max_drawdown")), "trades": int(_safe_num(all_base.get("trades")) or 0), "avg_positions": _safe_num(all_base.get("avg_positions")), "mf_rank_trigger_rate": _safe_num(all_base.get("mf_rank_trigger_rate"))},
        {"group_id": "ALL_MF_TOPK10", "status": "OK", "run_id": run_id, "total_return": _safe_num(all_mf.get("total_return")), "max_drawdown": _safe_num(all_mf.get("max_drawdown")), "trades": int(_safe_num(all_mf.get("trades")) or 0), "avg_positions": _safe_num(all_mf.get("avg_positions")), "mf_rank_trigger_rate": _safe_num(all_mf.get("mf_rank_trigger_rate"))},
        {"group_id": "WF_TEST_MEDIAN_DELTA", "status": "OK", "run_id": run_id, "total_return": wf_delta_ret_med, "max_drawdown": wf_delta_mdd_med},
        {"group_id": "REGIME_SCORE", "status": "OK", "run_id": run_id, "score": (float(regime_wins) / float(regime_total)) if regime_total > 0 else 0.0, "note": f"regime_wins={regime_wins}/{regime_total}, wf_wins={wf_wins}/3"},
    ]

    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    report_md = _build_multifactor_ranking_walkforward_report(
        rows=rows,
        details_df=details_df,
        full_start=full_start,
        full_end=full_end,
        universe_limit=universe_limit,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf,
        error=None,
    )
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    _close_stage(ctx, "output_write")
    ctx.write_profile(
        results_dir=str(exp_dir),
        extras={
            "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
            "perf": {
                "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
                "precompute_seconds": float(perf.get("precompute_seconds", 0.0) or 0.0),
                "simulation_seconds": float(perf.get("simulation_seconds", 0.0) or 0.0),
                "io_write_seconds": float(perf.get("io_write_seconds", 0.0) or 0.0),
                "db_queries": int(perf.get("db_queries", 0) or 0),
                "cache_hit_rate": float(perf.get("cache_hit_rate", 0.0) or 0.0),
                "total_wall": float(perf.get("total_wall", 0.0) or 0.0),
            },
            "slice_count": int(len(valid_slices)),
            "variant_count": int(len(variants)),
            "row_count": int(len(rows)),
            "detail_row_count": int(len(detail_rows)),
            "symbols_count": int(len(filtered_symbols)),
            "trading_days": int(len(full_calendar_days)),
        },
    )
    _emit_progress(progress_hook, 96, "多因子排名WF验证完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


def _build_multifactor_ranking_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    groups = ["BASE_SMA_ONLY", "MF_RANK_TOPK5", "MF_RANK_TOPK10"]

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    lines: list[str] = [
        "# MULTIFACTOR_RANKING_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- benchmark: {benchmark}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 核心对照表",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | mf_rank_trigger_rate | mf_avg_candidates_when_triggered | mf_topk_reject_count | mf_concentrated_buy_rate | top10_profit_concentration_pct |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in groups:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | {_fmt_pct(r.get('trigger_when_slots_small_rate'))} | "
            f"{_fmt_num(r.get('mean_order_value_scale'), 3)} | {_fmt_pct(r.get('mf_rank_trigger_rate'))} | {_fmt_num(r.get('mf_avg_candidates_when_triggered'), 2)} | "
            f"{int(r.get('mf_topk_reject_count', 0) or 0)} | {_fmt_pct(r.get('mf_concentrated_buy_rate'))} | {_fmt_pct(r.get('top10_profit_concentration_pct'))} |"
        )
    lines.append("")

    base = by_gid.get("BASE_SMA_ONLY", {})
    g5 = by_gid.get("MF_RANK_TOPK5", {})
    g10 = by_gid.get("MF_RANK_TOPK10", {})
    best_gid = "MF_RANK_TOPK5" if float(g5.get("cagr", -1e9) or -1e9) >= float(g10.get("cagr", -1e9) or -1e9) else "MF_RANK_TOPK10"
    best = by_gid.get(best_gid, {})
    delta_cagr = _delta(best.get("cagr"), base.get("cagr"))
    over_filtered = (
        float(best.get("avg_positions", 0.0) or 0.0) < 1.0
        or float(best.get("trigger_when_slots_small_rate", 0.0) or 0.0) > 0.20
    )

    lines.extend(
        [
            "## 3) 一句话结论",
            f"- CAGR 最优组: {best_gid}（{_fmt_pct(best.get('cagr'))}），相对 BASE_SMA_ONLY 变化 {_fmt_pct(delta_cagr)}。",
            (
                "- 诊断：出现过度裁剪（avg_positions<1 或 slots<=1_rate>20%）。建议放宽 topK 或降低集中权重。"
                if over_filtered
                else "- 诊断：未见明显 over-filter（持仓与空位指标可接受）。"
            ),
            "",
            "## 4) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(max(float(by_gid.get(g, {}).get('precompute_seconds', 0.0) or 0.0) for g in groups), 3)}",
            f"- simulation_seconds: {_fmt_num(max(float(by_gid.get(g, {}).get('simulation_seconds', 0.0) or 0.0) for g in groups), 3)}",
            f"- io_write_seconds: {_fmt_num(max(float(by_gid.get(g, {}).get('io_write_seconds', 0.0) or 0.0) for g in groups), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_multifactor_ranking_walkforward_report(
    *,
    rows: list[dict[str, Any]],
    details_df: pd.DataFrame,
    full_start: str,
    full_end: str,
    universe_limit: int,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_base = _pick("ALL", "BASE_SMA_ONLY")
    all_mf = _pick("ALL", "MF_RANK_TOPK10")

    lines: list[str] = [
        "# MULTIFACTOR_RANKING_WALKFORWARD_v1",
        "",
        "## 1) 运行信息",
        f"- full_range: {full_start} ~ {full_end}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- benchmark: {benchmark}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 全区间汇总对照（BASE vs MF_TOPK10）",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | mf_rank_trigger_rate | mf_topk_reject_count | mf_concentrated_buy_rate | top10_profit_concentration_pct |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid, row in [("BASE_SMA_ONLY", all_base), ("MF_RANK_TOPK10", all_mf)]:
        lines.append(
            f"| {gid} | {_fmt_pct(row.get('total_return'))} | {_fmt_pct(row.get('cagr'))} | {_fmt_pct(row.get('max_drawdown'))} | "
            f"{_fmt_num(row.get('sharpe'), 3)} | {_fmt_pct(row.get('vol_annual'))} | {_fmt_pct(row.get('max_daily_drawdown'))} | "
            f"{int(row.get('trades', 0) or 0)} | {_fmt_num(row.get('avg_positions'), 2)} | {_fmt_pct(row.get('turnover_avg'))} | "
            f"{_fmt_num(row.get('avg_candidates_per_day'), 2)} | {_fmt_num(row.get('avg_buys_per_day'), 2)} | {_fmt_pct(row.get('slots_le_1_rate'))} | "
            f"{_fmt_num(row.get('mean_order_value_scale'), 3)} | {_fmt_pct(row.get('mf_rank_trigger_rate'))} | {int(row.get('mf_topk_reject_count', 0) or 0)} | "
            f"{_fmt_pct(row.get('mf_concentrated_buy_rate'))} | {_fmt_pct(row.get('top10_profit_concentration_pct'))} |"
        )
    lines.append("")

    lines.extend(
        [
            "## 3) 多阶段结果表（P1/P2/P3）",
            "| phase | BASE_ret | MF_ret | Δret | BASE_mdd | MF_mdd | Δmdd |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    phase_wins = 0
    for ph in ["P1", "P2", "P3"]:
        b = _pick(ph, "BASE_SMA_ONLY")
        m = _pick(ph, "MF_RANK_TOPK10")
        d_ret = _delta(m.get("total_return"), b.get("total_return"))
        d_mdd = _delta(m.get("max_drawdown"), b.get("max_drawdown"))
        if (d_ret or 0.0) > 0 and (d_mdd or 0.0) > 0:
            phase_wins += 1
        lines.append(
            f"| {ph} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(m.get('total_return'))} | {_fmt_pct(d_ret)} | "
            f"{_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(m.get('max_drawdown'))} | {_fmt_pct(d_mdd)} |"
        )
    lines.append("")

    lines.extend(
        [
            "## 4) Walk-forward test 结果表（Fold1/2/3）",
            "| fold | train_range | test_range | BASE_ret | MF_ret | Δret | BASE_mdd | MF_mdd | Δmdd | mf_rank_trigger_rate |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    wf_wins = 0
    wf_delta_ret_vals: list[float] = []
    wf_delta_mdd_vals: list[float] = []
    for fd in ["Fold1", "Fold2", "Fold3"]:
        b = _pick(fd, "BASE_SMA_ONLY")
        m = _pick(fd, "MF_RANK_TOPK10")
        d_ret = _delta(m.get("total_return"), b.get("total_return"))
        d_mdd = _delta(m.get("max_drawdown"), b.get("max_drawdown"))
        if d_ret is not None:
            wf_delta_ret_vals.append(float(d_ret))
        if d_mdd is not None:
            wf_delta_mdd_vals.append(float(d_mdd))
        if (d_ret or 0.0) > 0 and (d_mdd or 0.0) > 0:
            wf_wins += 1
        train_range = f"{str(m.get('train_start') or b.get('train_start') or 'N/A')}~{str(m.get('train_end') or b.get('train_end') or 'N/A')}"
        test_range = f"{str(m.get('slice_start') or b.get('slice_start') or '')}~{str(m.get('slice_end') or b.get('slice_end') or '')}"
        lines.append(
            f"| {fd} | {train_range} | {test_range} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(m.get('total_return'))} | {_fmt_pct(d_ret)} | "
            f"{_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(m.get('max_drawdown'))} | {_fmt_pct(d_mdd)} | {_fmt_pct(m.get('mf_rank_trigger_rate'))} |"
        )
    lines.append("")

    wf_ret_med = float(pd.Series(wf_delta_ret_vals).median()) if wf_delta_ret_vals else 0.0
    wf_mdd_med = float(pd.Series(wf_delta_mdd_vals).median()) if wf_delta_mdd_vals else 0.0
    mf_trigger = float(all_mf.get("mf_rank_trigger_rate", 0.0) or 0.0)
    lines.extend(
        [
            "## 5) 稳定性结论",
            f"- MF 在阶段胜出：{phase_wins}/3",
            f"- MF 在 WF test 胜出：{wf_wins}/3",
            f"- WF Δret 中位数：{_fmt_pct(wf_ret_med)}",
            f"- WF Δmdd 中位数：{_fmt_pct(wf_mdd_med)}（回撤为负数，Δmdd>0 表示改善）",
            (f"- WARNING: mf_rank_trigger_rate={_fmt_pct(mf_trigger)} < 10%，结论可信度下降" if mf_trigger < 0.10 else f"- mf_rank_trigger_rate={_fmt_pct(mf_trigger)}，触发率充足"),
            "",
            "## 6) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(perf.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(perf.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(perf.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            f"- total_wall: {_fmt_num(perf.get('total_wall'), 3)}",
            "",
        ]
    )
    return "\n".join(lines)


def _run_mf_rank_regime_gate_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = MF_RANK_REGIME_GATE_REPORT_FILE,
) -> dict[str, Any]:
    ctx = RunContext(
        preset=str(preset),
        base_config_path=str(base_path),
        overrides=dict(overrides or {}),
    )
    variants = preset_variants(str(preset).upper())
    base_gid = "BASE_MF_OFF"
    always_gid = "MF_ALWAYS_ON"
    gated_gid = "MF_ONLY_RISK_ON"
    for v in variants:
        gid = str(v.get("group_id", ""))
        pf = dict(v.get("portfolio") or {})
        en = bool(pf.get("enable_multifactor_rank", False))
        mode = str(pf.get("mf_rank_gate_mode", "OFF")).upper()
        if not en:
            base_gid = gid or base_gid
        elif mode in {"ALWAYS", "ALWAYS_ON"}:
            always_gid = gid or always_gid
        else:
            gated_gid = gid or gated_gid
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    full_start = str(ov.get("start_date") or "2021-01-01")
    full_end = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    fast_mode = bool(ov.get("fast_mode", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = full_start
    bt_cfg["universe"]["end"] = full_end
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols
    tmp_cfg = exp_dir / "_mf_rank_gate_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "precompute_seconds": 0.0,
        "simulation_seconds": 0.0,
        "io_write_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
        "total_wall": 0.0,
    }
    wall_t0 = time.perf_counter()

    try:
        _emit_stage_progress(progress_hook, ctx, stage="universe_resolve", pct=8, message="阶段1/5：解析Universe成分")
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
        _close_stage(ctx, "universe_resolve")
    except UniverseLoadError as exc:
        _close_stage(ctx, "universe_resolve")
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {"group_id": base_gid, "status": "FAILED", "error": err},
            {"group_id": always_gid, "status": "FAILED", "error": err},
            {"group_id": gated_gid, "status": "FAILED", "error": err},
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_md = _build_mf_rank_regime_gate_report(
            title=(str(preset).upper() if str(preset).upper() == "MF_RANK_STRONG_GATE_VALIDATE_V1" else "MF_RANK_REGIME_GATE_VALIDATE_v1"),
            base_gid=base_gid,
            always_gid=always_gid,
            gated_gid=gated_gid,
            rows=rows,
            details_df=pd.DataFrame(),
            full_start=full_start,
            full_end=full_end,
            universe_limit=universe_limit,
            benchmark=benchmark,
            raw_count=0,
            filtered_count=0,
            excluded={},
            initial_capital=initial_capital,
            max_positions=max_positions,
            base_order_value=base_order_value,
            lot_mode=lot_mode,
            perf=perf,
            error=err,
        )
        report_path = write_report_to_local(report_filename, report_md)
        ctx.add_note(err)
        ctx.write_profile(
            results_dir=str(exp_dir),
            extras={
                "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
                "error": err,
                "variant_count": int(len(variants)),
                "row_count": int(len(rows)),
                "symbols_count": 0,
                "trading_days": 0,
            },
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_stage_progress(progress_hook, ctx, stage="data_load", pct=12, message="阶段2/5：加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(provider, data_cache, raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, perf)
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df_all = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    full_calendar_days = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)
    _close_stage(ctx, "data_load")

    hist_all = ohlcv_all.copy()
    hist_all["date"] = pd.to_datetime(hist_all["date"])
    hist_all["symbol"] = hist_all["symbol"].astype(str).str.lower()
    if filtered_symbols:
        hist_all = hist_all[hist_all["symbol"].isin(set(filtered_symbols))].copy()
    benchmark_df_all = benchmark_df_all.copy()
    benchmark_df_all["date"] = pd.to_datetime(benchmark_df_all["date"])

    slices = [
        {"name": "ALL", "segment": "ALL", "start": full_start, "end": full_end, "train_start": "", "train_end": ""},
        {"name": "P1", "segment": "REGIME", "start": "2021-01-01", "end": "2022-12-31", "train_start": "", "train_end": ""},
        {"name": "P2", "segment": "REGIME", "start": "2023-01-01", "end": "2024-12-31", "train_start": "", "train_end": ""},
        {"name": "P3", "segment": "REGIME", "start": "2025-01-01", "end": "2026-02-01", "train_start": "", "train_end": ""},
        {"name": "Fold1", "segment": "WF_TEST", "start": "2021-01-01", "end": "2022-12-31", "train_start": "N/A", "train_end": "N/A"},
        {"name": "Fold2", "segment": "WF_TEST", "start": "2023-01-01", "end": "2024-12-31", "train_start": "2021-01-01", "train_end": "2022-12-31"},
        {"name": "Fold3", "segment": "WF_TEST", "start": "2025-01-01", "end": "2026-02-01", "train_start": "2021-01-01", "train_end": "2024-12-31"},
    ]
    fs = date.fromisoformat(full_start)
    fe = date.fromisoformat(full_end)
    valid_slices: list[dict[str, str]] = []
    for sl in slices:
        ss = date.fromisoformat(str(sl["start"]))
        se = date.fromisoformat(str(sl["end"]))
        if se < fs or ss > fe:
            continue
        valid_slices.append(sl)

    _emit_stage_progress(progress_hook, ctx, stage="precompute", pct=16, message="阶段3/5：切片预计算")
    t1 = time.perf_counter()
    sliced_data: dict[str, tuple[pd.DataFrame, pd.DataFrame, list[date]]] = {}
    for sl in valid_slices:
        s_name = str(sl["name"])
        s_start = pd.Timestamp(str(sl["start"]))
        s_end = pd.Timestamp(str(sl["end"]))
        h = hist_all[(hist_all["date"] >= s_start) & (hist_all["date"] <= s_end)].copy()
        b = benchmark_df_all[(benchmark_df_all["date"] >= s_start) & (benchmark_df_all["date"] <= s_end)].copy()
        days = [d for d in full_calendar_days if d >= s_start.date() and d <= s_end.date()]
        sliced_data[s_name] = (h, b, days)
    perf["precompute_seconds"] = float(time.perf_counter() - t1)
    _close_stage(ctx, "precompute")

    run_id = f"run_mf_rank_gate_{exp_id[:10]}"
    runs: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    total_jobs = max(len(valid_slices) * len(variants), 1)
    job_idx = 0
    _emit_stage_progress(progress_hook, ctx, stage="simulation", pct=22, message="阶段4/5：执行组合仿真")
    t2 = time.perf_counter()
    for sl in valid_slices:
        s_name = str(sl["name"])
        h, b, days = sliced_data[s_name]
        for variant in variants:
            job_idx += 1
            group_id = str(variant.get("group_id", ""))
            _emit_progress(progress_hook, 15 + int(job_idx * 70 / total_jobs), f"[{job_idx}/{total_jobs}] {s_name}-{group_id}")
            strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
            portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
            run_dir = _results_root() / run_id / s_name / group_id
            run_dir.mkdir(parents=True, exist_ok=True)
            strategy_cfg = StrategyConfig(
                name=str(strategy_def.get("name", "sma_cross_regime_atr")),
                params=copy.deepcopy(strategy_def.get("params", {}) or {}),
            )
            strategy = build_strategy(strategy_cfg)
            strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
            result = run_portfolio_backtest(
                strategy=strategy,
                history=h,
                benchmark=b,
                trading_days=days,
                benchmark_symbol=benchmark,
                initial_cash=float(initial_capital),
                max_positions=int(portfolio_def.get("max_positions", max_positions)),
                lot_size=int(lot_size),
                enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
                strength_ma=int(portfolio_def.get("strength_ma", 120)),
                strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
                engine_mode=str(portfolio_def.get("engine_mode", "VECTOR_FAST_STRICT")),
                fast_mode=bool(fast_mode),
                enable_multifactor_rank=bool(portfolio_def.get("enable_multifactor_rank", False)),
                mf_topk=int(portfolio_def.get("mf_topk", 10)),
                mf_concentrate_topn=int(portfolio_def.get("mf_concentrate_topn", 3)),
                mf_concentrate_scale=float(portfolio_def.get("mf_concentrate_scale", 1.5)),
                mf_rank_gate_mode=str(portfolio_def.get("mf_rank_gate_mode", "ALWAYS_ON")),
            )
            m = dict(result.metrics or {})
            detail_rows.append(
                {
                    "slice_name": s_name,
                    "segment": str(sl.get("segment", "")),
                    "train_start": str(sl.get("train_start", "")),
                    "train_end": str(sl.get("train_end", "")),
                    "slice_start": str(sl.get("start", "")),
                    "slice_end": str(sl.get("end", "")),
                    "group_id": group_id,
                    "run_id": run_id,
                    "total_return": float(m.get("total_return", 0.0)),
                    "cagr": float(m.get("cagr", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "vol_annual": float(m.get("vol_annual", 0.0)),
                    "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
                    "trades": int(m.get("trades", 0)),
                    "avg_positions": float(m.get("avg_positions", 0.0)),
                    "turnover_avg": float(m.get("turnover_avg", 0.0)),
                    "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
                    "avg_buys_per_day": float(m.get("avg_buys_per_day", 0.0)),
                    "slots_le_1_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
                    "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
                    "mf_gate_on_rate": float(m.get("mf_gate_on_rate", 0.0)),
                    "mf_rank_trigger_rate": float(m.get("mf_rank_trigger_rate", 0.0)),
                    "mf_rank_applied_rate": float(m.get("mf_rank_applied_rate", 0.0)),
                    "mf_rank_applied_days": int(m.get("mf_rank_applied_days", 0)),
                    "mf_topk_reject_count": int(m.get("mf_topk_reject_count", 0)),
                    "mf_concentrated_buy_rate": float(m.get("mf_concentrated_buy_rate", 0.0)),
                    "strong_risk_on_rate": float(m.get("strong_risk_on_rate", 0.0)),
                    "strong_risk_on_days": int(m.get("strong_risk_on_days", 0)),
                    "mf_rank_suppressed_by_strong_gate_days": int(m.get("mf_rank_suppressed_by_strong_gate_days", 0)),
                    "top10_profit_concentration_pct": float(m.get("top10_profit_concentration_pct", 0.0)),
                }
            )
            runs.append(
                {
                    "group_id": f"{s_name}_{group_id}",
                    "run_id": run_id,
                    "status": "OK",
                    "results_dir": str(run_dir),
                    "strategy_name": str(strategy_cfg.name),
                    "params_json": json.dumps(
                        {
                            **strategy_cfg.params,
                            "enable_multifactor_rank": bool(portfolio_def.get("enable_multifactor_rank", False)),
                            "mf_rank_gate_mode": str(portfolio_def.get("mf_rank_gate_mode", "ALWAYS_ON")),
                            "mf_topk": int(portfolio_def.get("mf_topk", 10)),
                            "mf_concentrate_topn": int(portfolio_def.get("mf_concentrate_topn", 3)),
                            "mf_concentrate_scale": float(portfolio_def.get("mf_concentrate_scale", 1.5)),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
    perf["simulation_seconds"] = float(time.perf_counter() - t2)
    _close_stage(ctx, "simulation")
    _emit_stage_progress(progress_hook, ctx, stage="output_write", pct=90, message="阶段5/5：写出产物与报告")

    details_df = pd.DataFrame(detail_rows)
    io_t0 = time.perf_counter()
    if not details_df.empty:
        details_df.to_csv(exp_dir / "mf_rank_regime_gate_details.csv", index=False)
    perf["io_write_seconds"] = float(time.perf_counter() - io_t0)
    perf["cache_hit_rate"] = float(perf.get("cache_hit", 0)) / float(perf.get("cache_total", 1))
    perf["total_wall"] = float(time.perf_counter() - wall_t0)

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_base = _pick("ALL", base_gid)
    all_always = _pick("ALL", always_gid)
    all_only = _pick("ALL", gated_gid)

    rows = [
        {"group_id": f"ALL_{base_gid}", "status": "OK", "run_id": run_id, "total_return": _safe_num(all_base.get("total_return")), "max_drawdown": _safe_num(all_base.get("max_drawdown")), "trades": int(_safe_num(all_base.get("trades")) or 0), "avg_positions": _safe_num(all_base.get("avg_positions")), "mf_gate_on_rate": _safe_num(all_base.get("mf_gate_on_rate")), "mf_rank_trigger_rate": _safe_num(all_base.get("mf_rank_trigger_rate")), "mf_rank_applied_rate": _safe_num(all_base.get("mf_rank_applied_rate")), "mf_rank_applied_days": int(_safe_num(all_base.get("mf_rank_applied_days")) or 0), "strong_risk_on_rate": _safe_num(all_base.get("strong_risk_on_rate")), "mf_rank_suppressed_by_strong_gate_days": int(_safe_num(all_base.get("mf_rank_suppressed_by_strong_gate_days")) or 0)},
        {"group_id": f"ALL_{always_gid}", "status": "OK", "run_id": run_id, "total_return": _safe_num(all_always.get("total_return")), "max_drawdown": _safe_num(all_always.get("max_drawdown")), "trades": int(_safe_num(all_always.get("trades")) or 0), "avg_positions": _safe_num(all_always.get("avg_positions")), "mf_gate_on_rate": _safe_num(all_always.get("mf_gate_on_rate")), "mf_rank_trigger_rate": _safe_num(all_always.get("mf_rank_trigger_rate")), "mf_rank_applied_rate": _safe_num(all_always.get("mf_rank_applied_rate")), "mf_rank_applied_days": int(_safe_num(all_always.get("mf_rank_applied_days")) or 0), "strong_risk_on_rate": _safe_num(all_always.get("strong_risk_on_rate")), "mf_rank_suppressed_by_strong_gate_days": int(_safe_num(all_always.get("mf_rank_suppressed_by_strong_gate_days")) or 0)},
        {"group_id": f"ALL_{gated_gid}", "status": "OK", "run_id": run_id, "total_return": _safe_num(all_only.get("total_return")), "max_drawdown": _safe_num(all_only.get("max_drawdown")), "trades": int(_safe_num(all_only.get("trades")) or 0), "avg_positions": _safe_num(all_only.get("avg_positions")), "mf_gate_on_rate": _safe_num(all_only.get("mf_gate_on_rate")), "mf_rank_trigger_rate": _safe_num(all_only.get("mf_rank_trigger_rate")), "mf_rank_applied_rate": _safe_num(all_only.get("mf_rank_applied_rate")), "mf_rank_applied_days": int(_safe_num(all_only.get("mf_rank_applied_days")) or 0), "strong_risk_on_rate": _safe_num(all_only.get("strong_risk_on_rate")), "mf_rank_suppressed_by_strong_gate_days": int(_safe_num(all_only.get("mf_rank_suppressed_by_strong_gate_days")) or 0)},
    ]
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)

    report_md = _build_mf_rank_regime_gate_report(
        title=(str(preset).upper() if str(preset).upper() == "MF_RANK_STRONG_GATE_VALIDATE_V1" else "MF_RANK_REGIME_GATE_VALIDATE_v1"),
        base_gid=base_gid,
        always_gid=always_gid,
        gated_gid=gated_gid,
        rows=rows,
        details_df=details_df,
        full_start=full_start,
        full_end=full_end,
        universe_limit=universe_limit,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf,
        error=None,
    )
    report_path = write_report_to_local(report_filename, report_md)
    _emit_progress(progress_hook, 95, f"写入报告：{report_path}")
    _close_stage(ctx, "output_write")
    ctx.write_profile(
        results_dir=str(exp_dir),
        extras={
            "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
            "perf": {
                "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
                "precompute_seconds": float(perf.get("precompute_seconds", 0.0) or 0.0),
                "simulation_seconds": float(perf.get("simulation_seconds", 0.0) or 0.0),
                "io_write_seconds": float(perf.get("io_write_seconds", 0.0) or 0.0),
                "db_queries": int(perf.get("db_queries", 0) or 0),
                "cache_hit_rate": float(perf.get("cache_hit_rate", 0.0) or 0.0),
                "total_wall": float(perf.get("total_wall", 0.0) or 0.0),
            },
            "slice_count": int(len(valid_slices)),
            "variant_count": int(len(variants)),
            "row_count": int(len(rows)),
            "detail_row_count": int(len(detail_rows)),
            "symbols_count": int(len(filtered_symbols)),
            "trading_days": int(len(full_calendar_days)),
        },
    )
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _build_mf_rank_regime_gate_report(
    *,
    title: str,
    base_gid: str,
    always_gid: str,
    gated_gid: str,
    rows: list[dict[str, Any]],
    details_df: pd.DataFrame,
    full_start: str,
    full_end: str,
    universe_limit: int,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    def _delta(a: Any, b: Any) -> float | None:
        try:
            return float(a) - float(b)
        except Exception:
            return None

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    lines: list[str] = [
        f"# {title}",
        "",
        "## 1) 运行信息",
        f"- full_range: {full_start} ~ {full_end}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- benchmark: {benchmark}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 全区间核心对照表",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | strong_risk_on_rate | mf_gate_on_rate | mf_rank_trigger_rate | mf_rank_applied_days | mf_rank_applied_rate | mf_rank_suppressed_by_strong_gate_days | mf_topk_reject_count | mf_concentrated_buy_rate | top10_profit_concentration_pct |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in [base_gid, always_gid, gated_gid]:
        r = _pick("ALL", gid)
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | {_fmt_pct(r.get('slots_le_1_rate'))} | "
            f"{_fmt_num(r.get('mean_order_value_scale'), 3)} | {_fmt_pct(r.get('strong_risk_on_rate'))} | {_fmt_pct(r.get('mf_gate_on_rate'))} | {_fmt_pct(r.get('mf_rank_trigger_rate'))} | "
            f"{int(r.get('mf_rank_applied_days', 0) or 0)} | {_fmt_pct(r.get('mf_rank_applied_rate'))} | "
            f"{int(r.get('mf_rank_suppressed_by_strong_gate_days', 0) or 0)} | "
            f"{int(r.get('mf_topk_reject_count', 0) or 0)} | {_fmt_pct(r.get('mf_concentrated_buy_rate'))} | {_fmt_pct(r.get('top10_profit_concentration_pct'))} |"
        )
    lines.append("")

    lines.extend(
        [
            "## 3) 多阶段结果表（P1/P2/P3）",
            "| phase | BASE_ret | ALWAYS_ret | GATED_ret | Δret(ALWAYS-BASE) | Δret(GATED-BASE) | BASE_mdd | ALWAYS_mdd | GATED_mdd | Δmdd(ALWAYS-BASE) | Δmdd(GATED-BASE) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for ph in ["P1", "P2", "P3"]:
        b = _pick(ph, base_gid)
        a = _pick(ph, always_gid)
        o = _pick(ph, gated_gid)
        lines.append(
            f"| {ph} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(a.get('total_return'))} | {_fmt_pct(o.get('total_return'))} | "
            f"{_fmt_pct(_delta(a.get('total_return'), b.get('total_return')))} | {_fmt_pct(_delta(o.get('total_return'), b.get('total_return')))} | "
            f"{_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(a.get('max_drawdown'))} | {_fmt_pct(o.get('max_drawdown'))} | "
            f"{_fmt_pct(_delta(a.get('max_drawdown'), b.get('max_drawdown')))} | {_fmt_pct(_delta(o.get('max_drawdown'), b.get('max_drawdown')))} |"
        )
    lines.append("")

    lines.extend(
        [
            "## 4) 3折 Walk-forward test 结果表",
            "| fold | train_range | test_range | BASE_ret | ALWAYS_ret | GATED_ret | Δret(ALWAYS-BASE) | Δret(GATED-BASE) | mf_gate_on_rate(GATED) | mf_rank_applied_rate(GATED) |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for fd in ["Fold1", "Fold2", "Fold3"]:
        b = _pick(fd, base_gid)
        a = _pick(fd, always_gid)
        o = _pick(fd, gated_gid)
        train_range = f"{str(o.get('train_start') or b.get('train_start') or 'N/A')}~{str(o.get('train_end') or b.get('train_end') or 'N/A')}"
        test_range = f"{str(o.get('slice_start') or b.get('slice_start') or '')}~{str(o.get('slice_end') or b.get('slice_end') or '')}"
        lines.append(
            f"| {fd} | {train_range} | {test_range} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(a.get('total_return'))} | {_fmt_pct(o.get('total_return'))} | "
            f"{_fmt_pct(_delta(a.get('total_return'), b.get('total_return')))} | {_fmt_pct(_delta(o.get('total_return'), b.get('total_return')))} | {_fmt_pct(o.get('mf_gate_on_rate'))} | {_fmt_pct(o.get('mf_rank_applied_rate'))} |"
        )
    lines.append("")

    only_p1 = _pick("P1", gated_gid)
    only_p2 = _pick("P2", gated_gid)
    only_p3 = _pick("P3", gated_gid)
    base_p1 = _pick("P1", base_gid)
    base_p2 = _pick("P2", base_gid)
    base_p3 = _pick("P3", base_gid)
    d1 = _delta(only_p1.get("total_return"), base_p1.get("total_return")) or 0.0
    d2 = _delta(only_p2.get("total_return"), base_p2.get("total_return")) or 0.0
    d3 = _delta(only_p3.get("total_return"), base_p3.get("total_return")) or 0.0
    all_always = _pick("ALL", always_gid)
    all_only = _pick("ALL", gated_gid)
    degenerate_gate = False
    try:
        gate_gap = abs(float(all_always.get("mf_gate_on_rate", 0.0) or 0.0) - float(all_only.get("mf_gate_on_rate", 0.0) or 0.0))
        applied_gap = abs(float(all_always.get("mf_rank_applied_rate", 0.0) or 0.0) - float(all_only.get("mf_rank_applied_rate", 0.0) or 0.0))
        ret_gap = abs(float(all_always.get("total_return", 0.0) or 0.0) - float(all_only.get("total_return", 0.0) or 0.0))
        mdd_gap = abs(float(all_always.get("max_drawdown", 0.0) or 0.0) - float(all_only.get("max_drawdown", 0.0) or 0.0))
        degenerate_gate = gate_gap >= 0.1 and applied_gap < 1e-12 and ret_gap < 1e-12 and mdd_gap < 1e-12
    except Exception:
        degenerate_gate = False
    lines.extend(
        [
            "## 5) 一句话结论",
            f"- {gated_gid}：P1/P2 相对 BASE Δret={_fmt_pct(d1)}/{_fmt_pct(d2)}，P3 Δret={_fmt_pct(d3)}；若前两段减亏且后段保留增益，说明 gate 有效。",
            (
                "- WARNING: 门控退化为与 ALWAYS_ON 等价执行。常见原因是 BUY 候选本身已被 regime_on 限制，导致候选几乎只在 risk_on 日出现。"
                if degenerate_gate
                else f"- 门控执行已生效：{gated_gid} 的 gate/applied 统计与 {always_gid} 可区分。"
            ),
            "",
            "## 6) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(perf.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(perf.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(perf.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            f"- total_wall: {_fmt_num(perf.get('total_wall'), 3)}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_candidate_diag_report(
    *,
    start_s: str,
    end_s: str,
    benchmark: str,
    universe_limit: int,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    daily_df: pd.DataFrame,
    reason_counts: dict[str, int],
    perf: dict[str, Any],
    error: str | None,
) -> str:
    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    def _summary(s: pd.Series) -> dict[str, float]:
        x = pd.to_numeric(s, errors="coerce").dropna()
        if x.empty:
            return {"mean": 0.0, "median": 0.0, "p10": 0.0, "p90": 0.0}
        return {
            "mean": float(x.mean()),
            "median": float(x.median()),
            "p10": float(x.quantile(0.10)),
            "p90": float(x.quantile(0.90)),
        }

    lines: list[str] = [
        "# CANDIDATE_DIAG_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        f"- benchmark: {benchmark}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "- lot_mode: LOT100_MIN1LOT",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    funnel_cols = [
        "raw_buy_signal_count",
        "buy_candidate_count",
        "after_held_filter_count",
        "after_slot_filter_count",
        "after_cash_lot_count",
        "buy_slots_after_sell",
        "positions_count_end_of_day",
    ]
    for c in funnel_cols:
        if c not in daily_df.columns:
            daily_df[c] = 0

    lines.extend(
        [
            "## 2) 漏斗汇总表",
            "| metric | mean | median | p10 | p90 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    summary_map: dict[str, dict[str, float]] = {}
    for c in funnel_cols:
        stats = _summary(daily_df[c])
        summary_map[c] = stats
        lines.append(
            f"| {c} | {_fmt_num(stats['mean'], 4)} | {_fmt_num(stats['median'], 4)} | {_fmt_num(stats['p10'], 4)} | {_fmt_num(stats['p90'], 4)} |"
        )
    lines.append("")

    raw_mean = float(summary_map["raw_buy_signal_count"]["mean"] or 0.0)
    cand_mean = float(summary_map["buy_candidate_count"]["mean"] or 0.0)
    held_mean = float(summary_map["after_held_filter_count"]["mean"] or 0.0)
    slot_mean = float(summary_map["after_slot_filter_count"]["mean"] or 0.0)
    fill_mean = float(summary_map["after_cash_lot_count"]["mean"] or 0.0)

    def _safe_ratio(a: float, b: float) -> float:
        return (a / b) if b > 0 else 0.0

    candidate_rate = _safe_ratio(cand_mean, raw_mean)
    held_block_rate = 1.0 - _safe_ratio(held_mean, cand_mean)
    slot_block_rate = 1.0 - _safe_ratio(slot_mean, held_mean)
    cash_lot_block_rate = 1.0 - _safe_ratio(fill_mean, slot_mean)

    lines.extend(
        [
            "## 3) 关键占比",
            f"- candidate_rate = buy_candidate/raw_buy_signal = {_fmt_pct(candidate_rate)}",
            f"- held_block_rate = 1 - after_held/buy_candidate = {_fmt_pct(held_block_rate)}",
            f"- slot_block_rate = 1 - after_slot/after_held = {_fmt_pct(slot_block_rate)}",
            f"- cash_lot_block_rate = 1 - after_cash_lot/after_slot = {_fmt_pct(cash_lot_block_rate)}",
            "",
            "## 4) 拦截原因 Top10",
        ]
    )
    total_reason = int(sum(int(v or 0) for v in reason_counts.values()) or 0)
    if total_reason <= 0:
        lines.append("- 无拦截原因统计")
    else:
        top = sorted(reason_counts.items(), key=lambda kv: int(kv[1]), reverse=True)[:10]
        for k, v in top:
            pct_v = float(v) / float(total_reason) if total_reason > 0 else 0.0
            lines.append(f"- {k}: {int(v)} ({pct_v * 100:.2f}%)")

    lines.append("")
    lines.append("## 5) 一句话结论")
    if raw_mean <= 1e-9:
        reason = "信号几乎没有（A≈0）"
    elif cand_mean <= 1e-9:
        reason = "过滤太严（A>0 但 B≈0）"
    elif held_mean <= 1e-9:
        reason = "持仓重复/选股池太小（B>0 但 C≈0）"
    elif slot_mean <= 1e-9:
        reason = "空位不足（C>0 但 D≈0）"
    elif fill_mean <= 1e-9:
        reason = "资金/整手导致下不去单（D>0 但 E≈0）"
    else:
        reason = "候选与执行链路正常，可进一步看收益与回撤优化"
    lines.append(f"- 根因判断：{reason}")
    lines.append("")

    lines.extend(
        [
            "## 6) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(perf.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(perf.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(perf.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "",
        ]
    )
    return "\n".join(lines)


def _run_trend_quality_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = TREND_QUALITY_REPORT_FILE,
) -> dict[str, Any]:
    ctx = RunContext(
        preset=str(preset),
        base_config_path=str(base_path),
        overrides=dict(overrides or {}),
    )
    variants = preset_variants("TREND_QUALITY_VALIDATE_V1")
    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    full_start = str(ov.get("start_date") or "2021-01-01")
    full_end = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    max_positions = int(ov.get("max_positions", 10))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    fast_mode = bool(ov.get("fast_mode", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = full_start
    bt_cfg["universe"]["end"] = full_end
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols
    tmp_cfg = exp_dir / "_trend_quality_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "precompute_seconds": 0.0,
        "simulation_seconds": 0.0,
        "io_write_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
        "total_wall": 0.0,
    }
    wall_t0 = time.perf_counter()

    _emit_stage_progress(progress_hook, ctx, stage="universe_resolve", pct=8, message="阶段1/5：解析Universe成分")
    try:
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
    finally:
        _close_stage(ctx, "universe_resolve")

    _emit_stage_progress(progress_hook, ctx, stage="data_load", pct=12, message="阶段2/5：加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(provider, data_cache, raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, perf)
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df_all = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    full_calendar_days = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)
    _close_stage(ctx, "data_load")

    hist_all = ohlcv_all.copy()
    hist_all["date"] = pd.to_datetime(hist_all["date"])
    hist_all["symbol"] = hist_all["symbol"].astype(str).str.lower()
    if filtered_symbols:
        hist_all = hist_all[hist_all["symbol"].isin(set(filtered_symbols))].copy()
    benchmark_df_all = benchmark_df_all.copy()
    benchmark_df_all["date"] = pd.to_datetime(benchmark_df_all["date"])

    slices = [
        {"name": "ALL", "segment": "ALL", "start": full_start, "end": full_end, "train_start": "", "train_end": ""},
        {"name": "P1", "segment": "REGIME", "start": "2021-01-01", "end": "2022-12-31", "train_start": "", "train_end": ""},
        {"name": "P2", "segment": "REGIME", "start": "2023-01-01", "end": "2024-12-31", "train_start": "", "train_end": ""},
        {"name": "P3", "segment": "REGIME", "start": "2025-01-01", "end": "2026-02-01", "train_start": "", "train_end": ""},
        {"name": "Fold1", "segment": "WF_TEST", "start": "2021-01-01", "end": "2022-12-31", "train_start": "N/A", "train_end": "N/A"},
        {"name": "Fold2", "segment": "WF_TEST", "start": "2023-01-01", "end": "2024-12-31", "train_start": "2021-01-01", "train_end": "2022-12-31"},
        {"name": "Fold3", "segment": "WF_TEST", "start": "2025-01-01", "end": "2026-02-01", "train_start": "2021-01-01", "train_end": "2024-12-31"},
    ]
    fs = date.fromisoformat(full_start)
    fe = date.fromisoformat(full_end)
    valid_slices: list[dict[str, str]] = []
    for sl in slices:
        ss = date.fromisoformat(str(sl["start"]))
        se = date.fromisoformat(str(sl["end"]))
        if se < fs or ss > fe:
            continue
        valid_slices.append(sl)

    _emit_stage_progress(progress_hook, ctx, stage="precompute", pct=16, message="阶段3/5：切片预计算")
    t1 = time.perf_counter()
    sliced_data: dict[str, tuple[pd.DataFrame, pd.DataFrame, list[date]]] = {}
    for sl in valid_slices:
        s_name = str(sl["name"])
        s_start = pd.Timestamp(str(sl["start"]))
        s_end = pd.Timestamp(str(sl["end"]))
        h = hist_all[(hist_all["date"] >= s_start) & (hist_all["date"] <= s_end)].copy()
        b = benchmark_df_all[(benchmark_df_all["date"] >= s_start) & (benchmark_df_all["date"] <= s_end)].copy()
        days = [d for d in full_calendar_days if d >= s_start.date() and d <= s_end.date()]
        sliced_data[s_name] = (h, b, days)
    perf["precompute_seconds"] = float(time.perf_counter() - t1)
    _close_stage(ctx, "precompute")

    run_id = f"run_trend_quality_{exp_id[:10]}"
    runs: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    total_jobs = max(len(valid_slices) * len(variants), 1)
    job_idx = 0
    _emit_stage_progress(progress_hook, ctx, stage="simulation", pct=22, message="阶段4/5：执行组合仿真")
    t2 = time.perf_counter()
    for sl in valid_slices:
        s_name = str(sl["name"])
        h, b, days = sliced_data[s_name]
        for variant in variants:
            job_idx += 1
            group_id = str(variant.get("group_id", ""))
            _emit_progress(progress_hook, 15 + int(job_idx * 70 / total_jobs), f"[{job_idx}/{total_jobs}] {s_name}-{group_id}")
            strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
            portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
            run_dir = _results_root() / run_id / s_name / group_id
            run_dir.mkdir(parents=True, exist_ok=True)
            strategy_cfg = StrategyConfig(
                name=str(strategy_def.get("name", "sma_cross_regime_atr")),
                params=copy.deepcopy(strategy_def.get("params", {}) or {}),
            )
            strategy = build_strategy(strategy_cfg)
            strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
            result = run_portfolio_backtest(
                strategy=strategy,
                history=h,
                benchmark=b,
                trading_days=days,
                benchmark_symbol=benchmark,
                initial_cash=float(initial_capital),
                max_positions=int(portfolio_def.get("max_positions", max_positions)),
                lot_size=int(lot_size),
                enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
                strength_ma=int(portfolio_def.get("strength_ma", 120)),
                strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
                engine_mode=str(portfolio_def.get("engine_mode", "VECTOR_FAST_STRICT")),
                fast_mode=bool(fast_mode),
                enable_multifactor_rank=bool(portfolio_def.get("enable_multifactor_rank", False)),
                mf_topk=int(portfolio_def.get("mf_topk", 10)),
                mf_concentrate_topn=int(portfolio_def.get("mf_concentrate_topn", 3)),
                mf_concentrate_scale=float(portfolio_def.get("mf_concentrate_scale", 1.5)),
                mf_rank_gate_mode=str(portfolio_def.get("mf_rank_gate_mode", "OFF")),
            )
            m = dict(result.metrics or {})
            detail_rows.append(
                {
                    "slice_name": s_name,
                    "segment": str(sl.get("segment", "")),
                    "train_start": str(sl.get("train_start", "")),
                    "train_end": str(sl.get("train_end", "")),
                    "slice_start": str(sl.get("start", "")),
                    "slice_end": str(sl.get("end", "")),
                    "group_id": group_id,
                    "run_id": run_id,
                    "total_return": float(m.get("total_return", 0.0)),
                    "cagr": float(m.get("cagr", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "vol_annual": float(m.get("vol_annual", 0.0)),
                    "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
                    "trades": int(m.get("trades", 0)),
                    "avg_positions": float(m.get("avg_positions", 0.0)),
                    "turnover_avg": float(m.get("turnover_avg", 0.0)),
                    "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
                    "avg_buys_per_day": float(m.get("avg_buys_per_day", 0.0)),
                    "slots_le_1_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
                    "trend_slope_block_count": int(m.get("trend_slope_block_count", 0)),
                    "trend_continuity_block_count": int(m.get("trend_continuity_block_count", 0)),
                    "trend_filter_block_rate": float(m.get("trend_filter_block_rate", 0.0)),
                    "avg_trend_on_rate": float(m.get("avg_trend_on_rate", 0.0)),
                    "avg_quality_trend_on_rate": float(m.get("avg_quality_trend_on_rate", 0.0)),
                }
            )
            runs.append({"group_id": f"{s_name}_{group_id}", "run_id": run_id, "status": "OK", "results_dir": str(run_dir), "strategy_name": str(strategy_cfg.name)})
    perf["simulation_seconds"] = float(time.perf_counter() - t2)
    _close_stage(ctx, "simulation")
    _emit_stage_progress(progress_hook, ctx, stage="output_write", pct=90, message="阶段5/5：写出产物与报告")

    details_df = pd.DataFrame(detail_rows)
    io_t0 = time.perf_counter()
    if not details_df.empty:
        details_df.to_csv(exp_dir / "trend_quality_details.csv", index=False)
    perf["io_write_seconds"] = float(time.perf_counter() - io_t0)
    perf["cache_hit_rate"] = float(perf.get("cache_hit", 0)) / float(perf.get("cache_total", 1))
    perf["total_wall"] = float(time.perf_counter() - wall_t0)

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_rows = [_pick("ALL", "BASE_TREND"), _pick("ALL", "SLOPE_ON"), _pick("ALL", "SLOPE_CONT10")]
    rows = []
    gids = ["BASE_TREND", "SLOPE_ON", "SLOPE_CONT10"]
    for gid, rr in zip(gids, all_rows):
        rows.append(
            {
                "group_id": f"ALL_{gid}",
                "status": "OK",
                "run_id": run_id,
                "total_return": _safe_num(rr.get("total_return")),
                "max_drawdown": _safe_num(rr.get("max_drawdown")),
                "trades": int(_safe_num(rr.get("trades")) or 0),
                "avg_positions": _safe_num(rr.get("avg_positions")),
                "trend_filter_block_rate": _safe_num(rr.get("trend_filter_block_rate")),
                "avg_trend_on_rate": _safe_num(rr.get("avg_trend_on_rate")),
                "avg_quality_trend_on_rate": _safe_num(rr.get("avg_quality_trend_on_rate")),
            }
        )
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)

    report_md = _build_trend_quality_report(
        details_df=details_df,
        full_start=full_start,
        full_end=full_end,
        universe_limit=universe_limit,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf,
    )
    report_path = write_report_to_local(report_filename, report_md)
    _emit_progress(progress_hook, 95, f"写入报告：{report_path}")
    _close_stage(ctx, "output_write")
    ctx.write_profile(
        results_dir=str(exp_dir),
        extras={
            "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
            "perf": perf,
            "slice_count": int(len(valid_slices)),
            "variant_count": int(len(variants)),
            "row_count": int(len(rows)),
            "detail_row_count": int(len(detail_rows)),
            "symbols_count": int(len(filtered_symbols)),
            "trading_days": int(len(full_calendar_days)),
        },
    )
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(report_path),
    }


def _build_trend_quality_report(
    *,
    details_df: pd.DataFrame,
    full_start: str,
    full_end: str,
    universe_limit: int,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
) -> str:
    def _fmt_pct(v: Any) -> str:
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    def _pick(slice_name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["slice_name"] == slice_name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    groups = ["BASE_TREND", "SLOPE_ON", "SLOPE_CONT10"]
    lines: list[str] = [
        "# TREND_QUALITY_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {full_start} ~ {full_end}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- benchmark: {benchmark}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
        "## 2) 核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | trend_slope_block_count | trend_continuity_block_count | trend_filter_block_rate | avg_trend_on_rate | avg_quality_trend_on_rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in groups:
        r = _pick("ALL", gid)
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | {_fmt_pct(r.get('slots_le_1_rate'))} | "
            f"{int(r.get('trend_slope_block_count', 0) or 0)} | {int(r.get('trend_continuity_block_count', 0) or 0)} | {_fmt_pct(r.get('trend_filter_block_rate'))} | "
            f"{_fmt_pct(r.get('avg_trend_on_rate'))} | {_fmt_pct(r.get('avg_quality_trend_on_rate'))} |"
        )
    lines.append("")
    lines.extend(
        [
            "## 3) 多阶段 P1/P2/P3",
            "| phase | BASE_ret | SLOPE_ret | SLOPE_CONT10_ret | Δret(SLOPE-BASE) | Δret(SLOPE_CONT10-BASE) | BASE_mdd | SLOPE_mdd | SLOPE_CONT10_mdd | Δmdd(SLOPE-BASE) | Δmdd(SLOPE_CONT10-BASE) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for ph in ["P1", "P2", "P3"]:
        b = _pick(ph, "BASE_TREND")
        s = _pick(ph, "SLOPE_ON")
        c = _pick(ph, "SLOPE_CONT10")
        d1 = (float(s.get("total_return", 0.0)) - float(b.get("total_return", 0.0))) if s and b else 0.0
        d2 = (float(c.get("total_return", 0.0)) - float(b.get("total_return", 0.0))) if c and b else 0.0
        m1 = (float(s.get("max_drawdown", 0.0)) - float(b.get("max_drawdown", 0.0))) if s and b else 0.0
        m2 = (float(c.get("max_drawdown", 0.0)) - float(b.get("max_drawdown", 0.0))) if c and b else 0.0
        lines.append(
            f"| {ph} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(s.get('total_return'))} | {_fmt_pct(c.get('total_return'))} | "
            f"{_fmt_pct(d1)} | {_fmt_pct(d2)} | {_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(s.get('max_drawdown'))} | {_fmt_pct(c.get('max_drawdown'))} | {_fmt_pct(m1)} | {_fmt_pct(m2)} |"
        )
    lines.extend(
        [
            "",
            "## 4) 一句话结论",
            "- 观察 SLOPE_ON / SLOPE_CONT10 相对 BASE_TREND 的 CAGR 与回撤变化；若 avg_positions 明显下降且 slots<=1_rate 明显上升，说明出现 over-filter。",
            "- 若 over-filter，可降低 continuity_n 或放宽 slope_slow 条件。",
            "",
            "## 5) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(perf.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(perf.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(perf.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            f"- total_wall: {_fmt_num(perf.get('total_wall'), 3)}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_portfolio_report_error(
    *,
    start_s: str,
    end_s: str,
    universe_limit: int | None,
    initial_capital: float,
    max_positions: int,
    raw_count: int,
    filtered_count: int,
    error: str,
) -> str:
    return "\n".join(
        [
            "# ATR Portfolio Validate v1",
            "",
            "## 1) 运行信息",
            f"- date_range: {start_s} ~ {end_s}",
            f"- universe_limit: {universe_limit}",
            f"- initial_cash: {initial_capital:.0f}",
            f"- max_positions: {max_positions}",
            f"- raw_count: {raw_count}",
            f"- filtered_count: {filtered_count}",
            f"- ERROR: {error}",
            "",
        ]
    ) + "\n"


def _build_full_range_validate_report(
    *,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("BASE_ATR", {})
    rank = by_gid.get("RANK_ON", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    lines: list[str] = [
        "# FULL_RANGE_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- benchmark: {benchmark}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "- lot_mode: LOT100_MIN1LOT",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 核心对照表",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | rank_trigger_rate | avg_candidates_per_day | slots<=1_rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in ["BASE_ATR", "RANK_ON"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_pct(r.get('rank_trigger_rate'))} | {_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_pct(r.get('trigger_when_slots_small_rate'))} |"
        )
    lines.append("")

    lines.append("## 3) 最大回撤区间")
    daily_stats: dict[str, dict[str, Any]] = {}
    for gid in ["BASE_ATR", "RANK_ON"]:
        run_rec = next((x for x in runs if str(x.get("group_id")) == gid), None)
        if run_rec is None:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        daily_path = Path(str((run_rec.get("artifacts") or {}).get("portfolio_daily_csv", "")))
        if not daily_path.exists():
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        try:
            ddf = pd.read_csv(daily_path)
            stats = _portfolio_daily_series_stats(ddf)
            daily_stats[gid] = stats
            lines.append(
                f"- {gid}: max_dd_start={stats['max_dd_start']} | max_dd_end={stats['max_dd_end']} | max_dd={stats['max_dd'] * 100:.2f}%"
            )
        except Exception:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
    lines.append("")

    lines.append("## 4) 最差 10 个交易日")
    for gid in ["BASE_ATR", "RANK_ON"]:
        stats = daily_stats.get(gid)
        if not stats:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        worst = list(stats.get("worst_10_days") or [])[:10]
        if not worst:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        lines.append(
            f"- {gid}: " + ", ".join([f"{str(x.get('date'))}={float(x.get('daily_return') or 0.0) * 100:.2f}%" for x in worst])
        )
    lines.append("")

    delta_ret = _delta(rank.get("total_return"), base.get("total_return"))
    delta_dd = _delta(rank.get("max_drawdown"), base.get("max_drawdown"))
    delta_trades = _delta(rank.get("trades_count"), base.get("trades_count"))
    underfill = float(rank.get("avg_positions", 0.0) or 0.0) < 3.0
    lines.extend(
        [
            "## 5) 一句话结论",
            (
                f"- RANK_ON 相对 BASE_ATR：收益变化={_fmt_pct(delta_ret)}，回撤变化={_fmt_pct(delta_dd)}，"
                f"交易频率变化={_fmt_num(delta_trades, 0)} 笔，持仓不足风险={('是' if underfill else '否')}。"
            ),
            f"- 是否建议进入下一步（walk-forward/参数扫描）：{('建议' if ((delta_ret or 0.0) >= -0.001 and (delta_dd or 0.0) >= 0) else '暂不建议')}",
            "",
            "## 6) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(rank.get('precompute_seconds'), 3)}",
            f"- simulation_seconds: {_fmt_num(rank.get('simulation_seconds'), 3)}",
            f"- io_write_seconds: {_fmt_num(rank.get('io_write_seconds'), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "- speedup_vs_slow: N/A（本次未运行 slow 引擎）",
            "",
        ]
    )
    return "\n".join(lines)


def _build_max_position_expand_report(
    *,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    universe_limit: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    groups = ["MAXPOS_10", "MAXPOS_20", "MAXPOS_30"]
    lines: list[str] = [
        "# MAX_POSITION_EXPAND_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- benchmark: {benchmark}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 三组核心对照表",
            "| group | max_positions | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | cash_utilization_avg | mean_order_value_scale |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in groups:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {int(r.get('max_positions', 0) or 0)} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | "
            f"{_fmt_pct(r.get('max_drawdown'))} | {_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | "
            f"{_fmt_pct(r.get('max_daily_drawdown'))} | {int(r.get('trades_count', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | "
            f"{_fmt_pct(r.get('turnover_avg'))} | {_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | "
            f"{_fmt_pct(r.get('trigger_when_slots_small_rate'))} | {_fmt_pct(r.get('cash_utilization_avg'))} | {_fmt_num(r.get('mean_order_value_scale'), 3)} |"
        )
    lines.append("")

    lines.append("## 3) 资金利用率诊断")
    best_cagr_gid = None
    best_cagr_val = None
    for gid in groups:
        try:
            cagr = float(by_gid.get(gid, {}).get("cagr", -1e9) or -1e9)
        except Exception:
            cagr = -1e9
        if best_cagr_val is None or cagr > best_cagr_val:
            best_cagr_val = cagr
            best_cagr_gid = gid
    for gid in groups:
        r = by_gid.get(gid, {})
        max_pos = float(r.get("max_positions", 0.0) or 0.0)
        avg_pos = float(r.get("avg_positions", 0.0) or 0.0)
        occ = (avg_pos / max_pos) if max_pos > 0 else 0.0
        lines.append(f"- {gid}: 持仓占用率 avg_positions/max_positions = {_fmt_pct(occ)}")
    if best_cagr_gid is None:
        lines.append("- CAGR 最高组: N/A")
        lines.append("- 结论：数据不足，无法判断扩容效果。")
    else:
        lines.append(f"- CAGR 最高组: {best_cagr_gid}（{_fmt_pct(best_cagr_val)}）")
        low_occ = all(
            (float(by_gid.get(g, {}).get("avg_positions", 0.0) or 0.0) / max(float(by_gid.get(g, {}).get("max_positions", 0.0) or 1.0), 1.0))
            < 0.5
            for g in groups
        )
        if low_occ:
            lines.append("- 结论：扩容后 CAGR 提升不明显，可能受 candidates/slots 或 base_order_value 约束。")
        else:
            lines.append("- 结论：提高 max_positions 对资金利用率有帮助，是否显著增厚 CAGR 需结合回撤共同评估。")
    lines.append("")

    lines.extend(
        [
            "## 4) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('precompute_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- simulation_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('simulation_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- io_write_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('io_write_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_strong_only_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    groups = ["BASE_TIER", "STRONG_ONLY", "STRONG_ONLY_1P8"]
    lines: list[str] = [
        "# STRONG_ONLY_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- benchmark: {benchmark}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]
    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 2) 核心对照表",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | STRONG_TREND_BUY_RATE | WEAK_TREND_BUY_RATE | BLOCK_WEAK_TIER_DISABLED |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in groups:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | {_fmt_pct(r.get('trigger_when_slots_small_rate'))} | "
            f"{_fmt_num(r.get('mean_order_value_scale'), 3)} | {_fmt_pct(r.get('STRONG_TREND_BUY_RATE'))} | {_fmt_pct(r.get('WEAK_TREND_BUY_RATE'))} | "
            f"{int(r.get('BLOCK_WEAK_TIER_DISABLED', 0) or 0)} |"
        )
    lines.append("")

    base = by_gid.get("BASE_TIER", {})
    strong = by_gid.get("STRONG_ONLY", {})
    d_cagr = _delta(strong.get("cagr"), base.get("cagr"))
    d_pos = _delta(strong.get("avg_positions"), base.get("avg_positions"))
    lines.extend(
        [
            "## 3) 一句话结论",
            f"- STRONG_ONLY 相对 BASE_TIER：CAGR变化={_fmt_pct(d_cagr)}，avg_positions变化={_fmt_num(d_pos, 2)}。",
            (
                "- 结论："
                + ("CAGR 提升明显。" if (d_cagr or 0.0) > 0 else "CAGR 未提升。")
                + (
                    " 如收益下降，可能需要放宽 entry_buffer 或提高 strong_scale/改动量确认。"
                    if (d_cagr or 0.0) <= 0
                    else ""
                )
            ),
            "",
            "## 4) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('precompute_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- simulation_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('simulation_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- io_write_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('io_write_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            "",
        ]
    )
    return "\n".join(lines)


def _portfolio_daily_series_stats(daily_df: pd.DataFrame) -> dict[str, Any]:
    if daily_df is None or daily_df.empty:
        raise ValueError("MISSING_DAILY_SERIES")
    if "date" not in daily_df.columns:
        raise ValueError("MISSING_DAILY_SERIES")
    nav = pd.to_numeric(daily_df.get("nav"), errors="coerce")
    if nav.isna().all():
        raise ValueError("MISSING_DAILY_SERIES")
    work = daily_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["nav"] = nav
    work = work.dropna(subset=["date", "nav"]).sort_values("date").reset_index(drop=True)
    if work.empty:
        raise ValueError("MISSING_DAILY_SERIES")

    if "daily_return" in work.columns:
        daily_return = pd.to_numeric(work["daily_return"], errors="coerce")
    else:
        daily_return = work["nav"].pct_change()
    work["daily_return"] = daily_return.fillna(0.0)

    if "drawdown" in work.columns:
        drawdown = pd.to_numeric(work["drawdown"], errors="coerce")
    else:
        peak = work["nav"].cummax()
        drawdown = work["nav"] / peak - 1.0
    work["drawdown"] = drawdown.fillna(0.0)

    end_idx = int(work["drawdown"].idxmin())
    start_idx = int(work["nav"].iloc[: end_idx + 1].idxmax())
    worst = work.nsmallest(10, "daily_return")[["date", "daily_return"]].copy()
    worst["date"] = worst["date"].dt.strftime("%Y-%m-%d")
    return {
        "max_dd": float(work.loc[end_idx, "drawdown"]),
        "max_dd_start": str(work.loc[start_idx, "date"].date()),
        "max_dd_end": str(work.loc[end_idx, "date"].date()),
        "worst_10_days": worst.to_dict(orient="records"),
    }


def _build_portfolio_validate_report(
    *,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int | None,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("BASE", {})
    atr = by_gid.get("ATR_VB", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    lines: list[str] = []
    lines.append("# ATR Portfolio Validate v1")
    lines.append("")
    lines.append("## 1) 运行信息")
    lines.append(f"- date_range: {start_s} ~ {end_s}")
    lines.append(f"- universe_limit: {universe_limit}")
    lines.append(f"- universe_raw_count: {raw_count}")
    lines.append(f"- universe_filtered_count: {filtered_count}")
    lines.append(f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}")
    lines.append(f"- initial_cash: {initial_capital:.0f}")
    lines.append(f"- max_positions: {max_positions}")
    try:
        start_dt = datetime.strptime(start_s, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_s, "%Y-%m-%d").date()
        if (end_dt - start_dt).days > 365 * 4:
            lines.append("- WARNING: LONG_RANGE_TEST")
    except Exception:
        pass
    lines.append("")

    lines.append("## 2) BASE vs ATR_VB 对照表")
    lines.append("| Group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for gid in ["BASE", "ATR_VB"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0)):.3f} | {_fmt_pct(r.get('volatility'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0)):.2f} | {_fmt_pct(r.get('turnover_avg'))} |"
        )
    lines.append("")

    lines.append("## 3) 最大回撤区间对比")
    daily_stats: dict[str, dict[str, Any]] = {}
    for gid in ["BASE", "ATR_VB"]:
        run_rec = next((x for x in runs if str(x.get("group_id")) == gid), None)
        if run_rec is None:
            continue
        daily_path = Path(str((run_rec.get("artifacts") or {}).get("portfolio_daily_csv", "")))
        if not daily_path.exists():
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        ddf = pd.read_csv(daily_path)
        try:
            stats = _portfolio_daily_series_stats(ddf)
            daily_stats[gid] = stats
        except Exception:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        lines.append(
            f"- {gid}: max_dd_start={stats['max_dd_start']} | max_dd_end={stats['max_dd_end']} | "
            f"max_dd={stats['max_dd'] * 100:.2f}%"
        )
    lines.append("")

    lines.append("## 4) 最差 10 个交易日对比")
    for gid in ["BASE", "ATR_VB"]:
        stats = daily_stats.get(gid)
        if not stats:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        worst = stats["worst_10_days"]
        if not worst:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        worst = worst[:10]
        if len(worst) < 5:
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        if all(abs(float(x.get("daily_return") or 0.0)) < 1e-12 for x in worst):
            lines.append(f"- {gid}: ERROR: MISSING_DAILY_SERIES")
            continue
        lines.append(
            f"- {gid}: "
            + ", ".join([f"{str(x.get('date'))}={float(x.get('daily_return') or 0.0) * 100:.2f}%" for x in worst])
        )
    lines.append("")

    delta_dd = _delta(atr.get("max_drawdown"), base.get("max_drawdown"))
    delta_ret = _delta(atr.get("total_return"), base.get("total_return"))
    worthwhile = "是" if (delta_dd is not None and delta_dd < 0 and (delta_ret is None or delta_ret >= -0.002)) else "否"

    lines.append("## 5) 一句话结论")
    lines.append(
        f"- ATR_VB 相对 BASE：回撤变化 {(_fmt_pct(delta_dd) if delta_dd is not None else '-')}, "
        f"收益变化 {(_fmt_pct(delta_ret) if delta_ret is not None else '-')}, 是否值得默认启用：{worthwhile}"
    )
    if float(base.get("avg_positions", 0.0)) < 3.0 or float(atr.get("avg_positions", 0.0)) < 3.0:
        lines.append("- 提示：信号过少（avg_positions < 3），本轮统计稳定性有限。")
    lines.append("")
    lines.append("## 6) 强度排序审计")
    for gid in ["BASE", "ATR_VB"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"- {gid}: strength_rank_enabled={bool(r.get('strength_rank_enabled', False))}, "
            f"strength_ma={int(r.get('strength_ma', 120) or 120)}, "
            f"avg_candidates_per_day={float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f}, "
            f"not_in_topk_reject_count={int(r.get('not_in_topk_reject_count', 0) or 0)}"
        )
    lines.append("")
    return "\n".join(lines)


def _build_portfolio_fill_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    initial_capital: float,
    universe_meta: dict[str, Any],
    excluded: dict[str, int],
) -> str:
    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    ok_rows = [r for r in rows if str(r.get("status", "")).upper() == "OK"]
    lines: list[str] = [
        "# Portfolio Fill Validate v1（持仓密度实验）",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- symbols_source: {str(universe_meta.get('symbols_source', 'db_index_constituents'))}",
        f"- constituents_load_status: {str(universe_meta.get('constituents_load_status', 'UNKNOWN'))}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        "",
        "## 2) 四组对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | universe_filtered_count |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["F1", "F2", "F3", "F4"]:
        r = next((x for x in rows if str(x.get("group_id")) == gid), {})
        if str(r.get("status", "")).upper() != "OK":
            lines.append(f"| {gid} | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR |")
            continue
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0)):.3f} | {_fmt_pct(r.get('volatility'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0)):.2f} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{int(r.get('universe_filtered_count', 0) or 0)} |"
        )
    lines.extend(
        [
            "",
            "## 3) avg_positions 对比",
            "| group | avg_positions | max_positions | fill_ratio |",
            "|---|---:|---:|---:|",
        ]
    )
    for gid in ["F1", "F2", "F3", "F4"]:
        r = next((x for x in rows if str(x.get("group_id")) == gid), {})
        avg_pos = float(r.get("avg_positions", 0.0) or 0.0)
        max_pos = float(r.get("max_positions", 0.0) or 0.0)
        fill_ratio = (avg_pos / max_pos) if max_pos > 0 else 0.0
        lines.append(f"| {gid} | {avg_pos:.2f} | {max_pos:.0f} | {_fmt_pct(fill_ratio)} |")
        if max_pos > 0 and avg_pos < max_pos * 0.5:
            lines.append(f"- WARNING: UNDERFILLED_PORTFOLIO（{gid}）")

    best_ret = max(ok_rows, key=lambda x: float(x.get("total_return", -1e9)), default=None)
    best_dd = min(ok_rows, key=lambda x: float(x.get("max_drawdown", 1e9)), default=None)
    lines.extend(["", "## 4) 一句话结论"])
    if not ok_rows:
        lines.append("- 本次无有效分组结果（请检查数据与参数）。")
    else:
        density_improved = "是" if (best_ret and float(best_ret.get("avg_positions", 0.0)) >= 5.0) else "否"
        ret_msg = f"收益最优为 {str(best_ret.get('group_id'))}（{_fmt_pct(best_ret.get('total_return'))}）" if best_ret else "收益无明显改善"
        dd_msg = f"回撤最优为 {str(best_dd.get('group_id'))}（{_fmt_pct(best_dd.get('max_drawdown'))}）" if best_dd else "回撤无明显改善"
        lines.append(f"- 持仓密度是否提升：{density_improved}；{ret_msg}；{dd_msg}。")
    lines.extend(["", "## 5) 强度排序审计"])
    for gid in ["F1", "F2", "F3", "F4"]:
        r = next((x for x in rows if str(x.get("group_id")) == gid), {})
        lines.append(
            f"- {gid}: strength_rank_enabled={bool(r.get('strength_rank_enabled', False))}, "
            f"strength_ma={int(r.get('strength_ma', 120) or 120)}, "
            f"avg_candidates_per_day={float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f}, "
            f"not_in_topk_reject_count={int(r.get('not_in_topk_reject_count', 0) or 0)}"
        )
    lines.append("")
    return "\n".join(lines)


def _build_trend_filter_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("BASE_ATR", {})
    on = by_gid.get("TREND_FILTER_ON", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    lines: list[str] = [
        "# TREND_FILTER_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- min_list_days: 120",
        f"- min_avg_turnover_20d: 100000000",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "",
        "## 2) 两组核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | universe_filtered_count | blocked_by_trend_count | trend_filter_block_rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["BASE_ATR", "TREND_FILTER_ON"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0)):.3f} | {_fmt_pct(r.get('vol_annual', r.get('volatility')))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0)):.2f} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{int(r.get('universe_filtered_count', 0) or 0)} | {int(r.get('blocked_by_trend_count', 0) or 0)} | {_fmt_pct(r.get('trend_filter_block_rate'))} |"
        )

    lines.extend(
        [
            "",
            "## 3) BUY 被趋势过滤次数统计",
            f"- BASE_ATR blocked_by_trend_count: {int(base.get('blocked_by_trend_count', 0) or 0)}",
            f"- TREND_FILTER_ON blocked_by_trend_count: {int(on.get('blocked_by_trend_count', 0) or 0)}",
            f"- TREND_FILTER_ON trend_filter_block_rate: {_fmt_pct(on.get('trend_filter_block_rate'))}",
            "",
            "## 4) avg_positions 对比",
            f"- BASE_ATR: {float(base.get('avg_positions', 0.0) or 0.0):.2f}",
            f"- TREND_FILTER_ON: {float(on.get('avg_positions', 0.0) or 0.0):.2f}",
        ]
    )
    if float(on.get("avg_positions", 0.0) or 0.0) < 3.0:
        lines.append("- WARNING: OVER_FILTERED_SYSTEM")

    delta_dd = _delta(on.get("max_drawdown"), base.get("max_drawdown"))
    delta_ret = _delta(on.get("total_return"), base.get("total_return"))
    trades_drop = _delta(on.get("trades_count"), base.get("trades_count"))
    lines.extend(
        [
            "",
            "## 5) 一句话结论",
            (
                f"- 趋势过滤是否降低回撤：{('是' if (delta_dd is not None and delta_dd < 0) else '否')}；"
                f"是否提升收益：{('是' if (delta_ret is not None and delta_ret > 0) else '否')}；"
                f"是否显著降低交易频率：{('是' if (trades_drop is not None and trades_drop < 0) else '否')}；"
                f"是否导致组合持仓不足：{('是' if float(on.get('avg_positions', 0.0) or 0.0) < 3.0 else '否')}。"
            ),
            "",
        ]
    )
    lines.append("## 6) 强度排序审计")
    for gid in ["BASE_ATR", "TREND_FILTER_ON"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"- {gid}: strength_rank_enabled={bool(r.get('strength_rank_enabled', False))}, "
            f"strength_ma={int(r.get('strength_ma', 120) or 120)}, "
            f"avg_candidates_per_day={float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f}, "
            f"not_in_topk_reject_count={int(r.get('not_in_topk_reject_count', 0) or 0)}"
        )
    lines.append("")
    return "\n".join(lines)


def _build_strength_rank_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("BASE", {})
    on = by_gid.get("RANK_ON", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    lines: list[str] = [
        "# STRENGTH_RANK_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- min_list_days: 120",
        f"- min_avg_turnover_20d: 100000000",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "",
        "## 2) 两组核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | universe_filtered_count | not_in_topk_reject_count | avg_candidates_per_day |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["BASE", "RANK_ON"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0)):.3f} | {_fmt_pct(r.get('vol_annual', r.get('volatility')))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0)):.2f} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{int(r.get('universe_filtered_count', 0) or 0)} | {int(r.get('not_in_topk_reject_count', 0) or 0)} | "
            f"{float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f} |"
        )

    delta_avg_pos = _delta(on.get("avg_positions"), base.get("avg_positions"))
    delta_dd = _delta(on.get("max_drawdown"), base.get("max_drawdown"))
    delta_ret = _delta(on.get("total_return"), base.get("total_return"))
    lines.extend(
        [
            "",
            "## 3) 一句话结论",
            (
                f"- RANK_ON 相对 BASE：avg_positions变化={('+' if (delta_avg_pos or 0) >= 0 else '')}{float(delta_avg_pos or 0):.2f}，"
                f"回撤变化={_fmt_pct(delta_dd)}，收益变化={_fmt_pct(delta_ret)}。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _build_trend_tier_validate_report(
    *,
    rows: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("BASE_HARD_TREND", {})
    tier = by_gid.get("TIER_ON", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    lines: list[str] = [
        "# TREND_TIER_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "- engine_mode: VECTOR_FAST_STRICT",
        "",
        "## 2) 两组核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | trades | avg_positions | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | WEAK_TREND_BUY_RATE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["BASE_HARD_TREND", "TIER_ON"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0) or 0.0):.3f} | {_fmt_pct(r.get('vol_annual', r.get('volatility')))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0) or 0.0):.2f} | "
            f"{float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f} | {float(r.get('avg_buys_per_day', 0.0) or 0.0):.2f} | "
            f"{_fmt_pct(r.get('trigger_when_slots_small_rate'))} | {float(r.get('mean_order_value_scale', 0.0) or 0.0):.3f} | "
            f"{_fmt_pct(r.get('WEAK_TREND_BUY_RATE'))} |"
        )
    lines.append("")
    d_pos = _delta(tier.get("avg_positions"), base.get("avg_positions"))
    d_ret = _delta(tier.get("total_return"), base.get("total_return"))
    d_mdd = _delta(tier.get("max_drawdown"), base.get("max_drawdown"))
    lines.extend(
        [
            "## 3) 一句话结论",
            (
                f"- TIER_ON 相对 BASE_HARD_TREND：候选/持仓供给"
                f"{('提升' if (d_pos is not None and d_pos > 0) else '未提升')}，"
                f"收益变化={_fmt_pct(d_ret)}，回撤变化={_fmt_pct(d_mdd)}，"
                f"是否建议继续：{('建议' if (d_pos is not None and d_pos > 0 and (d_mdd or 0.0) >= -0.02) else '观察')}"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _extract_max_drawdown_span(daily_csv: Path) -> dict[str, Any]:
    if not daily_csv.exists():
        return {"max_dd": None, "max_dd_start": None, "max_dd_end": None, "worst_10_days": []}
    try:
        ddf = pd.read_csv(daily_csv)
        return _portfolio_daily_series_stats(ddf)
    except Exception:
        return {"max_dd": None, "max_dd_start": None, "max_dd_end": None, "worst_10_days": []}


def _render_worst_days(daily_csv: Path, top_n: int = 10) -> list[str]:
    stats = _extract_max_drawdown_span(daily_csv)
    worst = list(stats.get("worst_10_days") or [])[: int(max(top_n, 1))]
    if not worst:
        return ["- ERROR: MISSING_DAILY_SERIES"]
    if len(worst) < 5:
        return ["- ERROR: MISSING_DAILY_SERIES"]
    if all(abs(float(x.get("daily_return") or 0.0)) < 1e-12 for x in worst):
        return ["- ERROR: MISSING_DAILY_SERIES"]
    return [
        "- " + ", ".join([f"{str(x.get('date'))}={float(x.get('daily_return') or 0.0) * 100:.2f}%" for x in worst])
    ]


def _build_trend_accel_brake_validate_report(
    *,
    rows: list[dict[str, Any]],
    run_root: Path,
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}
    base = by_gid.get("TIER_BASE", {})
    accel = by_gid.get("ACCEL_ONLY", {})
    brake = by_gid.get("ACCEL_BRAKE", {})

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    reason_order = ["RISK_OFF_EXIT", "TREND_BREAK_EXIT", "ATR_STOP_LOSS", "ATR_TRAIL_EXIT", "SMA_CROSS_DOWN"]
    reason_by_group: dict[str, dict[str, int]] = {}
    for gid in ["TIER_BASE", "ACCEL_ONLY", "ACCEL_BRAKE"]:
        trades_path = run_root / gid / "portfolio_trades.csv"
        counts = {k: 0 for k in reason_order}
        if trades_path.exists():
            try:
                tdf = pd.read_csv(trades_path)
                if not tdf.empty and "side" in tdf.columns and "reason_code" in tdf.columns:
                    s = tdf[tdf["side"].astype(str).str.upper() == "SELL"]["reason_code"].astype(str).str.upper()
                    vc = s.value_counts()
                    for k in reason_order:
                        counts[k] = int(vc.get(k, 0))
            except Exception:
                pass
        reason_by_group[gid] = counts

    def _perf_of(r: dict[str, Any], k: str) -> float:
        try:
            return float(r.get(k, 0.0) or 0.0)
        except Exception:
            return 0.0

    perf_db = max(_perf_of(base, "perf_db_load_seconds"), _perf_of(accel, "perf_db_load_seconds"), _perf_of(brake, "perf_db_load_seconds"))
    perf_pre = max(_perf_of(base, "perf_precompute_seconds"), _perf_of(accel, "perf_precompute_seconds"), _perf_of(brake, "perf_precompute_seconds"))
    perf_sim = max(_perf_of(base, "perf_simulation_seconds"), _perf_of(accel, "perf_simulation_seconds"), _perf_of(brake, "perf_simulation_seconds"))
    perf_io = max(_perf_of(base, "perf_io_write_seconds"), _perf_of(accel, "perf_io_write_seconds"), _perf_of(brake, "perf_io_write_seconds"))
    perf_q = int(max(_perf_of(base, "perf_db_queries"), _perf_of(accel, "perf_db_queries"), _perf_of(brake, "perf_db_queries")))
    perf_hit = max(_perf_of(base, "perf_cache_hit_rate"), _perf_of(accel, "perf_cache_hit_rate"), _perf_of(brake, "perf_cache_hit_rate"))

    lines: list[str] = [
        "# TREND_ACCEL_BRAKE_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "- engine_mode: VECTOR_FAST_STRICT",
        "",
        "## 2) 三组核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | WEAK_TREND_BUY_RATE | STRONG_TREND_BUY_RATE | TREND_BREAK_EXIT_RATE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["TIER_BASE", "ACCEL_ONLY", "ACCEL_BRAKE"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0) or 0.0):.3f} | {_fmt_pct(r.get('vol_annual', r.get('volatility')))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0) or 0.0):.2f} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f} | {float(r.get('avg_buys_per_day', 0.0) or 0.0):.2f} | "
            f"{_fmt_pct(r.get('trigger_when_slots_small_rate'))} | {float(r.get('mean_order_value_scale', 0.0) or 0.0):.3f} | "
            f"{_fmt_pct(r.get('WEAK_TREND_BUY_RATE'))} | {_fmt_pct(r.get('STRONG_TREND_BUY_RATE'))} | {_fmt_pct(r.get('TREND_BREAK_EXIT_RATE'))} |"
        )

    lines.extend(
        [
            "",
            "## 3) 最大回撤区间",
        ]
    )
    for gid in ["TIER_BASE", "ACCEL_ONLY", "ACCEL_BRAKE"]:
        dd = _extract_max_drawdown_span(run_root / gid / "portfolio_daily.csv")
        lines.append(
            f"- {gid}: start={dd.get('max_dd_start') or '-'} end={dd.get('max_dd_end') or '-'} max_dd={_fmt_pct(dd.get('max_dd'))}"
        )

    lines.extend(["", "## 4) 最差10个交易日"])
    for gid in ["TIER_BASE", "ACCEL_ONLY", "ACCEL_BRAKE"]:
        lines.append(f"### {gid}")
        lines.extend(_render_worst_days(run_root / gid / "portfolio_daily.csv", top_n=10))
        lines.append("")

    lines.extend(["## 5) 一句话结论（短期收益优先）"])
    d_accel_ret = _delta(accel.get("total_return"), base.get("total_return"))
    d_accel_mdd = _delta(accel.get("max_drawdown"), base.get("max_drawdown"))
    d_brake_ret = _delta(brake.get("total_return"), base.get("total_return"))
    d_brake_mdd = _delta(brake.get("max_drawdown"), base.get("max_drawdown"))
    lines.append(
        f"- ACCEL_ONLY vs TIER_BASE：收益变化={_fmt_pct(d_accel_ret)}，回撤变化={_fmt_pct(d_accel_mdd)}。"
    )
    lines.append(
        f"- ACCEL_BRAKE vs TIER_BASE：收益变化={_fmt_pct(d_brake_ret)}，回撤变化={_fmt_pct(d_brake_mdd)}。"
    )
    if (d_brake_ret or 0.0) >= 0 and (d_brake_mdd or 0.0) >= (d_accel_mdd or -1.0):
        lines.append("- 结论：加速+刹车在短期收益提升同时对回撤有控制，优先考虑 ACCEL_BRAKE。")
    else:
        lines.append("- 结论：加速收益与风控存在权衡，建议继续微调强度或退出优先级。")

    lines.extend(["", "## 6) reason_code 分布（SELL）"])
    lines.append("| group | RISK_OFF_EXIT | TREND_BREAK_EXIT | ATR_STOP_LOSS | ATR_TRAIL_EXIT | SMA_CROSS_DOWN |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for gid in ["TIER_BASE", "ACCEL_ONLY", "ACCEL_BRAKE"]:
        rc = reason_by_group.get(gid, {})
        lines.append(
            f"| {gid} | {int(rc.get('RISK_OFF_EXIT', 0))} | {int(rc.get('TREND_BREAK_EXIT', 0))} | "
            f"{int(rc.get('ATR_STOP_LOSS', 0))} | {int(rc.get('ATR_TRAIL_EXIT', 0))} | {int(rc.get('SMA_CROSS_DOWN', 0))} |"
        )

    lines.extend(
        [
            "",
            "## 7) 性能信息",
            f"- db_load_seconds: {perf_db:.3f}",
            f"- precompute_seconds: {perf_pre:.3f}",
            f"- simulation_seconds: {perf_sim:.3f}",
            f"- io_write_seconds: {perf_io:.3f}",
            f"- db_queries: {perf_q}",
            f"- cache_hit_rate: {perf_hit:.2%}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_trend_brake_confirm_validate_report(
    *,
    rows: list[dict[str, Any]],
    run_root: Path,
    start_s: str,
    end_s: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
) -> str:
    by_gid = {str(r.get("group_id")): r for r in rows}

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    reason_order = ["RISK_OFF_EXIT", "TREND_BREAK_EXIT", "ATR_STOP_LOSS", "ATR_TRAIL_EXIT", "SMA_CROSS_DOWN"]
    reason_by_group: dict[str, dict[str, int]] = {}
    for gid in ["ACCEL_BRAKE_IMMEDIATE", "ACCEL_BRAKE_CONFIRM_3D", "ACCEL_BRAKE_CONFIRM_3D_GAP"]:
        trades_path = run_root / gid / "portfolio_trades.csv"
        counts = {k: 0 for k in reason_order}
        if trades_path.exists():
            try:
                tdf = pd.read_csv(trades_path)
                if not tdf.empty and "side" in tdf.columns and "reason_code" in tdf.columns:
                    s = tdf[tdf["side"].astype(str).str.upper() == "SELL"]["reason_code"].astype(str).str.upper()
                    vc = s.value_counts()
                    for k in reason_order:
                        counts[k] = int(vc.get(k, 0))
            except Exception:
                pass
        reason_by_group[gid] = counts

    lines: list[str] = [
        "# TREND_BRAKE_CONFIRM_VALIDATE_v1",
        "",
        "## 1) 运行信息",
        f"- date_range: {start_s} ~ {end_s}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        "- engine_mode: VECTOR_FAST_STRICT",
        "",
        "## 2) 核心对照表",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | TREND_BREAK_EXIT_COUNT | TREND_BREAK_EXIT_RATE | mean_break_streak_at_exit | days_held_median |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid in ["ACCEL_BRAKE_IMMEDIATE", "ACCEL_BRAKE_CONFIRM_3D", "ACCEL_BRAKE_CONFIRM_3D_GAP"]:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{float(r.get('sharpe', 0.0) or 0.0):.3f} | {_fmt_pct(r.get('vol_annual', r.get('volatility')))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {float(r.get('avg_positions', 0.0) or 0.0):.2f} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{float(r.get('avg_candidates_per_day', 0.0) or 0.0):.2f} | {float(r.get('avg_buys_per_day', 0.0) or 0.0):.2f} | "
            f"{_fmt_pct(r.get('trigger_when_slots_small_rate'))} | {int(r.get('TREND_BREAK_EXIT_COUNT', 0) or 0)} | "
            f"{_fmt_pct(r.get('TREND_BREAK_EXIT_RATE'))} | {float(r.get('mean_break_streak_at_exit', 0.0) or 0.0):.2f} | "
            f"{float(r.get('days_held_median', 0.0) or 0.0):.2f} |"
        )

    lines.extend(["", "## 3) 最大回撤区间"])
    for gid in ["ACCEL_BRAKE_IMMEDIATE", "ACCEL_BRAKE_CONFIRM_3D", "ACCEL_BRAKE_CONFIRM_3D_GAP"]:
        dd = _extract_max_drawdown_span(run_root / gid / "portfolio_daily.csv")
        lines.append(f"- {gid}: start={dd.get('max_dd_start') or '-'} end={dd.get('max_dd_end') or '-'} max_dd={_fmt_pct(dd.get('max_dd'))}")

    lines.extend(["", "## 4) 最差10个交易日"])
    for gid in ["ACCEL_BRAKE_IMMEDIATE", "ACCEL_BRAKE_CONFIRM_3D", "ACCEL_BRAKE_CONFIRM_3D_GAP"]:
        lines.append(f"### {gid}")
        lines.extend(_render_worst_days(run_root / gid / "portfolio_daily.csv", top_n=10))
        lines.append("")

    lines.extend(["## 5) reason_code 分布（SELL）"])
    lines.append("| group | RISK_OFF_EXIT | TREND_BREAK_EXIT | ATR_STOP_LOSS | ATR_TRAIL_EXIT | SMA_CROSS_DOWN |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for gid in ["ACCEL_BRAKE_IMMEDIATE", "ACCEL_BRAKE_CONFIRM_3D", "ACCEL_BRAKE_CONFIRM_3D_GAP"]:
        rc = reason_by_group.get(gid, {})
        lines.append(
            f"| {gid} | {int(rc.get('RISK_OFF_EXIT', 0))} | {int(rc.get('TREND_BREAK_EXIT', 0))} | "
            f"{int(rc.get('ATR_STOP_LOSS', 0))} | {int(rc.get('ATR_TRAIL_EXIT', 0))} | {int(rc.get('SMA_CROSS_DOWN', 0))} |"
        )

    imm = by_gid.get("ACCEL_BRAKE_IMMEDIATE", {})
    c3 = by_gid.get("ACCEL_BRAKE_CONFIRM_3D", {})
    d_trades = _delta(c3.get("trades_count"), imm.get("trades_count"))
    d_turn = _delta(c3.get("turnover_avg"), imm.get("turnover_avg"))
    d_mdd_day = _delta(c3.get("max_daily_drawdown"), imm.get("max_daily_drawdown"))
    d_ret = _delta(c3.get("total_return"), imm.get("total_return"))
    lines.extend(
        [
            "",
            "## 6) 一句话结论",
            f"- CONFIRM_3D 相对 IMMEDIATE：trades变化={float(d_trades or 0.0):.0f}，turnover变化={_fmt_pct(d_turn)}，max_daily_drawdown变化={_fmt_pct(d_mdd_day)}，收益变化={_fmt_pct(d_ret)}。",
            "- 若交易/换手显著下降且单日回撤改善，则确认型破位退出更适合短期收益与风险平衡。",
            "",
        ]
    )
    perf_db = max(float(by_gid.get(k, {}).get("perf_db_load_seconds", 0.0) or 0.0) for k in by_gid.keys()) if by_gid else 0.0
    perf_pre = max(float(by_gid.get(k, {}).get("perf_precompute_seconds", 0.0) or 0.0) for k in by_gid.keys()) if by_gid else 0.0
    perf_sim = max(float(by_gid.get(k, {}).get("perf_simulation_seconds", 0.0) or 0.0) for k in by_gid.keys()) if by_gid else 0.0
    perf_io = max(float(by_gid.get(k, {}).get("perf_io_write_seconds", 0.0) or 0.0) for k in by_gid.keys()) if by_gid else 0.0
    perf_q = int(max(float(by_gid.get(k, {}).get("perf_db_queries", 0.0) or 0.0) for k in by_gid.keys())) if by_gid else 0
    perf_hit = max(float(by_gid.get(k, {}).get("perf_cache_hit_rate", 0.0) or 0.0) for k in by_gid.keys()) if by_gid else 0.0
    lines.extend(
        [
            "## 7) 性能信息",
            f"- db_load_seconds: {perf_db:.3f}",
            f"- precompute_seconds: {perf_pre:.3f}",
            f"- simulation_seconds: {perf_sim:.3f}",
            f"- io_write_seconds: {perf_io:.3f}",
            f"- db_queries: {perf_q}",
            f"- cache_hit_rate: {perf_hit:.2%}",
            "",
        ]
    )
    return "\n".join(lines)


def _build_strength_rank_validate_v2_report(
    *,
    rows: list[dict[str, Any]],
    details_df: pd.DataFrame,
    regimes: list[dict[str, Any]],
    wf_folds: list[dict[str, Any]],
    full_start: str,
    full_end: str,
    universe_limit: int,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_too_small: bool,
    fast_mode: bool = False,
    warning_label: str = "RANK_RARELY_TRIGGERED",
    skipped_wf: list[str] | None = None,
    perf: dict[str, Any] | None = None,
) -> str:
    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _get(seg: str, name: str, gid: str) -> dict[str, Any]:
        if details_df.empty:
            return {}
        hit = details_df[(details_df["segment"] == seg) & (details_df["slice_name"] == name) & (details_df["group_id"] == gid)]
        return hit.iloc[0].to_dict() if not hit.empty else {}

    all_base = _get("ALL", "ALL", "BASE")
    all_rank = _get("ALL", "ALL", "RANK_ON")
    regime_lines: list[str] = []
    wf_lines: list[str] = []
    regime_win = 0
    wf_win = 0
    regime_total = len(regimes)
    wf_total = len(wf_folds)
    wf_delta_ret_vals: list[float] = []
    wf_delta_mdd_vals: list[float] = []

    lines: list[str] = [
        "# STRENGTH_RANK_VALIDATE_v2",
        "",
        "## 1) 运行信息",
        f"- full_range: {full_start} ~ {full_end}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- fast_mode: {str(fast_mode).lower()}",
        "- 策略参数固定：sma_cross_regime_atr + trend_filter + ATR(0.8/2.0)，仅对照 enable_strength_rank",
        "",
        "## 2) 全区间对照（BASE vs RANK_ON）",
        "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | trades | avg_positions | turnover_avg | not_in_topk | avg_candidates/day | avg_buy_slots/day | slots<=1_rate | rank_trigger_days | rank_trigger_rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid, src in [("BASE", all_base), ("RANK_ON", all_rank)]:
        lines.append(
            f"| {gid} | {_fmt_pct(src.get('total_return'))} | {_fmt_pct(src.get('cagr'))} | {_fmt_pct(src.get('max_drawdown'))} | "
            f"{float(src.get('sharpe', 0.0) or 0.0):.3f} | {_fmt_pct(src.get('vol_annual'))} | {int(src.get('trades', 0) or 0)} | "
            f"{float(src.get('avg_positions', 0.0) or 0.0):.2f} | {_fmt_pct(src.get('turnover_avg'))} | "
            f"{int(src.get('not_in_topk_reject_count', 0) or 0)} | {float(src.get('avg_candidates_per_day', 0.0) or 0.0):.2f} | "
            f"{float(src.get('avg_buy_slots_per_day', 0.0) or 0.0):.2f} | {_fmt_pct(src.get('trigger_when_slots_small_rate'))} | "
            f"{int(src.get('rank_trigger_days', 0) or 0)} | {_fmt_pct(src.get('rank_trigger_rate'))} |"
        )

    lines.extend(
        [
            "",
            "## 3) 多阶段结果表（P1/P2/P3）",
            "| stage | BASE_ret | RANK_ret | Δret | BASE_mdd | RANK_mdd | Δmdd | RANK_avg_pos | RANK_trigger_rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rg in regimes:
        name = str(rg.get("name", ""))
        b = _get("REGIME", name, "BASE")
        r = _get("REGIME", name, "RANK_ON")
        d_ret = _delta(r.get("total_return"), b.get("total_return"))
        d_mdd = _delta(r.get("max_drawdown"), b.get("max_drawdown"))
        if (d_ret or 0.0) > 0 and (d_mdd or 0.0) > 0:
            regime_win += 1
        regime_lines.append(f"{name}: Δret={_fmt_pct(d_ret)}, Δmdd={_fmt_pct(d_mdd)}")
        lines.append(
            f"| {name} | {_fmt_pct(b.get('total_return'))} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(d_ret)} | "
            f"{_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(r.get('max_drawdown'))} | {_fmt_pct(d_mdd)} | "
            f"{float(r.get('avg_positions', 0.0) or 0.0):.2f} | {_fmt_pct(r.get('rank_trigger_rate'))} |"
        )

    lines.extend(
        [
            "",
            "## 4) Walk-forward 结果表（仅 test 段，参数固定未调参）",
            "| fold | train_range | test_range | BASE_ret | RANK_ret | Δret | BASE_mdd | RANK_mdd | Δmdd | RANK_trigger_rate |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for fd in wf_folds:
        name = str(fd.get("name", ""))
        b = _get("WF_TEST", name, "BASE")
        r = _get("WF_TEST", name, "RANK_ON")
        d_ret = _delta(r.get("total_return"), b.get("total_return"))
        d_mdd = _delta(r.get("max_drawdown"), b.get("max_drawdown"))
        if d_ret is not None:
            wf_delta_ret_vals.append(float(d_ret))
        if d_mdd is not None:
            wf_delta_mdd_vals.append(float(d_mdd))
        if (d_ret or 0.0) > 0 and (d_mdd or 0.0) > 0:
            wf_win += 1
        wf_lines.append(f"{name}: Δret={_fmt_pct(d_ret)}, Δmdd={_fmt_pct(d_mdd)}")
        lines.append(
            f"| {name} | {fd.get('train_start')}~{fd.get('train_end')} | {fd.get('test_start')}~{fd.get('test_end')} | "
            f"{_fmt_pct(b.get('total_return'))} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(d_ret)} | "
            f"{_fmt_pct(b.get('max_drawdown'))} | {_fmt_pct(r.get('max_drawdown'))} | {_fmt_pct(d_mdd)} | {_fmt_pct(r.get('rank_trigger_rate'))} |"
        )
    if skipped_wf:
        lines.append("")
        lines.append(f"- SKIPPED_OUT_OF_RANGE folds: {', '.join(skipped_wf)}")

    wf_ret_med = float(pd.Series(wf_delta_ret_vals).median()) if wf_delta_ret_vals else 0.0
    wf_mdd_med = float(pd.Series(wf_delta_mdd_vals).median()) if wf_delta_mdd_vals else 0.0
    rank_trigger_rate_all = float(all_rank.get("rank_trigger_rate", 0.0) or 0.0)
    avg_candidates_all = float(all_rank.get("avg_candidates_per_day", 0.0) or 0.0)
    lines.extend(
        [
            "",
            "## 5) 稳定性结论",
            f"- RANK_ON 在 3 个阶段中胜出：{regime_win}/{regime_total}",
            f"- RANK_ON 在 3 个 WF test 中胜出：{wf_win}/{wf_total}",
            f"- WF test 的 Δret 中位数：{_fmt_pct(wf_ret_med)}",
            f"- WF test 的 Δmdd 中位数：{_fmt_pct(wf_mdd_med)}（回撤为负数，Δmdd>0 代表回撤改善）",
            f"- 全区间 rank_trigger_rate：{_fmt_pct(rank_trigger_rate_all)}",
            f"- 全区间 avg_candidates_per_day：{avg_candidates_all:.2f}",
        ]
    )
    if universe_too_small:
        lines.append("- UNIVERSE_TOO_SMALL：本次结果无效，仅供调试。")
    if rank_trigger_rate_all < 0.10:
        lines.append(f"- WARNING: {warning_label}（触发率低于10%，排序效果评估可信度不足）")
        lines.append("- 触发率目标达成：未达标（目标 >= 10%）")
    else:
        lines.append("- 触发率目标达成：已达标（>= 10%）")
    if perf:
        cache_total = int(perf.get("cache_total", 0) or 0)
        cache_hit = int(perf.get("cache_hit", 0) or 0)
        cache_hit_rate = (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0
        lines.extend(
            [
                "",
                "## 6) 性能信息",
                f"- db_load_seconds: {float(perf.get('db_load_seconds', 0.0)):.3f}",
                f"- feature_calc_seconds: {float(perf.get('feature_calc_seconds', 0.0)):.3f}",
                f"- simulation_seconds: {float(perf.get('simulation_seconds', 0.0)):.3f}",
                f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
                f"- cache_hit_rate: {cache_hit_rate:.2%}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _run_engine_perf_compare_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = ENGINE_PERF_COMPARE_REPORT_FILE,
) -> dict[str, Any]:
    variants = preset_variants("ENGINE_PERF_COMPARE_v1")
    ov = dict(overrides or {})
    ov.setdefault("fast_mode", True)
    ov.setdefault("start_date", "2025-02-01")
    ov.setdefault("end_date", "2026-02-01")
    ov.setdefault("universe_limit", 300)
    ov.setdefault("initial_capital", 1_000_000.0)
    ov.setdefault("max_positions", 10)
    ov.setdefault("min_avg_turnover_20d", 5e7)
    debug_first_divergence = bool(ov.get("debug_first_divergence", True))

    symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = Path(__file__).resolve().parents[2] / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_dump_path = debug_dir / "first_divergence_dump.json"
    if debug_dump_path.exists():
        debug_dump_path.unlink()
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov["start_date"])
    end_s = str(ov["end_date"])
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov["initial_capital"])
    max_positions = int(ov["max_positions"])
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov["universe_limit"])
    min_avg_turnover_20d = float(ov["min_avg_turnover_20d"])

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = symbols
    tmp_cfg = exp_dir / "_engine_perf_compare_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )

    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf_common: dict[str, Any] = {"db_queries": 0, "cache_total": 0, "cache_hit": 0}
    _emit_progress(progress_hook, 12, "加载Universe与行情数据")
    t_db = time.perf_counter()
    raw_symbols, _ = resolve_universe_symbols(
        as_of=cfg_obj.universe.end,
        db_loader=lambda as_of, lim: load_constituents_from_db(
            db=cfg_obj.db,
            runtime=cfg_obj.db_runtime,
            as_of=as_of,
            universe_limit=lim,
        ),
        user_symbols=symbols,
        allow_user_override=bool(ov.get("universe_use_user_override", False)),
        universe_limit=universe_limit,
    )
    ohlcv_all = _load_ohlcv_with_cache(provider, data_cache, raw_symbols, cfg_obj.universe.start, cfg_obj.universe.end, perf_common)
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(
        cands,
        UniverseFilterConfig(min_avg_turnover_20d=min_avg_turnover_20d),
    )
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).tolist() if not filtered.empty else []
    perf_common["db_queries"] = int(perf_common.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    db_load_seconds = float(time.perf_counter() - t_db)

    hist = ohlcv_all.copy()
    hist["date"] = pd.to_datetime(hist["date"])
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    hist = hist[hist["symbol"].isin(filtered_symbols)]
    bench = benchmark_df.copy()
    bench["date"] = pd.to_datetime(bench["date"])
    days = [d.date() for d in calendar]

    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    mode_results: dict[str, dict[str, Any]] = {}
    if hist.empty:
        err = "UNIVERSE_FILTER_EMPTY"
        rows = [
            {"group_id": "SLOW_AUDIT", "status": "FAILED", "error_message": err, "error": err},
            {"group_id": "VECTOR_FAST_STRICT", "status": "FAILED", "error_message": err, "error": err},
        ]
        report_dir = Path(__file__).resolve().parents[3] / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / report_filename
        report_path.write_text(
            "\n".join(
                [
                    "# ENGINE_PERF_COMPARE_v1",
                    "",
                    "## 结果",
                    f"- ERROR: {err}",
                    f"- date_range: {start_s} ~ {end_s}",
                    f"- universe_limit: {universe_limit}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": runs,
            "single_report_path": str(report_path),
            "equivalence_pass": False,
        }
    _emit_progress(progress_hook, 20, "执行慢引擎与向量引擎对比")
    for idx, v in enumerate(variants, start=1):
        mode = str(((v.get("portfolio") or {}).get("engine_mode", "SLOW_AUDIT"))).upper()
        group_id = str(v.get("group_id", mode))
        strategy_def = dict(v.get("strategy", {}) or {})
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=dict(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        _emit_progress(progress_hook, 20 + int(idx * 55 / max(len(variants), 1)), f"{group_id} 运行中")
        t_sim = time.perf_counter()
        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=bench,
            trading_days=days,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=True,
            strength_ma=120,
            strength_top_k_mode="FILL_TO_MAX",
            history_lookback_days=int(ov.get("history_lookback_days", 260)),
            engine_mode=mode,
            fast_mode=bool(ov.get("fast_mode", True)),
            debug_first_divergence=debug_first_divergence,
        )
        sim_seconds = float(time.perf_counter() - t_sim)
        run_id = f"run_{group_id.lower()}_{exp_id[:8]}"
        run_dir = _results_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)
        m = dict(result.metrics)
        mode_results[group_id] = {
            "wall_time_seconds": sim_seconds,
            "db_load_seconds": db_load_seconds if idx == 1 else 0.0,
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "feature_calc_seconds": float(m.get("feature_calc_seconds", m.get("precompute_seconds", 0.0)) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", sim_seconds) or sim_seconds),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
            "db_queries": int(perf_common.get("db_queries", 0)),
            "cache_hit_rate": (float(perf_common.get("cache_hit", 0)) / float(max(1, perf_common.get("cache_total", 0)))),
            "notes": str(m.get("notes", "")),
            "metrics": m,
            "daily": result.daily,
        }
        rows.append(
            {
                "group_id": group_id,
                "strategy_name": str(strategy_cfg.name),
                "status": "OK",
                "run_id": run_id,
                "total_return": float(m.get("total_return", 0.0)),
                "max_drawdown": float(m.get("max_drawdown", 0.0)),
                "sharpe": float(m.get("sharpe", 0.0)),
                "trades": int(m.get("trades", 0)),
                "trades_count": int(m.get("trades", 0)),
                "avg_positions": float(m.get("avg_positions", 0.0)),
                "rank_trigger_rate": float(m.get("rank_trigger_rate", 0.0)),
                "duration_ms": int(sim_seconds * 1000),
                "universe_filtered_count": int(len(filtered_symbols)),
                "max_positions": max_positions,
            }
        )
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv"),
                },
                "diff": {"portfolio": {"engine_mode": mode}},
            }
        )

    slow = mode_results.get("SLOW_AUDIT", {})
    vec = mode_results.get("VECTOR_FAST_STRICT", {})
    sm = dict(slow.get("metrics", {}))
    vm = dict(vec.get("metrics", {}))
    diff_values = {
        "delta_return": abs(float(vm.get("total_return", 0.0)) - float(sm.get("total_return", 0.0))),
        "delta_mdd": abs(float(vm.get("max_drawdown", 0.0)) - float(sm.get("max_drawdown", 0.0))),
        "delta_trades": abs(int(vm.get("trades", 0)) - int(sm.get("trades", 0))),
        "delta_avg_positions": abs(float(vm.get("avg_positions", 0.0)) - float(sm.get("avg_positions", 0.0))),
        "delta_trigger_rate": abs(float(vm.get("rank_trigger_rate", 0.0)) - float(sm.get("rank_trigger_rate", 0.0))),
    }
    trades_tol = max(2, int(round(int(sm.get("trades", 0)) * 0.01)))
    checks = {
        "return": diff_values["delta_return"] <= 0.002,
        "mdd": diff_values["delta_mdd"] <= 0.002,
        "trades": diff_values["delta_trades"] <= trades_tol,
        "avg_positions": diff_values["delta_avg_positions"] <= 0.1,
        "trigger_rate": diff_values["delta_trigger_rate"] <= 0.005,
    }
    overall_pass = all(checks.values())
    diff_summary: list[str] = []
    ds = slow.get("daily", pd.DataFrame())
    dv = vec.get("daily", pd.DataFrame())
    if isinstance(ds, pd.DataFrame) and isinstance(dv, pd.DataFrame) and (not ds.empty) and (not dv.empty):
        ms = ds[["date", "nav", "positions_count", "turnover_pct"]].copy()
        mv = dv[["date", "nav", "positions_count", "turnover_pct"]].copy()
        merged = ms.merge(mv, on="date", how="inner", suffixes=("_slow", "_vec"))
        if not merged.empty:
            merged["nav_diff_pct"] = (
                (pd.to_numeric(merged["nav_vec"], errors="coerce") - pd.to_numeric(merged["nav_slow"], errors="coerce")).abs()
                / pd.to_numeric(merged["nav_slow"], errors="coerce").replace(0, pd.NA)
            ).fillna(0.0)
            merged["pos_diff"] = (
                pd.to_numeric(merged["positions_count_vec"], errors="coerce")
                - pd.to_numeric(merged["positions_count_slow"], errors="coerce")
            ).abs()
            merged["to_diff"] = (
                pd.to_numeric(merged["turnover_pct_vec"], errors="coerce")
                - pd.to_numeric(merged["turnover_pct_slow"], errors="coerce")
            ).abs()
            bad = merged[(merged["nav_diff_pct"] > 0.002) | (merged["pos_diff"] > 0.1) | (merged["to_diff"] > 0.005)].head(10)
            for row in bad.itertuples(index=False):
                diff_summary.append(
                    f"- {row.date}: nav_diff={float(row.nav_diff_pct):.4%}, pos_diff={float(row.pos_diff):.2f}, turnover_diff={float(row.to_diff):.4%}"
                )

    lines = [
        f"# {str(preset)}",
        "",
        "## 1) 引擎耗时对比",
        "| 引擎 | wall_time_seconds | db_load_seconds | precompute_seconds | simulation_seconds | io_write_seconds | db_queries | cache_hit_rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for gid, rec in [("SLOW_AUDIT", slow), ("VECTOR_FAST_STRICT", vec)]:
        lines.append(
            f"| {gid} | {float(rec.get('wall_time_seconds', 0.0)):.3f} | {float(rec.get('db_load_seconds', 0.0)):.3f} | "
            f"{float(rec.get('precompute_seconds', rec.get('feature_calc_seconds', 0.0))):.3f} | {float(rec.get('simulation_seconds', 0.0)):.3f} | "
            f"{float(rec.get('io_write_seconds', 0.0)):.3f} | {int(rec.get('db_queries', 0))} | {float(rec.get('cache_hit_rate', 0.0)):.2%} |"
        )
    lines.extend(
        [
            "",
            "### 性能说明",
            f"- SLOW notes: {str(slow.get('notes', '') or '-')} ",
            f"- VECTOR notes: {str(vec.get('notes', '') or '-')} ",
        ]
    )
    lines.extend(
        [
            "",
            "## 2) 关键指标对比",
            "| 指标 | SLOW_AUDIT | VECTOR_FAST_STRICT | 绝对差值 |",
            "|---|---:|---:|---:|",
            f"| total_return | {float(sm.get('total_return', 0.0)):.6f} | {float(vm.get('total_return', 0.0)):.6f} | {float(diff_values['delta_return']):.6f} |",
            f"| max_drawdown | {float(sm.get('max_drawdown', 0.0)):.6f} | {float(vm.get('max_drawdown', 0.0)):.6f} | {float(diff_values['delta_mdd']):.6f} |",
            f"| sharpe | {float(sm.get('sharpe', 0.0)):.6f} | {float(vm.get('sharpe', 0.0)):.6f} | {abs(float(vm.get('sharpe',0.0))-float(sm.get('sharpe',0.0))):.6f} |",
            f"| trades | {int(sm.get('trades', 0))} | {int(vm.get('trades', 0))} | {int(diff_values['delta_trades'])} |",
            f"| avg_positions | {float(sm.get('avg_positions', 0.0)):.6f} | {float(vm.get('avg_positions', 0.0)):.6f} | {float(diff_values['delta_avg_positions']):.6f} |",
            f"| rank_trigger_rate | {float(sm.get('rank_trigger_rate', 0.0)):.6f} | {float(vm.get('rank_trigger_rate', 0.0)):.6f} | {float(diff_values['delta_trigger_rate']):.6f} |",
            "",
            "## 3) 阈值判定（PASS/FAIL）",
            f"- return: {'PASS' if checks['return'] else 'FAIL'} （|Δreturn|<=0.2%）",
            f"- mdd: {'PASS' if checks['mdd'] else 'FAIL'} （|Δmdd|<=0.2%）",
            f"- trades: {'PASS' if checks['trades'] else 'FAIL'} （Δtrades<=max(2,1%)）",
            f"- avg_positions: {'PASS' if checks['avg_positions'] else 'FAIL'} （|Δavg_positions|<=0.1）",
            f"- trigger_rate: {'PASS' if checks['trigger_rate'] else 'FAIL'} （|Δtrigger_rate|<=0.5%）",
            f"- 总体: {'PASS' if overall_pass else 'FAIL'}",
        ]
    )
    first_divergence_payload: dict[str, Any] | None = None
    if not overall_pass:
        lines.extend(["", "## 4) diff 摘要（前10条）"])
        lines.extend(diff_summary or ["- 无可用diff样本。"])
        if debug_first_divergence and isinstance(ds, pd.DataFrame) and isinstance(dv, pd.DataFrame) and (not ds.empty) and (not dv.empty):
            ms = ds[["date", "nav", "positions_count", "turnover_pct"]].copy()
            mv = dv[["date", "nav", "positions_count", "turnover_pct"]].copy()
            merged = ms.merge(mv, on="date", how="inner", suffixes=("_slow", "_vec"))
            if not merged.empty:
                merged["nav_diff_pct"] = (
                    (pd.to_numeric(merged["nav_vec"], errors="coerce") - pd.to_numeric(merged["nav_slow"], errors="coerce")).abs()
                    / pd.to_numeric(merged["nav_slow"], errors="coerce").replace(0, pd.NA)
                ).fillna(0.0)
                merged["pos_diff"] = (
                    pd.to_numeric(merged["positions_count_vec"], errors="coerce")
                    - pd.to_numeric(merged["positions_count_slow"], errors="coerce")
                ).abs()
                merged["to_diff"] = (
                    pd.to_numeric(merged["turnover_pct_vec"], errors="coerce")
                    - pd.to_numeric(merged["turnover_pct_slow"], errors="coerce")
                ).abs()
                bad = merged[
                    (merged["nav_diff_pct"] > 1e-9)
                    | (merged["pos_diff"] > 0)
                    | (merged["to_diff"] > 1e-12)
                ]
                if not bad.empty:
                    d0 = str(bad.iloc[0]["date"])
                    slow_trace = dict((slow.get("metrics") or {}).get("debug_trace", {}))
                    vec_trace = dict((vec.get("metrics") or {}).get("debug_trace", {}))
                    slow_day = dict(slow_trace.get(d0) or {})
                    vec_day = dict(vec_trace.get(d0) or {})
                    first_divergence_payload = {
                        "divergence_date": d0,
                        "slow": {
                            "date": d0,
                            "buy_slots": int(slow_day.get("buy_slots", 0) or 0),
                            "candidates_count": int(slow_day.get("buy_candidates_count", 0) or 0),
                            "held_symbols": dict(slow_day.get("held_symbols_before") or {}),
                            "cash_available": float(slow_day.get("cash_before", 0.0) or 0.0),
                            "enable_strength_rank": True,
                            "max_positions": max_positions,
                            "candidates_symbols": list(slow_day.get("buy_candidates_raw") or []),
                            "candidates_gate": [
                                {"symbol": sym, **dict((slow_day.get("buy_gate_by_symbol") or {}).get(sym) or {})}
                                for sym in list(slow_day.get("buy_candidates_raw") or [])
                            ],
                            "sorting_triggered": bool(slow_day.get("sorting_triggered", False)),
                            "sorted_selected_symbols": list(slow_day.get("sorted_selected_symbols") or []),
                        },
                        "vector": {
                            "date": d0,
                            "buy_slots": int(vec_day.get("buy_slots", 0) or 0),
                            "candidates_count": int(vec_day.get("buy_candidates_count", 0) or 0),
                            "held_symbols": dict(vec_day.get("held_symbols_before") or {}),
                            "cash_available": float(vec_day.get("cash_before", 0.0) or 0.0),
                            "enable_strength_rank": True,
                            "max_positions": max_positions,
                            "candidates_symbols": list(vec_day.get("buy_candidates_raw") or []),
                            "candidates_gate": [
                                {"symbol": sym, **dict((vec_day.get("buy_gate_by_symbol") or {}).get(sym) or {})}
                                for sym in list(vec_day.get("buy_candidates_raw") or [])
                            ],
                            "sorting_triggered": bool(vec_day.get("sorting_triggered", False)),
                            "sorted_selected_symbols": list(vec_day.get("sorted_selected_symbols") or []),
                        },
                    }
                    slow_sigs = list(slow_day.get("signal_details", []) or [])
                    vec_sigs = list(vec_day.get("signal_details", []) or [])
                    by_key = {}
                    for s in slow_sigs:
                        by_key[(str(s.get("symbol", "")), str(s.get("side", "")))] = {"slow": s}
                    for v in vec_sigs:
                        by_key.setdefault((str(v.get("symbol", "")), str(v.get("side", ""))), {})["vector"] = v
                    atr_div = None
                    for _, pair in by_key.items():
                        s = pair.get("slow") or {}
                        v = pair.get("vector") or {}
                        sr = str(s.get("reason_code", ""))
                        vr = str(v.get("reason_code", ""))
                        if sr != vr and ("ATR_STOP_LOSS" in sr or "ATR_STOP_LOSS" in vr):
                            atr_div = {"slow": s, "vector": v}
                            break
                    lines.extend(
                        [
                            "",
                            "### 首个分叉日调试",
                            f"- date: {d0}",
                            f"- slow.cash_before: {((slow_trace.get(d0) or {}).get('cash_before'))}",
                            f"- vector.cash_before: {((vec_trace.get(d0) or {}).get('cash_before'))}",
                            f"- slow.selected_symbols: {((slow_trace.get(d0) or {}).get('selected_symbols'))}",
                            f"- vector.selected_symbols: {((vec_trace.get(d0) or {}).get('selected_symbols'))}",
                        ]
                    )
                    slow_cands = sorted(set([str(x).lower() for x in list(slow_day.get("buy_candidates_raw", []) or [])]))
                    vec_cands = sorted(set([str(x).lower() for x in list(vec_day.get("buy_candidates_raw", []) or [])]))
                    only_slow = sorted(list(set(slow_cands) - set(vec_cands)))
                    only_vec = sorted(list(set(vec_cands) - set(slow_cands)))
                    slow_gates = dict(slow_day.get("buy_gate_by_symbol") or {})
                    vec_gates = dict(vec_day.get("buy_gate_by_symbol") or {})

                    def _fmt_gate(sym: str) -> str:
                        sg = dict(slow_gates.get(sym) or {})
                        vg = dict(vec_gates.get(sym) or {})
                        if not sg and not vg:
                            return f"- {sym}: gate=NA"
                        return (
                            f"- {sym}: "
                            f"slow[cross_up={sg.get('cross_up')}, entry_buffer_ok={sg.get('entry_buffer_ok')}, trend_ok={sg.get('trend_ok')}, regime_ok={sg.get('regime_ok')}, tradable={sg.get('tradable')}, ma_is_valid={sg.get('ma_is_valid')}, close={sg.get('close')}, slow_ma={sg.get('slow_ma')}] "
                            f"| vector[cross_up={vg.get('cross_up')}, entry_buffer_ok={vg.get('entry_buffer_ok')}, trend_ok={vg.get('trend_ok')}, regime_ok={vg.get('regime_ok')}, tradable={vg.get('tradable')}, ma_is_valid={vg.get('ma_is_valid')}, close={vg.get('close')}, slow_ma={vg.get('slow_ma')}]"
                        )

                    lines.extend(
                        [
                            "",
                            "#### FIRST_DIVERGENCE_CANDIDATES",
                            f"- earliest_diff_date: {d0}",
                            f"- slow_candidates_count: {len(slow_cands)}",
                            f"- vector_candidates_count: {len(vec_cands)}",
                            f"- only_in_slow(first20): {only_slow[:20]}",
                            f"- only_in_vector(first20): {only_vec[:20]}",
                        ]
                    )
                    if only_slow or only_vec:
                        lines.append("- gate_details:")
                        for s in only_slow[:20]:
                            lines.append(_fmt_gate(s))
                        for s in only_vec[:20]:
                            lines.append(_fmt_gate(s))
                    if atr_div is not None:
                        smeta = dict((atr_div.get("slow") or {}).get("meta") or {})
                        vmeta = dict((atr_div.get("vector") or {}).get("meta") or {})
                        lines.extend(
                            [
                                "",
                                "#### FIRST_DIVERGENCE_ATR_DEBUG",
                                f"- symbol: {atr_div.get('vector', {}).get('symbol') or atr_div.get('slow', {}).get('symbol')}",
                                f"- slow.sell_reason: {atr_div.get('slow', {}).get('reason_code')}",
                                f"- vector.sell_reason: {atr_div.get('vector', {}).get('reason_code')}",
                                f"- slow.entry_price: {smeta.get('entry_price')} | vector.entry_price: {vmeta.get('entry_price')}",
                                f"- slow.peak_close: {smeta.get('peak_close')} | vector.peak_close: {vmeta.get('peak_close')}",
                                f"- slow.close[t]: {smeta.get('close')} | vector.close[t]: {vmeta.get('close')}",
                                f"- slow.atr[t]: {smeta.get('atr_last')} | vector.atr[t]: {vmeta.get('atr_last')}",
                                f"- atr_stop_k: slow={smeta.get('atr_stop_k', 0.8)} vector={vmeta.get('atr_stop_k', 0.8)}",
                                f"- slow.stop_price: {smeta.get('stop_price')} | vector.stop_price: {vmeta.get('stop_price')}",
                                f"- slow.trail_price: {smeta.get('trail_price')} | vector.trail_price: {vmeta.get('trail_price')}",
                            ]
                        )

    report_dir = Path(__file__).resolve().parents[3] / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / report_filename
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    if first_divergence_payload is not None:
        (exp_dir / "first_divergence.json").write_text(
            json.dumps(first_divergence_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        debug_dump_path.write_text(
            json.dumps(first_divergence_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        debug_dump_path.write_text(
            json.dumps(
                {
                    "status": "no_divergence",
                    "equivalence_pass": bool(overall_pass),
                    "exp_id": exp_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    zip_path = _write_experiment_zip(exp_id, exp_dir, runs)
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "zip_path": str(zip_path),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(report_path),
        "equivalence_pass": overall_pass,
    }


def _write_outputs(
    exp_dir: Path,
    exp_id: str,
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    df = pd.DataFrame(rows)
    for col in MATRIX_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[MATRIX_COLUMNS]
    df.to_csv(exp_dir / "matrix.csv", index=False)
    (exp_dir / "matrix.json").write_text(
        json.dumps({"exp_id": exp_id, "rows": df.to_dict(orient="records")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (exp_dir / "runs.json").write_text(
        json.dumps(
            {
                "exp_id": exp_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "base_config_path": str(base_path),
                "base_config": base_cfg,
                "preset": preset,
                "runs": runs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_scan_artifacts(exp_dir: Path, rows: list[dict[str, Any]]) -> None:
    matrix = exp_dir / "matrix.csv"
    errs = validate_matrix(matrix)
    (exp_dir / "validate_scan_result.json").write_text(
        json.dumps({"ok": len(errs) == 0, "errors": errs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_validate_script(exp_dir)
    _write_effectiveness_report(exp_dir, rows)


def _write_baseline_ref(exp_dir: Path, preset: str) -> dict[str, Any]:
    baseline = _find_latest_baseline_row()
    payload = {
        "preset": preset,
        "baseline_missing": baseline is None,
        "baseline_run_id": None,
        "baseline_path": None,
        "baseline_metrics": {},
    }
    if baseline is not None:
        payload["baseline_run_id"] = baseline.get("run_id")
        rid = str(baseline.get("run_id", ""))
        payload["baseline_path"] = str(_results_root() / rid) if rid else None
        payload["baseline_metrics"] = {
            "total_return": _safe_num(baseline.get("total_return")),
            "max_drawdown": _safe_num(baseline.get("max_drawdown")),
            "sharpe": _safe_num(baseline.get("sharpe")),
            "trades_count": int(_safe_num(baseline.get("trades_count")) or 0),
            "closed_trades": int(_safe_num(baseline.get("closed_trades")) or 0),
            "atr_exit_rate": _safe_num(baseline.get("atr_exit_rate")) or 0.0,
            "score": _safe_num(baseline.get("score")) or 0.0,
        }
    (exp_dir / "baseline_ref.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _write_validate_v1_artifacts(exp_dir: Path, rows: list[dict[str, Any]], baseline_ref: dict[str, Any]) -> None:
    df = pd.DataFrame(rows)
    b = baseline_ref.get("baseline_metrics", {}) if isinstance(baseline_ref, dict) else {}
    lines: list[str] = []
    lines.append("# ATR Validate v1 Report")
    lines.append("")
    lines.append(f"- baseline_missing={baseline_ref.get('baseline_missing', True)}")
    if not baseline_ref.get("baseline_missing", True):
        lines.append(
            f"- baseline_run_id={baseline_ref.get('baseline_run_id')} "
            f"ret={b.get('total_return')} mdd={b.get('max_drawdown')} sharpe={b.get('sharpe')} "
            f"atr_exit_rate={b.get('atr_exit_rate')}"
        )
    lines.append("")
    for _, r in df.iterrows():
        d_ret = _delta(r.get("total_return"), b.get("total_return"))
        d_mdd = _delta(r.get("max_drawdown"), b.get("max_drawdown"))
        d_shp = _delta(r.get("sharpe"), b.get("sharpe"))
        d_exit = _delta(r.get("atr_exit_rate"), b.get("atr_exit_rate"))
        lines.append(
            "- "
            f"{r.get('group_id')} run={r.get('run_id')} mode={r.get('mode')} "
            f"ret={_fmt(r.get('total_return'))} mdd={_fmt(r.get('max_drawdown'))} sharpe={_fmt(r.get('sharpe'))} "
            f"atr_exit_rate={_fmt(r.get('atr_exit_rate'))} "
            f"d_ret={_fmt(d_ret)} d_mdd={_fmt(d_mdd)} d_sharpe={_fmt(d_shp)} d_exit={_fmt(d_exit)}"
        )
    lines.append("")
    lines.append("## Recommendation")
    lines.append("- default=B (V_B_S0.8_T2.0_M3); if unstable in WF, fallback C then D.")
    (exp_dir / "effectiveness_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_walkforward_report(exp_dir: Path, rows: list[dict[str, Any]], baseline_ref: dict[str, Any]) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(exp_dir / "wf_matrix.csv", index=False)
    b = baseline_ref.get("baseline_metrics", {}) if isinstance(baseline_ref, dict) else {}
    lines = ["# ATR Walk-Forward Report", ""]
    lines.append(f"- baseline_missing={baseline_ref.get('baseline_missing', True)}")
    lines.append("")
    if not df.empty:
        for fold, part in df.groupby("fold_id"):
            lines.append(f"## {fold} test")
            for _, r in part.iterrows():
                d_ret = _delta(r.get("total_return"), b.get("total_return"))
                d_mdd = _delta(r.get("max_drawdown"), b.get("max_drawdown"))
                lines.append(
                    "- "
                    f"{r.get('group_id')} ret={_fmt(r.get('total_return'))} mdd={_fmt(r.get('max_drawdown'))} "
                    f"score={_fmt(r.get('score'))} atr_exit_rate={_fmt(r.get('atr_exit_rate'))} "
                    f"d_ret={_fmt(d_ret)} d_mdd={_fmt(d_mdd)}"
                )
            lines.append("")

    # Selection rule
    lines.append("## Selection Rule")
    pick = "D"
    validation_failed = False
    if not df.empty:
        g = {}
        for gid in ["V_B_S0.8_T2.0_M3", "V_C_S0.8_TNA_M1", "V_D_S1.0_T2.0_M3"]:
            part = df[df["group_id"] == gid]
            ok = 0
            for _, r in part.iterrows():
                d_mdd = _delta(r.get("max_drawdown"), b.get("max_drawdown"))
                d_ret = _delta(r.get("total_return"), b.get("total_return"))
                if d_mdd is not None and d_ret is not None and d_mdd <= -0.2 * float(b.get("max_drawdown", 0) or 0) and d_ret >= -0.005:
                    ok += 1
            g[gid] = ok
        if g.get("V_B_S0.8_T2.0_M3", 0) >= 2:
            pick = "B"
        elif g.get("V_C_S0.8_TNA_M1", 0) >= 2:
            pick = "C"
        elif g.get("V_D_S1.0_T2.0_M3", 0) >= 2:
            pick = "D"
        else:
            validation_failed = True
    lines.append(f"- pick={pick}, validation_failed={validation_failed}")
    (exp_dir / "walkforward_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_universe_vote_report(
    exp_dir: Path,
    vote_rows: list[dict[str, Any]],
    excluded: dict[str, int],
    raw_count: int,
    filtered_count: int,
    baseline_ref: dict[str, Any],
) -> None:
    df = pd.DataFrame(vote_rows)
    lines = ["# Universe Vote Report", ""]
    lines.append(f"- baseline_missing={baseline_ref.get('baseline_missing', True)}")
    lines.append(f"- universe raw_count={raw_count}, filtered_count={filtered_count}")
    lines.append(f"- excluded_reason_top5={json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}")
    lines.append("")
    if not df.empty:
        for gid, part in df.groupby("group_id"):
            pass_rate = float(part["pass"].astype(bool).mean()) if "pass" in part.columns else 0.0
            d_mdd = pd.to_numeric(part["delta_mdd"], errors="coerce")
            d_ret = pd.to_numeric(part["delta_return"], errors="coerce")
            lines.append(
                f"- {gid}: pass_rate={pass_rate:.4f}, "
                f"median_d_mdd={d_mdd.median():.6f}, p10_d_mdd={d_mdd.quantile(0.1):.6f}, p90_d_mdd={d_mdd.quantile(0.9):.6f}, "
                f"median_d_ret={d_ret.median():.6f}, p10_d_ret={d_ret.quantile(0.1):.6f}, p90_d_ret={d_ret.quantile(0.9):.6f}"
            )
    lines.append("")
    lines.append("- if pass_rate <= 0.6: focus on structure/filter/position sizing, not more k tuning.")
    (exp_dir / "universe_vote_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _write_validate_script(exp_dir: Path) -> None:
    script = (
        "from backtest_v2.tools.validate_scan import main\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    (exp_dir / "validate_scan.py").write_text(script, encoding="utf-8")


def _write_effectiveness_report(exp_dir: Path, rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows)
    baseline = _find_latest_baseline_row()
    lines: list[str] = []
    lines.append("# ATR Param Scan v1 Effectiveness Report")
    lines.append("")
    lines.append(f"- exp_dir: `{exp_dir}`")
    lines.append("")
    if baseline is None:
        lines.append("- baseline_missing=true (latest ATR_RISK_2x2 OFF/OFF not found)")
    else:
        lines.append("- baseline_missing=false")
        lines.append(
            "- baseline: "
            f"run_id={baseline.get('run_id')} "
            f"ret={baseline.get('total_return')} "
            f"mdd={baseline.get('max_drawdown')} "
            f"sharpe={baseline.get('sharpe')} "
            f"trades={baseline.get('trades_count')} "
            f"closed={baseline.get('closed_trades')} "
            f"atr_exit_rate={baseline.get('atr_exit_rate')} "
            f"score={baseline.get('score')}"
        )
    lines.append("")

    ok = df[df["status"].astype(str) == "OK"].copy() if "status" in df.columns else df.copy()
    for col in ["score", "max_drawdown", "atr_exit_rate", "total_return", "sharpe", "trades_count", "closed_trades"]:
        if col in ok.columns:
            ok[col] = pd.to_numeric(ok[col], errors="coerce")

    def _top(title: str, table: pd.DataFrame) -> None:
        lines.append(f"## {title}")
        if table.empty:
            lines.append("- no rows")
            lines.append("")
            return
        head = table.head(10)
        for _, r in head.iterrows():
            d_ret = _delta(r.get("total_return"), baseline.get("total_return") if baseline else None)
            d_mdd = _delta(r.get("max_drawdown"), baseline.get("max_drawdown") if baseline else None)
            d_exit = _delta(r.get("atr_exit_rate"), baseline.get("atr_exit_rate") if baseline else None)
            lines.append(
                "- "
                f"{r.get('group_id')} run={r.get('run_id')} mode={r.get('mode')} "
                f"stop={r.get('atr_stop_k')} trail={r.get('atr_trail_k')} "
                f"ret={_fmt(r.get('total_return'))} mdd={_fmt(r.get('max_drawdown'))} "
                f"score={_fmt(r.get('score'))} atr_exit_rate={_fmt(r.get('atr_exit_rate'))} "
                f"stop_cnt={int(r.get('atr_stop_loss_count') or 0)} trail_cnt={int(r.get('atr_trail_exit_count') or 0)} "
                f"d_ret={_fmt(d_ret)} d_mdd={_fmt(d_mdd)} d_exit={_fmt(d_exit)}"
            )
        lines.append("")

    _top("Top 10 by score", ok.sort_values(["score"], ascending=False))
    _top("Top 10 by min max_drawdown", ok.sort_values(["max_drawdown"], ascending=True))
    _top("Top 10 by atr_exit_rate", ok.sort_values(["atr_exit_rate"], ascending=False))

    lines.append("## Recommended Range")
    rec = _recommend_range(ok, baseline)
    lines.extend([f"- {x}" for x in rec])
    lines.append("")
    lines.append("- validate_scan.py: `backtest_v2/tools/validate_scan.py`")
    lines.append("- per-exp validator entry: `validate_scan.py --matrix matrix.csv`")
    lines.append("- priority note: when stop+trail both hit on same bar, stop wins (ATR_STOP_LOSS).")
    (exp_dir / "effectiveness_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _find_latest_baseline_row() -> dict[str, Any] | None:
    exp_root = _results_root() / "experiments"
    if not exp_root.exists():
        return None
    dirs = [p for p in exp_root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        runs = _load_json(d / "runs.json")
        if str(runs.get("preset", "")).upper() != "ATR_RISK_2X2":
            continue
        m = d / "matrix.csv"
        if not m.exists():
            continue
        try:
            df = pd.read_csv(m)
        except Exception:
            continue
        if "params_json" not in df.columns:
            continue
        for _, r in df.iterrows():
            try:
                p = json.loads(str(r.get("params_json", "{}")))
            except Exception:
                p = {}
            if (not bool(p.get("enable_atr_stop", False))) and (not bool(p.get("enable_atr_trail", False))):
                row = r.to_dict()
                row["atr_exit_rate"] = float(row.get("atr_exit_rate", 0.0) or 0.0)
                row["score"] = float(row.get("score", 0.0) or 0.0)
                return row
    return None


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):.6f}"
    except Exception:
        return "NA"


def _delta(v: Any, b: Any) -> float | None:
    try:
        if b is None:
            return None
        return float(v) - float(b)
    except Exception:
        return None


def _recommend_range(df: pd.DataFrame, baseline: dict[str, Any] | None) -> list[str]:
    if df.empty:
        return ["no data"]
    top = df.sort_values(["score"], ascending=False).head(10).copy()
    stop_vals = [x for x in top["atr_stop_k"].astype(str).tolist() if x != "NA"]
    trail_vals = [x for x in top["atr_trail_k"].astype(str).tolist() if x != "NA"]
    mode_top = top["mode"].astype(str).value_counts().to_dict()
    stop_range = f"[{min(stop_vals)}, {max(stop_vals)}]" if stop_vals else "[NA, NA]"
    trail_range = f"[{min(trail_vals)}, {max(trail_vals)}]" if trail_vals else "[NA, NA]"
    mode_best = max(mode_top, key=mode_top.get) if mode_top else "M1"
    out = [
        f"candidate stop_k range: {stop_range}",
        f"candidate trail_k range: {trail_range}",
        f"suggested mode: {mode_best}",
    ]
    if baseline is not None:
        best = top.iloc[0]
        out.append(
            "vs baseline: "
            f"d_return={_fmt(_delta(best.get('total_return'), baseline.get('total_return')))} "
            f"d_max_dd={_fmt(_delta(best.get('max_drawdown'), baseline.get('max_drawdown')))} "
            f"d_atr_exit_rate={_fmt(_delta(best.get('atr_exit_rate', 0.0), baseline.get('atr_exit_rate', 0.0)))}"
        )
    return out


def _write_experiment_zip(exp_id: str, exp_dir: Path, runs: list[dict[str, Any]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = exp_dir.parent / f"{stamp}_{exp_id}.zip"
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        _zip_tree(zf, exp_dir, arc_prefix=exp_dir.name)
        for run in runs:
            run_id = str(run.get("run_id", "")).strip()
            if not run_id:
                continue
            run_dir = _results_root() / run_id
            if run_dir.exists():
                _zip_tree(zf, run_dir, arc_prefix=run_dir.name)
    return zip_path


def _zip_tree(zf: zipfile.ZipFile, base_dir: Path, arc_prefix: str) -> None:
    for p in sorted(base_dir.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(base_dir)
        zf.write(p, arcname=str(Path(arc_prefix) / rel))


def _parse_overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.symbols:
        out["symbols"] = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest experiment matrix")
    parser.add_argument("--config", required=True, help="base config path")
    parser.add_argument("--preset", default="G1-G4", help="variant preset id")
    parser.add_argument("--symbols", default="", help="comma-separated override symbols")
    args = parser.parse_args()

    result = run_experiment(args.config, args.preset, _parse_overrides(args))
    print(f"exp_id={result['exp_id']}")
    print(f"results_dir={result['results_dir']}")
    for row in result["matrix_head"]:
        print(
            f"{row['group_id']} {row['status']} run_id={row['run_id']} "
            f"ret={row.get('total_return')} mdd={row.get('max_drawdown')} filled_rate={row.get('filled_rate')}"
        )


def _build_multifactor_validate_report(
    *,
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    start_s: str,
    end_s: str,
    benchmark: str,
    raw_count: int,
    filtered_count: int,
    excluded: dict[str, int],
    initial_capital: float,
    max_positions: int,
    universe_limit: int,
    base_order_value: float,
    lot_mode: str,
    perf: dict[str, Any],
    error: str | None,
) -> str:
    """Generate detailed multifactor validation report similar to TREND_BRAKE_CONFIRM style."""
    by_gid = {str(r.get("group_id")): r for r in rows}

    def _fmt_pct(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v) * 100:.2f}%"
        except Exception:
            return "-"

    def _fmt_num(v: Any, nd: int = 4) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "-"

    # Extract group IDs from rows
    groups = [str(r.get("group_id")) for r in rows if r.get("group_id")]

    lines: list[str] = [
        "# MULTIFACTOR_VALIDATE",
        "",
        "## 1) 运行信息",
        f"- timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- engine_mode: VECTOR_FAST_STRICT",
        f"- date_range: {start_s} ~ {end_s}",
        "- universe_rules: HS300+CSI500, exclude_st=true, exclude_suspended=true, min_list_days=120, min_avg_turnover_20d=1e8",
        f"- benchmark: {benchmark}",
        f"- universe_limit: {universe_limit}",
        f"- universe_raw_count: {raw_count}",
        f"- universe_filtered_count: {filtered_count}",
        f"- excluded_reason_top5: {json.dumps(dict(list(excluded.items())[:5]), ensure_ascii=False)}",
        f"- initial_cash: {initial_capital:.0f}",
        f"- max_positions: {max_positions}",
        f"- base_order_value: {base_order_value:.0f}",
        f"- lot_mode: {lot_mode}",
        "",
    ]

    if error:
        lines.extend([f"- ERROR: {error}", ""])
        return "\n".join(lines)

    # Build comprehensive comparison table (17 columns like TREND_BRAKE_CONFIRM)
    lines.extend(
        [
            "## 2) 核心对照表",
            "| group | total_return | cagr | max_drawdown | sharpe | vol_annual | max_daily_drawdown | trades | avg_positions | turnover_avg | avg_candidates_per_day | avg_buys_per_day | slots<=1_rate | mean_order_value_scale | STRONG_TREND_BUY_RATE | WEAK_TREND_BUY_RATE | BLOCK_WEAK_TIER_DISABLED |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for gid in groups:
        r = by_gid.get(gid, {})
        lines.append(
            f"| {gid} | {_fmt_pct(r.get('total_return'))} | {_fmt_pct(r.get('cagr'))} | {_fmt_pct(r.get('max_drawdown'))} | "
            f"{_fmt_num(r.get('sharpe'), 3)} | {_fmt_pct(r.get('vol_annual'))} | {_fmt_pct(r.get('max_daily_drawdown'))} | "
            f"{int(r.get('trades_count', 0) or 0)} | {_fmt_num(r.get('avg_positions'), 2)} | {_fmt_pct(r.get('turnover_avg'))} | "
            f"{_fmt_num(r.get('avg_candidates_per_day'), 2)} | {_fmt_num(r.get('avg_buys_per_day'), 2)} | {_fmt_pct(r.get('trigger_when_slots_small_rate'))} | "
            f"{_fmt_num(r.get('mean_order_value_scale'), 3)} | {_fmt_pct(r.get('STRONG_TREND_BUY_RATE'))} | {_fmt_pct(r.get('WEAK_TREND_BUY_RATE'))} | "
            f"{int(r.get('BLOCK_WEAK_TIER_DISABLED', 0) or 0)} |"
        )
    lines.append("")

    # Max drawdown intervals
    lines.append("## 3) 最大回撤区间")
    daily_stats: dict[str, dict[str, Any]] = {}
    for gid in groups:
        run_rec = next((x for x in runs if str(x.get("group_id")) == gid), None)
        if run_rec is None:
            lines.append(f"- {gid}: start=N/A end=N/A max_dd=N/A")
            continue
        daily_path = Path(str((run_rec.get("artifacts") or {}).get("portfolio_daily_csv", "")))
        if not daily_path.exists():
            lines.append(f"- {gid}: start=N/A end=N/A max_dd=N/A")
            continue
        try:
            ddf = pd.read_csv(daily_path)
            stats = _portfolio_daily_series_stats(ddf)
            daily_stats[gid] = stats
            lines.append(
                f"- {gid}: start={stats['max_dd_start']} end={stats['max_dd_end']} max_dd={_fmt_pct(stats['max_dd'])}"
            )
        except Exception:
            lines.append(f"- {gid}: start=N/A end=N/A max_dd=N/A")
    lines.append("")

    # Worst 10 trading days
    lines.append("## 4) 最差10个交易日")
    for gid in groups:
        stats = daily_stats.get(gid)
        if not stats:
            lines.append(f"### {gid}")
            lines.append("- 无数据")
            lines.append("")
            continue
        worst = list(stats.get("worst_10_days") or [])[:10]
        if not worst:
            lines.append(f"### {gid}")
            lines.append("- 无数据")
            lines.append("")
            continue
        lines.append(f"### {gid}")
        lines.append(
            "- " + ", ".join([f"{str(x.get('date'))}={_fmt_pct(x.get('daily_return'))}" for x in worst])
        )
        lines.append("")

    # Load trades data for reason_code analysis
    lines.append("## 5) reason_code 分布（SELL）")
    lines.append("| group | RISK_OFF_EXIT | TREND_BREAK_EXIT | ATR_STOP_LOSS | ATR_TRAIL_EXIT | SMA_CROSS_DOWN |")
    lines.append("|---|---:|---:|---:|---:|---:|")

    for gid in groups:
        run_rec = next((x for x in runs if str(x.get("group_id")) == gid), None)
        reason_counts = {"RISK_OFF_EXIT": 0, "TREND_BREAK_EXIT": 0, "ATR_STOP_LOSS": 0, "ATR_TRAIL_EXIT": 0, "SMA_CROSS_DOWN": 0}
        if run_rec is not None:
            trades_path = Path(str((run_rec.get("artifacts") or {}).get("portfolio_trades_csv", "")))
            if trades_path.exists():
                try:
                    trades_df = pd.read_csv(trades_path)
                    side_col = "side" if "side" in trades_df.columns else ("action" if "action" in trades_df.columns else "")
                    if side_col:
                        sell_trades = trades_df[trades_df[side_col].astype(str).str.upper() == "SELL"]
                    else:
                        sell_trades = pd.DataFrame()
                    for reason in reason_counts.keys():
                        if "reason_code" in sell_trades.columns:
                            reason_counts[reason] = int(sell_trades["reason_code"].astype(str).str.upper().eq(reason).sum())
                except Exception:
                    pass

        lines.append(
            f"| {gid} | {reason_counts['RISK_OFF_EXIT']} | {reason_counts['TREND_BREAK_EXIT']} | "
            f"{reason_counts['ATR_STOP_LOSS']} | {reason_counts['ATR_TRAIL_EXIT']} | {reason_counts['SMA_CROSS_DOWN']} |"
        )
    lines.append("")

    # Conclusion
    lines.append("## 6) 一句话结论")
    if len(groups) >= 2:
        baseline = by_gid.get(groups[0], {})
        multifactor = by_gid.get(groups[1], {})
        delta_ret = _delta(multifactor.get("total_return"), baseline.get("total_return"))
        delta_cagr = _delta(multifactor.get("cagr"), baseline.get("cagr"))
        delta_sharpe = _delta(multifactor.get("sharpe"), baseline.get("sharpe"))
        delta_trades = _delta(multifactor.get("trades_count"), baseline.get("trades_count"))
        delta_turnover = _delta(multifactor.get("turnover_avg"), baseline.get("turnover_avg"))
        delta_max_dd = _delta(multifactor.get("max_drawdown"), baseline.get("max_drawdown"))

        lines.append(
            f"- {groups[1]} 相对 {groups[0]}：total_return变化={_fmt_pct(delta_ret)}，CAGR变化={_fmt_pct(delta_cagr)}，"
            f"Sharpe变化={_fmt_num(delta_sharpe, 3)}，trades变化={_fmt_num(delta_trades, 0)}，"
            f"turnover变化={_fmt_pct(delta_turnover)}，max_drawdown变化={_fmt_pct(delta_max_dd)}。"
        )

        if delta_cagr and delta_cagr > 0 and delta_sharpe and delta_sharpe > 0:
            conclusion = "多因子融合显著改善了风险调整后收益，建议进一步优化因子权重和融合模式。"
        elif delta_cagr and delta_cagr > 0:
            conclusion = "多因子融合提升了收益，但Sharpe未显著改善，考虑调整风险控制参数。"
        else:
            conclusion = "多因子融合未带来明显改善，建议重新审视因子选择和权重配置。"
        lines.append(f"- 结论：{conclusion}")
    else:
        lines.append("- 分组不足，无法进行对比分析。")
    lines.append("")

    # Performance info
    total_wall_seconds = float(sum(float(r.get("duration_ms", 0) or 0) for r in rows) / 1000.0)
    lines.extend(
        [
            "## 7) 性能信息",
            f"- db_load_seconds: {_fmt_num(perf.get('db_load_seconds'), 3)}",
            f"- precompute_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('precompute_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- simulation_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('simulation_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- io_write_seconds: {_fmt_num(max([float(by_gid.get(g, {}).get('io_write_seconds', 0.0) or 0.0) for g in groups] or [0.0]), 3)}",
            f"- db_queries: {int(perf.get('db_queries', 0) or 0)}",
            f"- cache_hit_rate: {_fmt_pct(perf.get('cache_hit_rate'))}",
            f"- total_variant_wall_seconds: {_fmt_num(total_wall_seconds, 3)}",
            "",
        ]
    )
    return "\n".join(lines)


def _estimate_strategy_lookback_days(strategy: Any) -> int:
    """
    Estimate minimal useful history window for per-day signal generation.
    Used to cap composite generic-signal history slices in vector mode.
    """
    candidates: list[int] = [260]
    attrs = [
        "fast",
        "slow",
        "trend_fast_ma",
        "trend_slow_ma",
        "regime_ma",
        "atr_n",
        "vol_ma_period",
        "roc_period",
        "rs_period",
        "bb_period",
        "min_data_days",
    ]
    for a in attrs:
        try:
            v = int(getattr(strategy, a, 0) or 0)
            if v > 0:
                candidates.append(v)
        except Exception:
            pass

    subs = getattr(strategy, "strategies", None)
    if isinstance(subs, list):
        for s in subs:
            try:
                candidates.append(int(_estimate_strategy_lookback_days(s)))
            except Exception:
                pass
    # Keep a safety margin for rolling windows and benchmark joins.
    return max(candidates) + 20


def _run_multifactor_validate_v1(
    base_path: Path,
    base_cfg: dict[str, Any],
    preset: str,
    overrides: dict[str, Any],
    progress_hook: Callable[[int, str], None] | None = None,
    report_filename: str = MULTIFACTOR_BALANCED_REPORT_FILE,
) -> dict[str, Any]:
    """Run multifactor validation experiments with proper universe loading and detailed reporting."""
    ctx = RunContext(
        preset=str(preset),
        base_config_path=str(base_path),
        overrides=dict(overrides or {}),
    )
    # Load variants based on preset (MULTIFACTOR_BALANCED_V1 or MULTIFACTOR_FUSION_COMPARE_V1)
    preset_upper = preset.upper()
    variants = preset_variants(preset_upper)

    ov = dict(overrides or {})
    seed_symbols = list(ov.get("symbols") or DEFAULT_EXPERIMENT_SYMBOLS)
    exp_id = _build_exp_id(base_cfg, variants, preset, seed_symbols)
    exp_dir = _results_root() / "experiments" / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    base_rules_path = base_path.parent / "instrument_rules_cn.csv"
    exp_rules_path = exp_dir / "instrument_rules_cn.csv"
    if base_rules_path.exists() and not exp_rules_path.exists():
        shutil.copyfile(base_rules_path, exp_rules_path)

    start_s = str(ov.get("start_date") or "2021-01-01")
    end_s = str(ov.get("end_date") or "2026-02-01")
    benchmark = str(ov.get("benchmark") or base_cfg.get("universe", {}).get("benchmark") or "000300.SH")
    initial_capital = float(ov.get("initial_capital", 1_000_000.0))
    lot_size = int(ov.get("lot_size", 100))
    universe_limit = int(ov.get("universe_limit", 1000))
    base_order_value = float(ov.get("order_value", 10_000.0))
    lot_mode = str(ov.get("univote_lot_mode", "LOT100_MIN1LOT"))
    max_positions = int(ov.get("max_positions", 10))
    fast_mode = bool(ov.get("fast_mode", True))
    enable_mf_fast_gate = bool(ov.get("enable_multifactor_fast_gate", True))
    allow_user_override = bool(ov.get("universe_use_user_override", False))

    bt_cfg = copy.deepcopy(base_cfg)
    _inject_db_credentials_from_env(bt_cfg)
    bt_cfg.setdefault("universe", {})
    bt_cfg["universe"]["start"] = start_s
    bt_cfg["universe"]["end"] = end_s
    bt_cfg["universe"]["benchmark"] = benchmark
    bt_cfg["universe"]["symbols"] = seed_symbols

    tmp_cfg = exp_dir / "_multifactor_tmp.json"
    tmp_cfg.write_text(json.dumps(bt_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_obj, _ = load_config(str(tmp_cfg))
    provider = PostgresDataProvider(
        db=cfg_obj.db,
        runtime=cfg_obj.db_runtime,
        data_source=cfg_obj.data_source,
        schema_mapping=cfg_obj.schema_mapping,
        rules_path=tmp_cfg.parent / "instrument_rules_cn.csv",
    )
    data_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
    perf: dict[str, Any] = {
        "db_load_seconds": 0.0,
        "db_queries": 0,
        "cache_total": 0,
        "cache_hit": 0,
    }

    try:
        _emit_stage_progress(progress_hook, ctx, stage="universe_resolve", pct=8, message="阶段1/5：解析Universe成分")
        raw_symbols, universe_meta = resolve_universe_symbols(
            as_of=cfg_obj.universe.end,
            db_loader=lambda as_of, lim: load_constituents_from_db(
                db=cfg_obj.db,
                runtime=cfg_obj.db_runtime,
                as_of=as_of,
                universe_limit=lim,
            ),
            user_symbols=seed_symbols,
            allow_user_override=allow_user_override,
            universe_limit=universe_limit,
        )
        _close_stage(ctx, "universe_resolve")
    except UniverseLoadError as exc:
        _close_stage(ctx, "universe_resolve")
        err = f"UNIVERSE_LOAD_FAILED(status={exc.status})"
        rows = [
            {
                "group_id": str(v.get("group_id", "")),
                "status": "FAILED",
                "error_message": err,
                "error": err,
                "warning_notes": str(exc),
            }
            for v in variants
        ]
        _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, [])
        report_path = write_report_to_local(
            report_filename,
            _build_multifactor_validate_report(
                rows=rows,
                runs=[],
                start_s=start_s,
                end_s=end_s,
                benchmark=benchmark,
                raw_count=0,
                filtered_count=0,
                excluded={},
                initial_capital=initial_capital,
                max_positions=max_positions,
                universe_limit=universe_limit,
                base_order_value=base_order_value,
                lot_mode=lot_mode,
                perf={"db_load_seconds": 0.0, "db_queries": 0, "cache_hit_rate": 0.0},
                error=err,
            ),
        )
        ctx.add_note(err)
        ctx.write_profile(
            results_dir=str(exp_dir),
            extras={
                "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
                "error": err,
                "variant_count": int(len(variants)),
                "row_count": int(len(rows)),
            },
        )
        return {
            "exp_id": exp_id,
            "preset": preset,
            "base_config_path": str(base_path),
            "results_dir": str(exp_dir),
            "rows": rows,
            "matrix_head": rows,
            "runs": [],
            "single_report_path": str(report_path),
        }

    _emit_stage_progress(progress_hook, ctx, stage="data_load", pct=12, message="阶段2/5：加载行情与Universe过滤")
    t0 = time.perf_counter()
    ohlcv_all = _load_ohlcv_with_cache(
        provider,
        data_cache,
        raw_symbols,
        cfg_obj.universe.start,
        cfg_obj.universe.end,
        perf,
    )
    cands = build_candidates_from_ohlcv(ohlcv_all, pd.Timestamp(cfg_obj.universe.end))
    filtered, snapshot, excluded = apply_universe_filters(cands, UniverseFilterConfig(min_avg_turnover_20d=1e8))
    snapshot.to_csv(exp_dir / "universe_snapshot.csv", index=False)
    filtered_symbols = filtered["symbol"].astype(str).str.lower().tolist() if not filtered.empty else []
    raw_count = int(universe_meta.get("raw_count", len(raw_symbols)) or 0)
    filtered_count = int(len(filtered_symbols))
    perf["db_queries"] = int(perf.get("db_queries", 0)) + 1
    benchmark_df = provider.load_benchmark_daily(benchmark, cfg_obj.universe.start, cfg_obj.universe.end)
    calendar = provider.load_trading_calendar_cn(cfg_obj.universe.start, cfg_obj.universe.end)
    day_list = [d.date() for d in calendar]
    perf["db_load_seconds"] = float(time.perf_counter() - t0)
    _close_stage(ctx, "data_load")

    hist = ohlcv_all.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.lower()
    if filtered_symbols:
        symbol_set = set(filtered_symbols)
        hist = hist[hist["symbol"].isin(symbol_set)].copy()

    runs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    run_id = f"run_multifactor_{exp_id[:10]}"
    total_variants = max(len(variants), 1)
    _emit_stage_progress(progress_hook, ctx, stage="simulation", pct=20, message="阶段3/5：执行组合仿真")
    for idx, variant in enumerate(variants, start=1):
        group_id = str(variant.get("group_id", ""))
        _emit_progress(progress_hook, 15 + int((idx - 1) * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 运行中")
        # 关键修复：使用deepcopy避免参数字典引用共享
        strategy_def = copy.deepcopy(variant.get("strategy", {}) or {})
        portfolio_def = copy.deepcopy(variant.get("portfolio", {}) or {})
        run_dir = _results_root() / run_id / group_id
        run_dir.mkdir(parents=True, exist_ok=True)
        strategy_cfg = StrategyConfig(
            name=str(strategy_def.get("name", "sma_cross_regime_atr")),
            params=copy.deepcopy(strategy_def.get("params", {}) or {}),
        )
        strategy = build_strategy(strategy_cfg)
        strategy.set_context({"run_id": run_id, "results_dir": str(run_dir)})
        strategy.state["enable_multifactor_fast_gate"] = bool(enable_mf_fast_gate)
        history_lookback_days = int(ov.get("history_lookback_days") or _estimate_strategy_lookback_days(strategy))

        started_run = time.perf_counter()
        result = run_portfolio_backtest(
            strategy=strategy,
            history=hist,
            benchmark=benchmark_df,
            trading_days=day_list,
            benchmark_symbol=benchmark,
            initial_cash=initial_capital,
            max_positions=max_positions,
            lot_size=lot_size,
            enable_strength_rank=bool(portfolio_def.get("enable_strength_rank", False)),
            strength_ma=int(portfolio_def.get("strength_ma", 120)),
            strength_top_k_mode=str(portfolio_def.get("strength_top_k_mode", "FILL_TO_MAX")),
            history_lookback_days=int(history_lookback_days),
            engine_mode=str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
            fast_mode=fast_mode,
        )
        duration_ms = int((time.perf_counter() - started_run) * 1000)
        result.daily.to_csv(run_dir / "portfolio_daily.csv", index=False)
        result.trades.to_csv(run_dir / "portfolio_trades.csv", index=False)
        if not fast_mode:
            result.candidates.to_csv(run_dir / "portfolio_candidates.csv", index=False)

        m = dict(result.metrics)
        weak_blocked = int(m.get("BLOCK_WEAK_TIER_DISABLED", 0) or 0)
        row = {
            "group_id": group_id,
            "status": "OK",
            "strategy_name": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "strategy_name_full": str(strategy_def.get("name", "sma_cross_regime_atr")),
            "params_json": json.dumps(strategy_def.get("params", {}), ensure_ascii=False, sort_keys=True),
            "params_json_full": json.dumps(
                {
                    **dict(strategy_def.get("params", {}) or {}),
                    "enable_strength_rank": bool(portfolio_def.get("enable_strength_rank", False)),
                    "strength_ma": int(portfolio_def.get("strength_ma", 120)),
                    "engine_mode": str(ov.get("engine_mode") or portfolio_def.get("engine_mode") or "VECTOR_FAST_STRICT"),
                    "max_positions": int(max_positions),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "run_id": run_id,
            "total_return": float(m.get("total_return", 0.0)),
            "cagr": float(m.get("cagr", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "vol_annual": float(m.get("vol_annual", 0.0)),
            "max_daily_drawdown": float(m.get("max_daily_drawdown", 0.0)),
            "trades_count": int(m.get("trades", 0)),
            "trades": int(m.get("trades", 0)),
            "avg_positions": float(m.get("avg_positions", 0.0)),
            "turnover_avg": float(m.get("turnover_avg", 0.0)),
            "avg_candidates_per_day": float(m.get("avg_candidates_per_day", 0.0)),
            "avg_buys_per_day": float(m.get("avg_buys_per_day", m.get("avg_selected_per_day", 0.0))),
            "trigger_when_slots_small_rate": float(m.get("trigger_when_slots_small_rate", 0.0)),
            "mean_order_value_scale": float(m.get("mean_order_value_scale", 0.0)),
            "WEAK_TREND_BUY_RATE": float(m.get("WEAK_TREND_BUY_RATE", 0.0)),
            "STRONG_TREND_BUY_RATE": float(m.get("STRONG_TREND_BUY_RATE", 0.0)),
            "BLOCK_WEAK_TIER_DISABLED": weak_blocked,
            "universe_filtered_count": filtered_count,
            "max_positions": int(max_positions),
            "precompute_seconds": float(m.get("precompute_seconds", 0.0) or 0.0),
            "simulation_seconds": float(m.get("simulation_seconds", 0.0) or 0.0),
            "io_write_seconds": float(m.get("io_write_seconds", 0.0) or 0.0),
            "duration_ms": int(duration_ms),
            "history_lookback_days": int(history_lookback_days),
            "multifactor_fast_gate_days": int(m.get("multifactor_fast_gate_days", 0) or 0),
        }
        rows.append(row)
        runs.append(
            {
                "group_id": group_id,
                "run_id": run_id,
                "status": "OK",
                "error_message": None,
                "artifacts": {
                    "portfolio_daily_csv": str(run_dir / "portfolio_daily.csv"),
                    "portfolio_trades_csv": str(run_dir / "portfolio_trades.csv"),
                    "portfolio_candidates_csv": str(run_dir / "portfolio_candidates.csv") if not fast_mode else "",
                    "universe_snapshot_csv": str(exp_dir / "universe_snapshot.csv"),
                },
            }
        )
        _emit_progress(progress_hook, 15 + int(idx * 70 / total_variants), f"[{idx}/{total_variants}] {group_id} 完成")
    _close_stage(ctx, "simulation")

    _emit_stage_progress(progress_hook, ctx, stage="output_write", pct=90, message="阶段4/5：写出实验产物")
    _write_outputs(exp_dir, exp_id, base_path, base_cfg, preset, rows, runs)
    cache_total = int(perf.get("cache_total", 0) or 0)
    cache_hit = int(perf.get("cache_hit", 0) or 0)
    perf_report = {
        "db_load_seconds": float(perf.get("db_load_seconds", 0.0) or 0.0),
        "db_queries": int(perf.get("db_queries", 0) or 0),
        "cache_hit_rate": (float(cache_hit) / float(cache_total)) if cache_total > 0 else 0.0,
    }
    report_md = _build_multifactor_validate_report(
        rows=rows,
        runs=runs,
        start_s=start_s,
        end_s=end_s,
        benchmark=benchmark,
        raw_count=raw_count,
        filtered_count=filtered_count,
        excluded=excluded,
        initial_capital=initial_capital,
        max_positions=max_positions,
        universe_limit=universe_limit,
        base_order_value=base_order_value,
        lot_mode=lot_mode,
        perf=perf_report,
        error=None,
    )
    _close_stage(ctx, "output_write")
    _emit_stage_progress(progress_hook, ctx, stage="report_write", pct=94, message="阶段5/5：生成人类可读报告")
    report_path = write_report_to_local(report_filename, report_md)
    repo_root = Path(__file__).resolve().parents[3]
    host_reports = repo_root / "reports"
    host_reports.mkdir(parents=True, exist_ok=True)
    host_report_path = host_reports / report_filename
    host_report_path.write_text(report_md, encoding="utf-8")
    _close_stage(ctx, "report_write")
    ctx.write_profile(
        results_dir=str(exp_dir),
        extras={
            "engine_mode": str(ov.get("engine_mode") or "VECTOR_FAST_STRICT"),
            "perf": {
                "db_load_seconds": float(perf_report.get("db_load_seconds", 0.0) or 0.0),
                "db_queries": int(perf_report.get("db_queries", 0) or 0),
                "cache_hit_rate": float(perf_report.get("cache_hit_rate", 0.0) or 0.0),
            },
            "variant_count": int(total_variants),
            "row_count": int(len(rows)),
            "symbols_count": int(len(filtered_symbols)),
            "trading_days": int(len(day_list)),
        },
    )
    _emit_progress(progress_hook, 96, "多因子验证完成")
    return {
        "exp_id": exp_id,
        "preset": preset,
        "base_config_path": str(base_path),
        "results_dir": str(exp_dir),
        "rows": rows,
        "matrix_head": rows,
        "runs": runs,
        "single_report_path": str(host_report_path if host_report_path.exists() else report_path),
    }


if __name__ == "__main__":
    main()
