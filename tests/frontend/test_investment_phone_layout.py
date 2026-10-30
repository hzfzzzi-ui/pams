import hashlib
import importlib.util
from pathlib import Path
import subprocess

import tinycss2


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mobile_styles", ROOT / "frontend/tools/mobile_styles.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_phone_breakpoints_preserve_narrow_devices_and_disable_desktop():
    result = MODULE.phone_styles(
        "@media (max-width: 760px){.a{display:grid}}"
        "@media (min-width: 761px){.a{display:block}}"
        "@media (max-width: 380px){.a{gap:2px}}"
        "@media (prefers-reduced-motion: reduce){.a{animation:none}}"
    )
    assert "(min-width: 0px)" in result
    assert "(max-width: 0px)" in result
    assert "(max-width: 380px)" in result
    assert "prefers-reduced-motion: reduce" in result


def test_viewport_lengths_use_phone_width_without_rewriting_strings():
    result = MODULE.phone_styles('.a{width:calc(100dvw - 18px);font-size:10vw;content:"100vw"}')
    assert "min(100dvw, 430px)" in result
    assert "min(10vw, 43px)" in result
    assert 'content:"100vw"' in result


def test_original_phone_baseline_is_retained_and_build_is_deterministic():
    baselines = {
        "style": "aef39b73d3746bebbede94daa04c559de14d3991ca2e4e579e4f2c09a84387a7",
        "design-system": "7a19043760523616103d4d363e3fba01d74fa49398ad7f88efc658d86e501b3d",
    }
    for name, fingerprint in baselines.items():
        source = (ROOT / f"frontend/public/assets/css/{name}.css").read_text(encoding="utf-8")
        assert hashlib.sha256(source.encode()).hexdigest() == fingerprint
        result = MODULE.phone_styles(source)
        assert result == MODULE.phone_styles(source)
        assert not [r for r in tinycss2.parse_stylesheet(result) if r.type == "error"]


def test_phone_mode_is_limited_to_investment_and_approved_more_hub():
    html = (ROOT / "frontend/public/investments.html").read_text(encoding="utf-8")
    assert "investment-phone-layout" in html
    assert "style-phone.css?v=20261031-legacy-css-v1" in html
    more = (ROOT / "frontend/public/more.html").read_text(encoding="utf-8")
    assert "style-phone.css?v=20261031-legacy-css-v1" in more
    for name in ("ledger", "accounts", "data_sources"):
        assert "-phone.css" not in (ROOT / f"frontend/public/{name}.html").read_text(encoding="utf-8")


def test_pending_badge_does_not_mutate_financial_fields():
    subprocess.run(["node", "--experimental-vm-modules", "tests/frontend/frontend_phone_badges.mjs"],
                   cwd=ROOT, check=True, capture_output=True, text=True)
