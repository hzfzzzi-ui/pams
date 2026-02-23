from __future__ import annotations

from backtest_v2.tools.variants import preset_variants


def test_variants_mf_rank_strong_gate_validate_v1() -> None:
    variants = preset_variants("MF_RANK_STRONG_GATE_VALIDATE_V1")
    assert len(variants) == 3
    by_gid = {str(v.get("group_id")): v for v in variants}
    assert {"BASE_MF_OFF", "MF_ALWAYS_ON", "MF_STRONG_GATE_ON"} == set(by_gid.keys())

    base = dict(by_gid["BASE_MF_OFF"].get("portfolio") or {})
    always = dict(by_gid["MF_ALWAYS_ON"].get("portfolio") or {})
    strong = dict(by_gid["MF_STRONG_GATE_ON"].get("portfolio") or {})

    assert str(base.get("engine_mode")) == "VECTOR_FAST_STRICT"
    assert str(always.get("engine_mode")) == "VECTOR_FAST_STRICT"
    assert str(strong.get("engine_mode")) == "VECTOR_FAST_STRICT"
    assert int(base.get("universe_limit", 0)) == 1000
    assert int(always.get("universe_limit", 0)) == 1000
    assert int(strong.get("universe_limit", 0)) == 1000

    assert bool(base.get("enable_multifactor_rank", True)) is False
    assert str(base.get("mf_rank_gate_mode")) == "OFF"

    assert bool(always.get("enable_multifactor_rank", False)) is True
    assert str(always.get("mf_rank_gate_mode")) in {"ALWAYS", "ALWAYS_ON"}
    assert int(always.get("mf_topk", 0)) == 10

    assert bool(strong.get("enable_multifactor_rank", False)) is True
    assert str(strong.get("mf_rank_gate_mode")) == "STRONG_RISK_ON"
    assert int(strong.get("mf_topk", 0)) == 10

