from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_api_write_endpoints_are_limited_to_local_origins() -> None:
    main_py = (REPO_ROOT / "backend" / "api" / "main.py").read_text(encoding="utf-8")

    assert "allow_origins=[\"*\"]" not in main_py
    assert "ALLOWED_WEB_ORIGINS" in main_py
    assert "LOCAL_WRITE_HOSTS" in main_py
    assert "MUTATING_METHODS" in main_py
    assert "local_write_origin_guard" in main_py
    assert "write request origin is not allowed" in main_py
