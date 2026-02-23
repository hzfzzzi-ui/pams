from __future__ import annotations

from typing import Any


def _fmt_k(v: float) -> str:
    return f"{v:.1f}"


def preset_variants(preset: str) -> list[dict[str, Any]]:
    key = str(preset or "").strip().upper()
    if key == "G1-G4":
        return [
            {"group_id": "G1", "strategy": {"name": "sma_cross", "params": {}}},
            {"group_id": "G2", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 200}}},
            {"group_id": "G3", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 120}}},
            {"group_id": "G4", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 250}}},
        ]
    if key == "REGIME_BUFFER_2X2":
        return [
            {"group_id": "G5", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 120, "entry_buffer": 0.0}}},
            {"group_id": "G6", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 120, "entry_buffer": 0.01}}},
            {"group_id": "G7", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 200, "entry_buffer": 0.0}}},
            {"group_id": "G8", "strategy": {"name": "sma_cross_regime", "params": {"regime_ma": 200, "entry_buffer": 0.01}}},
        ]
    if key == "ATR_RISK_2X2":
        base = {"regime_ma": 120, "entry_buffer": 0.01, "debug_atr": True}
        return [
            {"group_id": "G9", "strategy": {"name": "sma_cross_regime_atr", "params": {**base, "enable_atr_stop": False, "enable_atr_trail": False}}},
            {"group_id": "G10", "strategy": {"name": "sma_cross_regime_atr", "params": {**base, "enable_atr_stop": True, "enable_atr_trail": False}}},
            {"group_id": "G11", "strategy": {"name": "sma_cross_regime_atr", "params": {**base, "enable_atr_stop": False, "enable_atr_trail": True}}},
            {"group_id": "G12", "strategy": {"name": "sma_cross_regime_atr", "params": {**base, "enable_atr_stop": True, "enable_atr_trail": True}}},
        ]
    if key == "ATR_PARAM_SCAN_V1":
        ks = [0.8, 1.0, 1.2, 1.5, 2.0, 2.5]
        base = {"regime_ma": 120, "entry_buffer": 0.01, "debug_atr": True}
        out: list[dict[str, Any]] = []
        # MODE_1: stop only
        for stop_k in ks:
            gid = f"S{_fmt_k(stop_k)}_TNA_M1"
            out.append(
                {
                    "group_id": gid,
                    "strategy": {
                        "name": "sma_cross_regime_atr",
                        "params": {
                            **base,
                            "enable_atr_stop": True,
                            "enable_atr_trail": False,
                            "atr_stop_k": float(stop_k),
                        },
                    },
                }
            )
        # MODE_2: trail only
        for trail_k in ks:
            gid = f"SNA_T{_fmt_k(trail_k)}_M2"
            out.append(
                {
                    "group_id": gid,
                    "strategy": {
                        "name": "sma_cross_regime_atr",
                        "params": {
                            **base,
                            "enable_atr_stop": False,
                            "enable_atr_trail": True,
                            "atr_trail_k": float(trail_k),
                        },
                    },
                }
            )
        # MODE_3: stop + trail
        for stop_k in ks:
            for trail_k in ks:
                gid = f"S{_fmt_k(stop_k)}_T{_fmt_k(trail_k)}_M3"
                out.append(
                    {
                        "group_id": gid,
                        "strategy": {
                            "name": "sma_cross_regime_atr",
                            "params": {
                                **base,
                                "enable_atr_stop": True,
                                "enable_atr_trail": True,
                                "atr_stop_k": float(stop_k),
                                "atr_trail_k": float(trail_k),
                            },
                        },
                    }
                )
        return out
    if key == "ATR_PARAM_VALIDATE_V1":
        base = {
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "debug_atr": True,
        }
        return [
            {
                "group_id": "V_C_S0.8_TNA_M1",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **base,
                        "enable_atr_stop": True,
                        "atr_stop_k": 0.8,
                        "enable_atr_trail": False,
                    },
                },
            },
            {
                "group_id": "V_B_S0.8_T2.0_M3",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **base,
                        "enable_atr_stop": True,
                        "atr_stop_k": 0.8,
                        "enable_atr_trail": True,
                        "atr_trail_k": 2.0,
                    },
                },
            },
            {
                "group_id": "V_D_S1.0_T2.0_M3",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **base,
                        "enable_atr_stop": True,
                        "atr_stop_k": 1.0,
                        "enable_atr_trail": True,
                        "atr_trail_k": 2.0,
                    },
                },
            },
        ]
    if key == "ATR_PARAM_VALIDATE_UNIVOTE_V2":
        return [
            {
                "group_id": "V_C_S0.8_TNA_M1",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        "fast": 5,
                        "slow": 20,
                        "regime_ma": 120,
                        "entry_buffer": 0.01,
                        "atr_n": 14,
                        "debug_atr": True,
                        "enable_atr_stop": True,
                        "atr_stop_k": 0.8,
                        "enable_atr_trail": False,
                    },
                },
            },
            {
                "group_id": "V_B_S0.8_T2.0_M3",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        "fast": 5,
                        "slow": 20,
                        "regime_ma": 120,
                        "entry_buffer": 0.01,
                        "atr_n": 14,
                        "debug_atr": True,
                        "enable_atr_stop": True,
                        "atr_stop_k": 0.8,
                        "enable_atr_trail": True,
                        "atr_trail_k": 2.0,
                    },
                },
            },
            {
                "group_id": "V_D_S1.0_T2.0_M3",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        "fast": 5,
                        "slow": 20,
                        "regime_ma": 120,
                        "entry_buffer": 0.01,
                        "atr_n": 14,
                        "debug_atr": True,
                        "enable_atr_stop": True,
                        "atr_stop_k": 1.0,
                        "enable_atr_trail": True,
                        "atr_trail_k": 2.0,
                    },
                },
            },
        ]
    if key == "ATR_PORTFOLIO_VALIDATE_V1":
        base_common = {"fast": 5, "slow": 20, "regime_ma": 120, "entry_buffer": 0.01}
        return [
            {
                "group_id": "BASE",
                "strategy": {
                    "name": "sma_cross_regime",
                    "params": {
                        **base_common,
                    },
                },
            },
            {
                "group_id": "ATR_VB",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **base_common,
                        "atr_n": 14,
                        "enable_atr_stop": True,
                        "atr_stop_k": 0.8,
                        "enable_atr_trail": True,
                        "atr_trail_k": 2.0,
                    },
                },
            },
        ]
    if key == "PORTFOLIO_FILL_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
        }
        return [
            {
                "group_id": "F1",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"max_positions": 10},
                "universe": {"limit": 300, "min_avg_turnover_20d": 1e8},
            },
            {
                "group_id": "F2",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"max_positions": 15},
                "universe": {"limit": 300, "min_avg_turnover_20d": 1e8},
            },
            {
                "group_id": "F3",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"max_positions": 10},
                "universe": {"limit": 600, "min_avg_turnover_20d": 5e7},
            },
            {
                "group_id": "F4",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"max_positions": 15},
                "universe": {"limit": 600, "min_avg_turnover_20d": 5e7},
            },
        ]
    if key == "TREND_FILTER_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
        }
        return [
            {
                "group_id": "BASE_ATR",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {**base_params, "enable_trend_filter": False},
                },
            },
            {
                "group_id": "TREND_FILTER_ON",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **base_params,
                        "enable_trend_filter": True,
                        "trend_fast_ma": 120,
                        "trend_slow_ma": 200,
                    },
                },
            },
        ]
    if key == "TREND_TIER_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": False,
            "weak_trend_order_scale": 0.5,
            "strong_trend_order_scale": 1.0,
        }
        tier_on_params = dict(base_params)
        tier_on_params["enable_trend_tier"] = True
        return [
            {
                "group_id": "BASE_HARD_TREND",
                "strategy": {"name": "sma_cross_regime_atr", "params": base_params},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "TIER_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": tier_on_params},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "TREND_ACCEL_BRAKE_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.0,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": False,
        }
        accel_only = dict(base_params)
        accel_only["strong_trend_order_scale"] = 1.5
        accel_brake = dict(accel_only)
        accel_brake["enable_trend_break_exit"] = True
        return [
            {
                "group_id": "TIER_BASE",
                "strategy": {"name": "sma_cross_regime_atr", "params": base_params},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "ACCEL_ONLY",
                "strategy": {"name": "sma_cross_regime_atr", "params": accel_only},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "ACCEL_BRAKE",
                "strategy": {"name": "sma_cross_regime_atr", "params": accel_brake},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "TREND_BRAKE_CONFIRM_VALIDATE_V1":
        common = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
        }
        immediate = {**common, "trend_break_confirm_days": 1, "trend_break_confirm_gap_pct": 0.0}
        confirm_3d = {**common, "trend_break_confirm_days": 3, "trend_break_confirm_gap_pct": 0.0}
        confirm_3d_gap = {**common, "trend_break_confirm_days": 3, "trend_break_confirm_gap_pct": 0.01}
        return [
            {
                "group_id": "ACCEL_BRAKE_IMMEDIATE",
                "strategy": {"name": "sma_cross_regime_atr", "params": immediate},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "ACCEL_BRAKE_CONFIRM_3D",
                "strategy": {"name": "sma_cross_regime_atr", "params": confirm_3d},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "ACCEL_BRAKE_CONFIRM_3D_GAP",
                "strategy": {"name": "sma_cross_regime_atr", "params": confirm_3d_gap},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "STRENGTH_RANK_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "BASE",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"enable_strength_rank": False, "strength_ma": 120, "strength_top_k_mode": "FILL_TO_MAX"},
            },
            {
                "group_id": "RANK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"enable_strength_rank": True, "strength_ma": 120, "strength_top_k_mode": "FILL_TO_MAX"},
            },
        ]
    if key == "STRENGTH_RANK_VALIDATE_V2":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        regimes = [
            {"name": "P1", "start": "2022-01-01", "end": "2022-12-31"},
            {"name": "P2", "start": "2023-01-01", "end": "2024-12-31"},
            {"name": "P3", "start": "2025-01-01", "end": "2026-02-01"},
        ]
        wf_folds = [
            {
                "name": "Fold1",
                "train_start": "2022-01-01",
                "train_end": "2022-12-31",
                "test_start": "2023-01-01",
                "test_end": "2023-12-31",
            },
            {
                "name": "Fold2",
                "train_start": "2022-01-01",
                "train_end": "2023-12-31",
                "test_start": "2024-01-01",
                "test_end": "2024-12-31",
            },
            {
                "name": "Fold3",
                "train_start": "2022-01-01",
                "train_end": "2024-12-31",
                "test_start": "2025-01-01",
                "test_end": "2026-02-01",
            },
        ]
        return [
            {
                "group_id": "BASE",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"enable_strength_rank": False, "strength_ma": 120, "strength_top_k_mode": "FILL_TO_MAX"},
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
            {
                "group_id": "RANK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {"enable_strength_rank": True, "strength_ma": 120, "strength_top_k_mode": "FILL_TO_MAX"},
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
        ]
    if key == "STRENGTH_RANK_VALIDATE_V2A":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        regimes = [
            {"name": "P1", "start": "2022-01-01", "end": "2022-12-31"},
            {"name": "P2", "start": "2023-01-01", "end": "2024-12-31"},
            {"name": "P3", "start": "2025-01-01", "end": "2026-02-01"},
        ]
        wf_folds = [
            {
                "name": "Fold1",
                "train_start": "2022-01-01",
                "train_end": "2022-12-31",
                "test_start": "2023-01-01",
                "test_end": "2023-12-31",
            },
            {
                "name": "Fold2",
                "train_start": "2022-01-01",
                "train_end": "2023-12-31",
                "test_start": "2024-01-01",
                "test_end": "2024-12-31",
            },
            {
                "name": "Fold3",
                "train_start": "2022-01-01",
                "train_end": "2024-12-31",
                "test_start": "2025-01-01",
                "test_end": "2026-02-01",
            },
        ]
        return [
            {
                "group_id": "BASE",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "max_positions": 3,
                },
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
            {
                "group_id": "RANK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "max_positions": 3,
                },
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
        ]
    if key == "STRENGTH_RANK_VALIDATE_V2B":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        regimes = [
            {"name": "P1", "start": "2022-01-01", "end": "2022-12-31"},
            {"name": "P2", "start": "2023-01-01", "end": "2024-12-31"},
            {"name": "P3", "start": "2025-01-01", "end": "2026-02-01"},
        ]
        wf_folds = [
            {
                "name": "Fold1",
                "train_start": "2022-01-01",
                "train_end": "2022-12-31",
                "test_start": "2023-01-01",
                "test_end": "2023-12-31",
            },
            {
                "name": "Fold2",
                "train_start": "2022-01-01",
                "train_end": "2023-12-31",
                "test_start": "2024-01-01",
                "test_end": "2024-12-31",
            },
            {
                "name": "Fold3",
                "train_start": "2022-01-01",
                "train_end": "2024-12-31",
                "test_start": "2025-01-01",
                "test_end": "2026-02-01",
            },
        ]
        return [
            {
                "group_id": "BASE",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "max_positions": 10,
                    "engine_mode": "VECTOR_FAST_STRICT",
                },
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
            {
                "group_id": "RANK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "max_positions": 10,
                    "engine_mode": "VECTOR_FAST_STRICT",
                },
                "regimes": regimes,
                "wf_folds": wf_folds,
            },
        ]
    if key == "ENGINE_PERF_COMPARE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "SLOW_AUDIT",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "SLOW_AUDIT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "VECTOR_FAST_STRICT",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "ENGINE_PERF_COMPARE_V2":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "SLOW_AUDIT",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "SLOW_AUDIT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "VECTOR_FAST_STRICT",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "FULL_RANGE_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "BASE_ATR",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                    "rank_topk": 10,
                },
            },
            {
                "group_id": "RANK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": True,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                    "rank_topk": 10,
                },
            },
        ]
    if key == "CANDIDATE_DIAG_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "BASE_ATR",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            }
        ]
    if key == "SLOT_DEBUG_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
        }
        return [
            {
                "group_id": "BASE_ATR",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            }
        ]
    if key == "MAX_POSITION_EXPAND_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        return [
            {
                "group_id": "MAXPOS_10",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "MAXPOS_20",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 20,
                },
            },
            {
                "group_id": "MAXPOS_30",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 30,
                },
            },
        ]
    if key == "STRONG_ONLY_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        g1 = dict(base_params)
        g1["strong_trend_order_scale"] = 1.5
        g1["weak_trend_order_scale"] = 0.5
        g2 = dict(base_params)
        g2["strong_trend_order_scale"] = 2.0
        g2["weak_trend_order_scale"] = 0.0
        g3 = dict(base_params)
        g3["strong_trend_order_scale"] = 1.8
        g3["weak_trend_order_scale"] = 0.0
        return [
            {
                "group_id": "BASE_TIER",
                "strategy": {"name": "sma_cross_regime_atr", "params": g1},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "STRONG_ONLY",
                "strategy": {"name": "sma_cross_regime_atr", "params": g2},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
            {
                "group_id": "STRONG_ONLY_1P8",
                "strategy": {"name": "sma_cross_regime_atr", "params": g3},
                "portfolio": {
                    "enable_strength_rank": False,
                    "strength_ma": 120,
                    "strength_top_k_mode": "FILL_TO_MAX",
                    "engine_mode": "VECTOR_FAST_STRICT",
                    "max_positions": 10,
                },
            },
        ]
    if key == "MULTIFACTOR_RANKING_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        base_portfolio = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
            "enable_multifactor_rank": False,
            "mf_topk": 10,
            "mf_concentrate_topn": 3,
            "mf_concentrate_scale": 1.5,
        }
        return [
            {
                "group_id": "BASE_SMA_ONLY",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": dict(base_portfolio),
            },
            {
                "group_id": "MF_RANK_TOPK5",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(base_portfolio),
                    "enable_multifactor_rank": True,
                    "mf_topk": 5,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
            {
                "group_id": "MF_RANK_TOPK10",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(base_portfolio),
                    "enable_multifactor_rank": True,
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
        ]
    if key == "MULTIFACTOR_RANKING_WALKFORWARD_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        common_port = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
            "enable_multifactor_rank": False,
            "mf_topk": 10,
            "mf_concentrate_topn": 3,
            "mf_concentrate_scale": 1.5,
            "full_range_start": "2021-01-01",
            "full_range_end": "2026-02-01",
            "universe_limit": 1000,
        }
        return [
            {
                "group_id": "BASE_SMA_ONLY",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": dict(common_port),
            },
            {
                "group_id": "MF_RANK_TOPK10",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": True,
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
        ]
    if key == "MF_RANK_REGIME_GATE_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        common_port = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
            "enable_multifactor_rank": False,
            "mf_topk": 10,
            "mf_concentrate_topn": 3,
            "mf_concentrate_scale": 1.5,
            "mf_rank_gate_mode": "OFF",
            "full_range_start": "2021-01-01",
            "full_range_end": "2026-02-01",
            "universe_limit": 1000,
        }
        return [
            {
                "group_id": "BASE_MF_OFF",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": False,
                    "mf_rank_gate_mode": "OFF",
                },
            },
            {
                "group_id": "MF_ALWAYS_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": True,
                    "mf_rank_gate_mode": "ALWAYS_ON",
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
            {
                "group_id": "MF_ONLY_RISK_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": True,
                    "mf_rank_gate_mode": "ONLY_RISK_ON",
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
        ]
    if key == "MF_RANK_STRONG_GATE_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
        }
        common_port = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
            "enable_multifactor_rank": False,
            "mf_topk": 10,
            "mf_concentrate_topn": 3,
            "mf_concentrate_scale": 1.5,
            "mf_rank_gate_mode": "OFF",
            "full_range_start": "2021-01-01",
            "full_range_end": "2026-02-01",
            "universe_limit": 1000,
        }
        return [
            {
                "group_id": "BASE_MF_OFF",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": False,
                    "mf_rank_gate_mode": "OFF",
                },
            },
            {
                "group_id": "MF_ALWAYS_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": True,
                    "mf_rank_gate_mode": "ALWAYS",
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
            {
                "group_id": "MF_STRONG_GATE_ON",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": {
                    **dict(common_port),
                    "enable_multifactor_rank": True,
                    "mf_rank_gate_mode": "STRONG_RISK_ON",
                    "mf_topk": 10,
                    "mf_concentrate_topn": 3,
                    "mf_concentrate_scale": 1.5,
                },
            },
        ]
    if key == "TREND_QUALITY_VALIDATE_V1":
        base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
            "enable_strength_rank": False,
            "enable_trend_slope_filter": False,
            "slope_lookback": 5,
            "enable_trend_continuity_filter": False,
            "continuity_n": 10,
        }
        common_port = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
            "enable_multifactor_rank": False,
            "full_range_start": "2021-01-01",
            "full_range_end": "2026-02-01",
            "universe_limit": 1000,
        }
        return [
            {
                "group_id": "BASE_TREND",
                "strategy": {"name": "sma_cross_regime_atr", "params": dict(base_params)},
                "portfolio": dict(common_port),
            },
            {
                "group_id": "SLOPE_ON",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {**dict(base_params), "enable_trend_slope_filter": True, "slope_lookback": 5},
                },
                "portfolio": dict(common_port),
            },
            {
                "group_id": "SLOPE_CONT10",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": {
                        **dict(base_params),
                        "enable_trend_slope_filter": True,
                        "slope_lookback": 5,
                        "enable_trend_continuity_filter": True,
                        "continuity_n": 10,
                    },
                },
                "portfolio": dict(common_port),
            },
        ]
    if key == "MULTIFACTOR_BALANCED_V1":
        # MULTIFACTOR_BALANCED_V1宸插簾寮?        # 璇蜂娇鐢∕ULTIFACTOR_INCREMENTAL_V1鏇夸唬
        raise ValueError(
            "MULTIFACTOR_BALANCED_V1 is deprecated. "
            "The old multifactor enhancement architecture has been removed. "
            "Please use MULTIFACTOR_INCREMENTAL_V1 which uses the correct composite strategy architecture."
        )
    if key == "MULTIFACTOR_FUSION_COMPARE_V1":
        # 鍩轰簬ACCEL_BRAKE_CONFIRM_3D閰嶇疆鐨勫鍥犲瓙瀵规瘮瀹為獙锛堝凡搴熷純锛?        # 璇蜂娇鐢∕ULTIFACTOR_INCREMENTAL_V1鏇夸唬
        raise ValueError(
            "MULTIFACTOR_FUSION_COMPARE_V1 is deprecated. "
            "The old multifactor enhancement architecture has been removed. "
            "Please use MULTIFACTOR_INCREMENTAL_V1 which uses the correct composite strategy architecture."
        )
    if key == "MULTIFACTOR_INCREMENTAL_V1":
        # 閫愭澧炲姞鍥犲瓙鐨勫閲忓疄楠?        # 鐩殑锛氶噺鍖栨瘡涓洜瀛愮殑澧為噺浠峰€?
        # 鍩哄噯SMA閰嶇疆
        sma_base_params = {
            "fast": 5,
            "slow": 20,
            "regime_ma": 120,
            "entry_buffer": 0.01,
            "atr_n": 14,
            "enable_atr_stop": True,
            "atr_stop_k": 0.8,
            "enable_atr_trail": True,
            "atr_trail_k": 2.0,
            "enable_trend_filter": True,
            "trend_fast_ma": 120,
            "trend_slow_ma": 200,
            "enable_trend_tier": True,
            "enable_trend_accel": True,
            "strong_trend_order_scale": 1.5,
            "weak_trend_order_scale": 0.5,
            "enable_trend_break_exit": True,
            "trend_break_confirm_days": 3,
            "trend_break_confirm_gap_pct": 0.0,
        }

        # 鍥犲瓙鍙傛暟 (鏇翠弗鏍肩殑闃堝€间互纭繚鐪熷疄杩囨护鏁堟灉)
        volume_params = {"vol_mult": 1.8}        # 1.8x volume (浠?.5鎻愬崌)
        roc_params = {"roc_threshold": 0.03}     # 3% momentum (浠?%鎻愬崌)
        rs_params = {"rs_threshold": 0.8}        # Top 20% (浠嶵op 30%鎻愬崌)
        bb_params = {"bb_std_mult": 2.5}         # 2.5蟽 (浠?蟽鎻愬崌)

        # 閫氱敤portfolio閰嶇疆
        portfolio_config = {
            "enable_strength_rank": False,
            "strength_ma": 120,
            "strength_top_k_mode": "FILL_TO_MAX",
            "engine_mode": "VECTOR_FAST_STRICT",
            "max_positions": 10,
        }

        # 鍏抽敭淇锛氫娇鐢╟opy.deepcopy()涓烘瘡涓粍鍒涘缓鐙珛鐨勫弬鏁板壇鏈?        # 閬垮厤鎵€鏈夌粍鍏变韩鍚屼竴涓猟ict瀵硅薄寮曠敤
        import copy

        return [
            # 缁?锛氱函SMA Cross锛堝熀鍑嗭級
            {
                "group_id": "SMA_ONLY",
                "strategy": {
                    "name": "sma_cross_regime_atr",
                    "params": copy.deepcopy(sma_base_params),
                },
                "portfolio": copy.deepcopy(portfolio_config),
            },
            # 缁?锛歋MA + Volume
            {
                "group_id": "SMA_VOLUME",
                "strategy": {
                    "name": "composite",
                    "params": {
                        "combine_mode": "AND",
                        "strategies": [
                            {"name": "sma_cross_regime_atr", "params": copy.deepcopy(sma_base_params)},
                            {"name": "volume_breakout", "params": copy.deepcopy(volume_params)},
                        ],
                    },
                },
                "portfolio": copy.deepcopy(portfolio_config),
            },
            # 缁?锛歋MA + Volume + ROC
            {
                "group_id": "SMA_VOL_ROC",
                "strategy": {
                    "name": "composite",
                    "params": {
                        "combine_mode": "AND",
                        "strategies": [
                            {"name": "sma_cross_regime_atr", "params": copy.deepcopy(sma_base_params)},
                            {"name": "volume_breakout", "params": copy.deepcopy(volume_params)},
                            {"name": "momentum_roc", "params": copy.deepcopy(roc_params)},
                        ],
                    },
                },
                "portfolio": copy.deepcopy(portfolio_config),
            },
            # 缁?锛歋MA + Volume + ROC + RS
            {
                "group_id": "SMA_VOL_ROC_RS",
                "strategy": {
                    "name": "composite",
                    "params": {
                        "combine_mode": "AND",
                        "strategies": [
                            {"name": "sma_cross_regime_atr", "params": copy.deepcopy(sma_base_params)},
                            {"name": "volume_breakout", "params": copy.deepcopy(volume_params)},
                            {"name": "momentum_roc", "params": copy.deepcopy(roc_params)},
                            {"name": "relative_strength", "params": copy.deepcopy(rs_params)},
                        ],
                    },
                },
                "portfolio": copy.deepcopy(portfolio_config),
            },
            # 组5：全因子（SMA + Volume + ROC + RS + BB）
            {
                "group_id": "SMA_VOL_ROC_RS_BB",
                "strategy": {
                    "name": "composite",
                    "params": {
                        "combine_mode": "AND",
                        "strategies": [
                            {"name": "sma_cross_regime_atr", "params": copy.deepcopy(sma_base_params)},
                            {"name": "volume_breakout", "params": copy.deepcopy(volume_params)},
                            {"name": "momentum_roc", "params": copy.deepcopy(roc_params)},
                            {"name": "relative_strength", "params": copy.deepcopy(rs_params)},
                            {"name": "bollinger_breakout", "params": copy.deepcopy(bb_params)},
                        ],
                    },
                },
                "portfolio": copy.deepcopy(portfolio_config),
            },
        ]
    if key in {
        "MULTIFACTOR_INCREMENTAL_V1_6_1A",
        "EXPERIMENT_6_1A",
        "6.1A",
        "MULTIFACTOR_INCREMENTAL_V1_6_4",
        "EXPERIMENT_6_4",
        "6.4",
    }:
        return preset_variants("MULTIFACTOR_INCREMENTAL_V1")
    if key in {
        "MULTIFACTOR_INCREMENTAL_V1_6_1B",
        "EXPERIMENT_6_1B",
        "6.1B",
        "MULTIFACTOR_INCREMENTAL_V1_6_1_LIMIT",
        "EXPERIMENT_6_1_LIMIT",
        "6.1L",
    }:
        return preset_variants("MULTIFACTOR_INCREMENTAL_V1")
    if key in {"ATR_PARAM_VALIDATE_WF3", "ATR_PARAM_VALIDATE_UNIVOTE"}:
        # These are orchestrated presets handled by experiment_runner branches.
        return []
    raise ValueError(f"Unsupported preset: {preset}")

