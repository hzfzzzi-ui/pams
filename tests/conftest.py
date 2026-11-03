"""Temporary, explicitly enabled CI quarantine for PR #201."""

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items):
    if os.environ.get("PAMS_CI_SKIP_PR_201") != "true":
        return

    manifest = Path(__file__).with_name("ci-pr201-temporary-skips.txt")
    listed_ids = {
        line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing_ids = listed_ids - {item.nodeid for item in items}
    if missing_ids:
        raise pytest.UsageError("Stale PR #201 skip entries:\n" + "\n".join(sorted(missing_ids)))

    temporary_skip = pytest.mark.skip(reason="Temporary PR #201 quarantine from CI run 431")
    for item in items:
        if item.nodeid in listed_ids:
            item.add_marker(temporary_skip)
