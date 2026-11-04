"""Validate local browser module identity and one-way shared dependencies."""
from pathlib import Path
import re
from urllib.parse import urlsplit

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


def test_local_module_urls_resolve_as_unversioned_relative_paths():
    references = 0
    for source, reference in module_references():
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            continue
        target = ((PUBLIC / url.path.lstrip("/")) if url.path.startswith("/") else source.parent / url.path).resolve()
        assert target.is_relative_to(PUBLIC.resolve()), (source, reference)
        assert target.is_file(), (source, reference)
        assert not url.query and not url.fragment, (source, reference)
        assert not url.path.startswith("/"), (source, reference, "Use paths relative to the asset directory")
        references += 1
    assert references, "No local module graph was inspected"


def test_shared_components_do_not_depend_on_business_features():
    for source, reference in module_references():
        relative = source.relative_to(PUBLIC).as_posix()
        if not relative.startswith(("assets/js/shared/", "assets/js/components/")):
            continue
        target = (source.parent / urlsplit(reference).path).resolve()
        assert not target.is_relative_to((PUBLIC / "assets/js/features").resolve()), (source, reference)
