"""The same assets produce the same URLs, including transitive imports."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def build_fixture(public, dependency):
    files = {
        "app.html": '<link href="assets/css/style-phone.css"><script src="assets/js/app.js"></script>'
                    '<script src="https://cdn.example.com/library.js"></script>',
        "assets/css/style-phone.css": "body { margin: 0; }",
        "assets/js/app.js": "import './nested/settings.js';",
        "assets/js/nested/settings.js": dependency,
    }
    for name, content in files.items():
        path = public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "frontend/tools/fingerprint_assets.py"), str(public)],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def test_release_assets_change_url_when_only_a_nested_module_changes(tmp_path):
    first = tmp_path / "first"
    version = build_fixture(first, "export const settings = 1;")
    assert version == build_fixture(tmp_path / "identical", "export const settings = 1;")
    assert version != build_fixture(tmp_path / "updated", "export const settings = 2;")

    html = (first / "app.html").read_text(encoding="utf-8")
    assert f'assets/{version}/js/app.js' in html
    assert f'assets/{version}/css/style-phone.css' in html
    assert 'https://cdn.example.com/library.js' in html
    js = first / "assets" / version / "js"
    assert (js / "app.js").read_text(encoding="utf-8") == "import './nested/settings.js';"
    assert (js / "nested/settings.js").read_text(encoding="utf-8") == "export const settings = 1;"
