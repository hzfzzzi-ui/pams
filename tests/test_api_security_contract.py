from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_api_write_endpoints_are_limited_to_local_origins() -> None:
    main_py = (REPO_ROOT / "backend" / "api" / "main.py").read_text(encoding="utf-8")

    assert "allow_origins=[\"*\"]" not in main_py
    assert "ALLOWED_WEB_ORIGINS" in main_py
    assert "LOCAL_WRITE_HOSTS" in main_py
    assert "MUTATING_METHODS" in main_py
    assert "local_write_origin_guard" in main_py
    assert "write request origin is not allowed" in main_py


def test_analysis_get_endpoints_do_not_expose_asset_master_sync() -> None:
    analysis_py = (REPO_ROOT / "backend" / "api" / "endpoints" / "analysis.py").read_text(encoding="utf-8")

    lookthrough_get = re.search(
        r'@router\.get\("/lookthrough"\)(?P<body>.*?)@router\.get\("/return-attribution"\)',
        analysis_py,
        re.S,
    )
    return_get = re.search(
        r'@router\.get\("/return-attribution"\)(?P<body>.*?)@router\.post\("/asset-master/sync"\)',
        analysis_py,
        re.S,
    )

    assert lookthrough_get
    assert return_get
    assert "sync_asset_master=True" not in lookthrough_get.group("body")
    assert "sync_asset_master=True" not in return_get.group("body")
    assert '@router.post("/asset-master/sync")' in analysis_py
    assert "sync_asset_master=True" in analysis_py


def test_investment_return_series_get_endpoints_are_read_only() -> None:
    main_py = (REPO_ROOT / "backend" / "api" / "main.py").read_text(encoding="utf-8")

    section_get = re.search(
        r'@app\.get\("/investment/section-return-series"\)(?P<body>.*?)'
        r'@app\.post\("/investment/section-return-series/snapshot"\)',
        main_py,
        re.S,
    )
    total_get = re.search(
        r'@app\.get\("/investment/return-series"\)(?P<body>.*?)'
        r'@app\.post\("/investment/return-series/snapshot"\)',
        main_py,
        re.S,
    )
    section_post = re.search(
        r'@app\.post\("/investment/section-return-series/snapshot"\)(?P<body>.*?)@app\.get\("/nav/weekly"\)',
        main_py,
        re.S,
    )
    total_post = re.search(
        r'@app\.post\("/investment/return-series/snapshot"\)(?P<body>.*?)@app\.get\("/nav/drawdown"\)',
        main_py,
        re.S,
    )

    assert section_get
    assert total_get
    assert section_post
    assert total_post
    for block in (section_get.group("body"), total_get.group("body")):
        assert "refreshSnapshot" not in block
        assert "_upsert_investment_return_series_snapshot" not in block
        assert "conn.commit()" not in block
    assert "_upsert_investment_return_series_snapshot" in section_post.group("body")
    assert "_upsert_investment_return_series_snapshot" in total_post.group("body")


def test_portfolio_valuation_state_get_is_read_only_and_publish_is_explicit_post() -> None:
    endpoint_py = (REPO_ROOT / "backend" / "api" / "endpoints" / "portfolio_state.py").read_text(
        encoding="utf-8"
    )

    get_block = re.search(
        r'@router\.get\("/portfolio/valuation-state"\)(?P<body>.*?)'
        r'@router\.post\("/portfolio/valuation-state/publish"\)',
        endpoint_py,
        re.S,
    )
    post_block = re.search(
        r'@router\.post\("/portfolio/valuation-state/publish"\)(?P<body>.*)',
        endpoint_py,
        re.S,
    )

    assert get_block
    assert post_block
    assert "refresh: bool" not in get_block.group("body")
    assert "cycle:" not in get_block.group("body")
    assert "publish_portfolio_valuation_state_payload" not in get_block.group("body")
    assert "refresh=False" in get_block.group("body")
    assert "publish_portfolio_valuation_state_payload" in post_block.group("body")
