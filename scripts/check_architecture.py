"""Check PAMS repository architecture without inspecting private local files."""
from __future__ import annotations

import argparse
import ast
import importlib.util
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'database/postgres/bootstrap/manifest.txt'
SCHEMA_PREFIX = 'database/postgres/schema/'

# Exact project paths only. Text in compatibility code or tests is permitted.
RETIRED_PREFIXES = (
    'backtest_V2/', 'strategy/', 'strategy-gpt/',
    'telegram-bot/', 'telegram-bot-assistant/',
    'backend/api/backtest/', 'backend/api/strategy/', 'backend/api/datahub/',
    'backend/api/services/',
)
RETIRED_PAGES = frozenset({
    'frontend/public/backtest.html', 'frontend/public/strategy.html',
    'frontend/public/research.html', 'frontend/public/watchlist.html',
    'frontend/public/experiments.html',
})
RETIRED_FILES = frozenset({
    'generate_mock_navdata.py',
    'backend/api/endpoints/backtest_v2.py',
    'backend/api/endpoints/backtest_v2_artifacts.py',
    'backend/api/endpoints/live_signals.py',
    'frontend/public/assets/js/backtest.js',
    'frontend/public/assets/js/strategy.js',
    'frontend/public/assets/js/research.js',
    'frontend/public/assets/js/watchlist.js',
    'frontend/public/experiments.html.bak',
    'database/postgres/schema/008_task_queue.sql',
    'database/postgres/schema/009_notifications.sql',
    'database/postgres/schema/010_strategy.sql',
    'database/postgres/schema/011_fundamental_sentiment.sql',
    'database/postgres/schema/012_watchlist_management.sql',
    'database/postgres/schema/013_pit_tables.sql',
    'database/postgres/schema/015_live_signal_packages.sql',
})
RETIRED_PREFIXES_FOLDED = tuple(prefix.casefold() for prefix in RETIRED_PREFIXES)
RETIRED_FILES_FOLDED = frozenset(entry.casefold() for entry in RETIRED_PAGES | RETIRED_FILES)
# These old scripts embed historical migrations or repairs and are not a
# bootstrap contract. Current structural migrations and static seed data remain
# valid; their SQL is not judged using generic INSERT/UPDATE string searches.
RETIRED_SQL_NUMBERS = frozenset(
    {27, 28, 29, 30, 33, *range(37, 42), *range(43, 48), *range(49, 67), 68}
)
HISTORY_NAME = re.compile(r'(?:^|_)(?:backfill|drop_deprecated)(?:_|\.|$)', re.I)

