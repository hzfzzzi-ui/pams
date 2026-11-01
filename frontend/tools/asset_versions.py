"""Stamp local JS/CSS references in a disposable frontend build tree."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys


REFERENCE = re.compile(
    r'''(?P<quote>["'])(?P<path>(?:\.{1,2}/|/|assets/|[A-Za-z0-9_-]+/)[^"'\s?#]*?\.(?:js|css))'''
    r'''(?P<query>\?[^"'\s#]*)?(?P<fragment>\#[^"'\s]*)?(?P=quote)''',
    re.IGNORECASE,
)


def stamp(root: Path) -> str:
    """Use one deterministic revision so changes propagate through module imports."""
    root = root.resolve()
    files = sorted(p for p in root.rglob('*') if p.is_file())
    digest = hashlib.sha256()
    for file in files:
        path = file.relative_to(root).as_posix().encode('utf-8')
        content = file.read_bytes()
        digest.update(len(path).to_bytes(8, 'big') + path)
        digest.update(len(content).to_bytes(8, 'big') + content)
    revision = digest.hexdigest()[:20]

    for file in files:
        if file.suffix.lower() not in {'.html', '.js', '.css'}:
            continue
        original = file.read_text(encoding='utf-8')

        def rewrite(match: re.Match) -> str:
            path = match['path']
            target = (root / path.lstrip('/')) if path.startswith('/') else (file.parent / path)
            target = target.resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return match.group(0)
            parts = [part for part in (match['query'] or '').lstrip('?').split('&')
                     if part and part.split('=', 1)[0] != 'v']
            parts.append('v=' + revision)
            return f"{match['quote']}{path}?{'&'.join(parts)}{match['fragment'] or ''}{match['quote']}"

        transformed = REFERENCE.sub(rewrite, original)
        if transformed != original:
            file.write_text(transformed, encoding='utf-8', newline='\n')
    return revision


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: asset_versions.py BUILD_ROOT')
    print('frontend asset revision:', stamp(Path(sys.argv[1])))
