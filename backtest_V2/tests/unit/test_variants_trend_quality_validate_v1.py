from __future__ import annotations

from backtest_v2.tools.variants import preset_variants


def test_variants_trend_quality_validate_v1() -> None:
    variants = preset_variants("TREND_QUALITY_VALIDATE_V1")
    assert len(variants) == 3
    by_gid = {str(v.get("group_id")): v for v in variants}
    assert {"BASE_TREND", "SLOPE_ON", "SLOPE_CONT10"} == set(by_gid.keys())

    p_base = dict((by_gid["BASE_TREND"].get("strategy") or {}).get("params") or {})
    p_slope = dict((by_gid["SLOPE_ON"].get("strategy") or {}).get("params") or {})
    p_cont = dict((by_gid["SLOPE_CONT10"].get("strategy") or {}).get("params") or {})
    port_base = dict(by_gid["BASE_TREND"].get("portfolio") or {})
    assert str(port_base.get("engine_mode")) == "VECTOR_FAST_STRICT"

    assert bool(p_base.get("enable_trend_slope_filter", True)) is False
    assert bool(p_base.get("enable_trend_continuity_filter", True)) is False

    assert bool(p_slope.get("enable_trend_slope_filter", False)) is True
    assert int(p_slope.get("slope_lookback", 0)) == 5
    assert bool(p_slope.get("enable_trend_continuity_filter", True)) is False

    assert bool(p_cont.get("enable_trend_slope_filter", False)) is True
    assert int(p_cont.get("slope_lookback", 0)) == 5
    assert bool(p_cont.get("enable_trend_continuity_filter", False)) is True
    assert int(p_cont.get("continuity_n", 0)) == 10