# Local mode never walks configuration, account ledgers, data or import files.
WORKTREE_PATHS = (
    'generate_mock_navdata.py',
    'backend/api/domain', 'backend/api/endpoints', 'backend/api/services',
    'backend/api/backtest', 'backend/api/strategy', 'backend/api/datahub',
    'frontend/public', 'tests', 'database/postgres/schema', MANIFEST,
)
RETIRED_ROOTS = ('backtest_V2', 'strategy', 'strategy-gpt', 'telegram-bot', 'telegram-bot-assistant')
PRIVATE_PREFIXES = (
    'data/', '账户台账/', 'import_templates/', 'templates/', 'output/', 'outputs/',
    'reports/', 'logs/', 'artifacts/', 'test-results/', 'tmp/', '.codex-artifacts/',
    '.pytest_cache/', '.mypy_cache/', '.playwright-cli/', 'htmlcov/',
    'database/backups/', 'database/postgres/backups/',
)
PRIVATE_FILES = frozenset({'config/bank_reconcile.yaml'})
ROOT_BACKUP_EXTENSIONS = ('.dump', '.backup', '.sqlite', '.sqlite3', '.db', '.zip', '.tar', '.tar.gz', '.bak', '.log', '.xls', '.xlsx')
DDL_STATEMENT = re.compile(r'(?:^|;|\n)\s*(?:CREATE(?:\s+(?:TEMP|TEMPORARY|UNLOGGED))?|ALTER|DROP)\s+TABLE\b', re.I)
SQL_LITERAL_OR_COMMENT = re.compile(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/")


def _private_path(path: str) -> bool:
    folded = path.casefold()
    if folded in PRIVATE_FILES:
        return True
    if folded.startswith(tuple(prefix.casefold() for prefix in PRIVATE_PREFIXES)):
        return True
    pure = PurePosixPath(folded)
    filename = pure.name
    if (filename == '.env' or filename.startswith('.env.')) and not filename.endswith(('.example', '.template')):
        return True
    return len(pure.parts) == 1 and (filename.endswith(ROOT_BACKUP_EXTENSIONS)
                                     or filename in {'.coverage', 'coverage.xml'}
                                     or (filename.startswith('.tmp_') and filename.endswith('.json')))


def tracked_files(root: Path) -> list[str]:
    """Use the index; never walk ignored/untracked data and secrets."""
    result = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return [p.decode('utf-8') for p in result.stdout.split(b'\0') if p]


def _git_paths(root: Path, args: list[str]) -> list[str]:
    result = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z', *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return [p.decode('utf-8') for p in result.stdout.split(b'\0') if p]


def worktree_files(root: Path) -> list[str]:
    """Inspect existing source paths only, adding nonignored local additions.

    Retired top-level directories are tested for existence, without enumerating
    their contents. The public error names the retired directory, never its
    potentially private contents. A directory ignored by Git is skipped unless
    it contains an existing indexed path.
    """
    names = _git_paths(root, ['--cached', '--others', '--exclude-standard', '--', *WORKTREE_PATHS])
    paths = {name for name in names if not _private_path(name) and
             ((root / name).is_file() or (root / name).is_symlink())}
    indexed_retired = _git_paths(root, ['--cached', '--', *RETIRED_ROOTS])
    for directory in RETIRED_ROOTS:
        target = root / directory
        if not target.exists() and not target.is_symlink():
            continue
        indexed = any(name.casefold().startswith(directory.casefold() + '/') and
                      ((root / name).is_file() or (root / name).is_symlink())
                      for name in indexed_retired)
        ignored = subprocess.run(
            ['git', '-C', str(root), 'check-ignore', '--no-index', '-q', directory + '/'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if ignored.returncode not in (0, 1):
            raise subprocess.CalledProcessError(ignored.returncode, ignored.args, stderr=ignored.stderr)
        if indexed or ignored.returncode == 1:
            paths.add(directory + '/__retired_directory__')
    return sorted(paths)


def _forbidden_import(name: str) -> bool:
    return name == 'main' or name.startswith('main.') or name == 'endpoints' or name.startswith('endpoints.') or name == 'backend.api.main' or name.startswith('backend.api.main.') or name == 'backend.api.endpoints' or name.startswith('backend.api.endpoints.')


def _domain_import_violations(path: str, text: str) -> list[str]:
    """Inspect executable Python imports, including relative imports."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [f'{path}:{exc.lineno}: invalid Python syntax']
    relative = PurePosixPath(path).relative_to('backend/api')
    parts = list(relative.with_suffix('').parts)
    package = '.'.join(parts[:-1])
    if parts[-1] == '__init__':
        package = '.'.join(parts[:-1])
    result = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if node.level:
                try:
                    module = importlib.util.resolve_name('.' * node.level + module, package)
                except (ImportError, ValueError):
                    result.append(f'{path}:{node.lineno}: invalid relative import')
                    continue
            names = [module, *(f'{module}.{alias.name}' if module else alias.name for alias in node.names)]
        if any(_forbidden_import(name) for name in names):
            result.append(f'{path}:{node.lineno}: domain must not import API entrypoints')
    return result


def _static_string(node: ast.AST, known: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return ''.join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else ' __dynamic__ ' for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_string(node.left, known), _static_string(node.right, known)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == 'format':
            return _static_string(node.func.value, known)
        if node.func.attr == 'SQL' and node.args:
            return _static_string(node.args[0], known)
    return None


def _endpoint_ddl_violations(path: str, text: str) -> list[str]:
    """Inspect static SQL passed to execute, not docstrings or test messages."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [f'{path}:{exc.lineno}: invalid Python syntax']
    assigned: dict[str, set[str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, set()).add(_static_string(node.value, {}))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assigned.setdefault(node.target.id, set()).add(_static_string(node.value, {}))
    known = {name: next(iter(values)) for name, values in assigned.items()
             if len(values) == 1 and None not in values}
    errors = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {'execute', 'executemany'} and node.args):
            continue
        statement = _static_string(node.args[0], known)
        if statement and DDL_STATEMENT.search(SQL_LITERAL_OR_COMMENT.sub(' ', statement)):
            errors.append(f'{path}:{node.lineno}: runtime table DDL belongs in database/postgres/schema')
    return errors


def _manifest_violations(root: Path, tracked: set[str]) -> list[str]:
    if MANIFEST not in tracked or not (root / MANIFEST).is_file() or (root / MANIFEST).is_symlink():
        return [f'{MANIFEST}: tracked bootstrap manifest is missing']
    errors = []
    seen: set[str] = set()
    schema_root = (root / SCHEMA_PREFIX).resolve()
    for number, line in enumerate((root / MANIFEST).read_text(encoding='utf-8-sig').splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith('#'):
            continue
        pure = PurePosixPath(entry)
        if pure.is_absolute() or '..' in pure.parts or not entry.startswith(SCHEMA_PREFIX) or len(pure.parts) != 4 or pure.suffix != '.sql':
            errors.append(f'{MANIFEST}:{number}: invalid schema path {entry}')
            continue
        if entry in seen:
            errors.append(f'{MANIFEST}:{number}: duplicate entry {entry}')
        seen.add(entry)
        path = root / entry
        if entry not in tracked or not path.is_file() or not path.resolve().is_relative_to(schema_root):
            errors.append(f'{MANIFEST}:{number}: schema must be tracked and present: {entry}')
        match = re.match(r'^(\d+)_', pure.name, re.I)
        if HISTORY_NAME.search(pure.name) or (match and int(match.group(1)) in RETIRED_SQL_NUMBERS):
            errors.append(f'{MANIFEST}:{number}: historical backfill/repair is not bootstrap schema: {entry}')
    if not seen:
        errors.append(f'{MANIFEST}: bootstrap manifest is empty')
    return errors


def check_architecture(root: Path, files: Iterable[str]) -> list[str]:
    """Return actionable errors for tracked paths only."""
    paths = set(files)
    errors: list[str] = []
    for path in sorted(paths):
        folded = path.casefold()
        if _private_path(path):
            errors.append('private data, secrets or runtime output must not be tracked (path redacted)')
            continue
        if folded.startswith(RETIRED_PREFIXES_FOLDED) or folded in RETIRED_FILES_FOLDED:
            errors.append(f'{path}: retired module/page must not be reintroduced')
        pure = PurePosixPath(path)
        if len(pure.parts) == 2 and pure.parts[0].casefold() == 'tests' and pure.name.casefold().startswith('test_') and pure.suffix.casefold() == '.py':
            errors.append(f'{path}: place tests under tests/<domain>/')
        is_domain = path.startswith('backend/api/domain/') and pure.suffix == '.py'
        is_endpoint = path.startswith('backend/api/endpoints/') and pure.suffix == '.py'
        if is_domain or is_endpoint:
            source = root / path
            if source.is_symlink():
                errors.append(f'{path}: API Python files must not be symlinks')
            elif source.is_file():
                text = source.read_text(encoding='utf-8-sig')
                if is_domain:
                    errors.extend(_domain_import_violations(path, text))
                else:
                    errors.extend(_endpoint_ddl_violations(path, text))
    errors.extend(_manifest_violations(root, paths))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true', help='include local nonignored source changes; exclude deleted paths')
    args = parser.parse_args()
    try:
        files = worktree_files(ROOT) if args.worktree else tracked_files(ROOT)
        errors = check_architecture(ROOT, files)
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f'Architecture check failed: {type(exc).__name__}', file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print('PAMS architecture boundaries passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
