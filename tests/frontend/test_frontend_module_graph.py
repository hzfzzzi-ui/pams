"""Validate local browser module identity and one-way shared dependencies."""
from collections import defaultdict
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

PUBLIC = Path(__file__).resolve().parents[2] / "frontend/public"
IMPORT = re.compile(r"\b(?:from\s*|import\s*(?:\(\s*)?)(['\"])([^'\"]+\.js(?:\?[^'\"]*)?)\1")
SCRIPT = re.compile(r"<script\b[^>]*\bsrc=['\"]([^'\"]+\.js(?:\?[^'\"]*)?)['\"]")


def module_references():
    for source in PUBLIC.rglob("*.js"):
        for match in IMPORT.finditer(source.read_text(encoding="utf-8")):
            yield source, match.group(2)
    for source in PUBLIC.glob("*.html"):
        for match in SCRIPT.finditer(source.read_text(encoding="utf-8")):
            yield source, match.group(1)


def test_local_module_urls_resolve_and_share_one_cache_identity():
    identities = defaultdict(set)
    for source, reference in module_references():
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            continue
        target = ((PUBLIC / url.path.lstrip("/")) if url.path.startswith("/") else source.parent / url.path).resolve()
        assert target.is_relative_to(PUBLIC.resolve()), (source, reference)
        assert target.is_file(), (source, reference)
        assert parse_qs(url.query).get("v"), (source, reference, "Unversioned local import")
        identities[target].add(url.query)
    assert identities, "No local module graph was inspected"
    assert {str(path.relative_to(PUBLIC)): sorted(versions) for path, versions in identities.items() if len(versions) != 1} == {}


def test_shared_components_do_not_depend_on_business_features():
    for source, reference in module_references():
        relative = source.relative_to(PUBLIC).as_posix()
        if not relative.startswith(("assets/js/shared/", "assets/js/components/")):
            continue
        target = (source.parent / urlsplit(reference).path).resolve()
        assert not target.is_relative_to((PUBLIC / "assets/js/features").resolve()), (source, reference)
