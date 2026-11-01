"""The published build updates the whole module graph without source edits."""
import importlib.util
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('asset_versions', ROOT / 'frontend/tools/asset_versions.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(root):
    (root / 'assets/js').mkdir(parents=True)
    (root / 'assets/css').mkdir(parents=True)
    (root / 'index.html').write_text(
        '<script src="assets/js/app.js?v=old"></script>'
        '<link href="assets/css/style.css">'
        '<script src="https://example.com/vendor.js?v=external"></script>', encoding='utf-8')
    (root / 'assets/js/app.js').write_text(
        "import './leaf.js?v=old&mode=x#part';\nimport './missing.js?v=old';\n", encoding='utf-8')
    (root / 'assets/js/leaf.js').write_text('export const value = 1;\n', encoding='utf-8')
    (root / 'assets/css/style.css').write_text('body { color: red; }\n', encoding='utf-8')


def test_leaf_change_revisions_html_and_transitive_module_urls(tmp_path):
    original = tmp_path / 'source'
    fixture(original)
    first, second = tmp_path / 'one', tmp_path / 'two'
    shutil.copytree(original, first)
    shutil.copytree(original, second)
    old = module.stamp(first)
    (second / 'assets/js/leaf.js').write_text('export const value = 2;\n', encoding='utf-8')
    new = module.stamp(second)
    assert old != new
    for root, revision in ((first, old), (second, new)):
        html = (root / 'index.html').read_text(encoding='utf-8')
        app = (root / 'assets/js/app.js').read_text(encoding='utf-8')
        assert f'assets/js/app.js?v={revision}' in html
        assert f'assets/css/style.css?v={revision}' in html
        assert f'./leaf.js?mode=x&v={revision}#part' in app
        assert "./missing.js?v=old" in app
        assert 'https://example.com/vendor.js?v=external' in html
    assert '?v=old' in (original / 'index.html').read_text(encoding='utf-8')


def test_revision_is_deterministic_and_includes_generated_css(tmp_path):
    source = tmp_path / 'source'
    fixture(source)
    one, two, changed = (tmp_path / name for name in ('one', 'two', 'changed'))
    for root in (one, two, changed):
        shutil.copytree(source, root)
    (changed / 'assets/css/style.css').write_text('body { color: blue; }', encoding='utf-8')
    a, b, c = module.stamp(one), module.stamp(two), module.stamp(changed)
    assert re.fullmatch('[0-9a-f]{20}', a)
    assert a == b and c != a
    assert (one / 'index.html').read_bytes() == (two / 'index.html').read_bytes()


def test_real_frontend_references_share_one_revision(tmp_path):
    build = tmp_path / 'public'
    shutil.copytree(ROOT / 'frontend/public', build)
    revision = module.stamp(build)
    references = re.compile(r'''["']([^"'\s]+\.(?:js|css)(?:\?[^"'\s#]*)?(?:\#[^"'\s]*)?)["']''', re.I)
    checked = 0
    for file in build.rglob('*'):
        if file.suffix.lower() not in {'.html', '.js', '.css'}:
            continue
        for match in references.finditer(file.read_text(encoding='utf-8')):
            url = match.group(1)
            if '://' in url or url.startswith('//'):
                continue
            path = url.split('?', 1)[0].split('#', 1)[0]
            target = (build / path.lstrip('/') if path.startswith('/') else file.parent / path).resolve()
            if target.is_relative_to(build.resolve()) and target.is_file():
                assert re.search(r'(?:\?|&)v=' + revision + r'(?:&|#|$)', url), (file, url)
                checked += 1
    assert checked > 0
