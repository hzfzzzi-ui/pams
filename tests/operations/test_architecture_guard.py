"""Contract checks use synthetic repositories and never inspect private files."""
from pathlib import Path
import subprocess

from scripts.check_architecture import MANIFEST, check_architecture, tracked_files, worktree_files


def fixture_repo(tmp_path: Path):
    manifest = tmp_path / MANIFEST
    manifest.parent.mkdir(parents=True)
    schema = 'database/postgres/schema/004_fx_rates.sql'
    sql = tmp_path / schema
    sql.parent.mkdir(parents=True)
    sql.write_text('CREATE TABLE synthetic (id int);\n', encoding='utf-8')
    manifest.write_text(f'# empty database only\n{schema}\n', encoding='utf-8')
    return [MANIFEST, schema]


def test_current_shape_and_untracked_private_material_are_ignored(tmp_path):
    files = fixture_repo(tmp_path)
    # Not included in Git's tracked-file list, so neither content nor syntax is read.
    private = tmp_path / 'backtest_V2' / 'personal.py'
    private.parent.mkdir()
    private.write_bytes(b'\xffprivate')
    assert check_architecture(tmp_path, files) == []


def test_retired_paths_and_root_tests_are_rejected(tmp_path):
    files = fixture_repo(tmp_path)
    files += ['BACKTEST_v2/run.py', 'strategy/engine.py', 'strategy-gpt/x.py',
              'telegram-bot/bot.py', 'telegram-bot-assistant/bot.py',
              'backend/api/services/old.py', 'backend/api/backtest/router.py',
              'backend/api/endpoints/live_signals.py',
              'frontend/public/assets/js/strategy.js',
              'frontend/public/research.html', 'tests/Test_old.py']
    errors = check_architecture(tmp_path, files)
    assert len(errors) == 11
    assert any('tests/<domain>/' in error for error in errors)
    assert any('retired' in error for error in errors)


def test_domain_cannot_import_entrypoints_but_comments_and_domain_imports_are_allowed(tmp_path):
    files = fixture_repo(tmp_path)
    good = 'backend/api/domain/assets/ok.py'
    bad = 'backend/api/domain/assets/bad.py'
    (tmp_path / good).parent.mkdir(parents=True)
    (tmp_path / good).write_text('# from endpoints import bad\nfrom domain.assets import value\n', encoding='utf-8')
    (tmp_path / bad).write_text('import main\nfrom endpoints import risk\nfrom backend.api import endpoints\n', encoding='utf-8')
    errors = check_architecture(tmp_path, files + [good, bad])
    assert len(errors) == 3
    assert all('bad.py' in error for error in errors)


def test_manifest_rejects_duplicate_missing_and_history(tmp_path):
    files = fixture_repo(tmp_path)
    old = 'database/postgres/schema/049_europe_sweden_asset_mapping.sql'
    missing = 'database/postgres/schema/101_synthetic_backfill.sql'
    (tmp_path / old).write_text('-- synthetic\n', encoding='utf-8')
    (tmp_path / MANIFEST).write_text('\n'.join([files[1], files[1], old, missing]) + '\n', encoding='utf-8')
    errors = check_architecture(tmp_path, files + [old])
    assert any('duplicate entry' in e for e in errors)
    assert any('must be tracked and present' in e for e in errors)
    assert sum('historical backfill/repair' in e for e in errors) == 2


def test_manifest_rejects_escape_and_allows_structural_suffixes(tmp_path):
    files = fixture_repo(tmp_path)
    structural = 'database/postgres/schema/028a_account_reference_constraints.sql'
    (tmp_path / structural).write_text('-- synthetic\n', encoding='utf-8')
    (tmp_path / MANIFEST).write_text(f'{structural}\ndatabase/postgres/schema/../outside.sql\n', encoding='utf-8')
    errors = check_architecture(tmp_path, files + [structural])
    assert len(errors) == 1
    assert 'invalid schema path' in errors[0]


def test_manifest_requires_tracked_files_and_nonempty_contents(tmp_path):
    files = fixture_repo(tmp_path)
    assert any('tracked bootstrap manifest is missing' in e for e in check_architecture(tmp_path, files[1:]))
    (tmp_path / MANIFEST).write_text('# nothing\n', encoding='utf-8')
    assert any('manifest is empty' in e for e in check_architecture(tmp_path, files))


def test_domain_symlink_is_rejected_without_reading_it(tmp_path, monkeypatch):
    files = fixture_repo(tmp_path)
    relative = 'backend/api/domain/assets/unsafe.py'
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b'\xff')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: p == source or original(p))
    errors = check_architecture(tmp_path, files + [relative])
    assert any('must not be symlinks' in error for error in errors)


def test_tracked_secrets_data_and_runtime_outputs_are_rejected_but_examples_and_fixtures_allowed(tmp_path):
    files = fixture_repo(tmp_path)
    sensitive = ['.env', 'config/.env.production', 'data/ledger.csv', '账户台账/source.pdf',
                 'import_templates/private.xlsx', 'templates/private.csv', '.tmp_state.json',
                 'outputs/report.csv', 'test-results/a.png',
                 'database/postgres/backups/db.dump', 'snapshot.tar.gz']
    safe = ['.env.example', 'config/.env.production.example',
            'tests/fixtures/data/synthetic.csv', 'tests/fixtures/templates/synthetic.csv',
            'tests/fixtures/example.xlsx']
    errors = check_architecture(tmp_path, files + sensitive + safe)
    assert len(errors) == len(sensitive)
    assert all('private data, secrets or runtime output' in e for e in errors)


def test_endpoint_rejects_executed_static_table_ddl_only(tmp_path):
    files = fixture_repo(tmp_path)
    endpoint = tmp_path / 'backend/api/endpoints/synthetic.py'
    endpoint.parent.mkdir(parents=True)
    endpoint.write_text('''\
"""Documentation example CREATE TABLE is not executed."""
query = """-- comment\n CREATE TABLE synthetic (id int)"""
cur.execute(query)
cur.execute("ALTER TABLE synthetic ADD COLUMN name text")
cur.execute(f"DROP TABLE {name}")
cur.execute("SELECT 'CREATE TABLE not_executed; DROP TABLE not_executed'")
''', encoding='utf-8')
    errors = check_architecture(tmp_path, files + ['backend/api/endpoints/synthetic.py'])
    assert len(errors) == 3
    assert all('runtime table DDL' in error for error in errors)


def test_worktree_mode_uses_existing_index_and_unignored_source_additions(tmp_path, monkeypatch):
    files = fixture_repo(tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    gone = tmp_path / 'tests/test_deleted.py'
    gone.parent.mkdir()
    gone.write_text('pass\n', encoding='utf-8')
    ignored = tmp_path / 'tests/test_ignored.py'
    (tmp_path / '.gitignore').write_text('tests/test_ignored.py\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(tmp_path), 'add', *files, 'tests/test_deleted.py', '.gitignore'], check=True)
    gone.unlink()
    ignored.write_text('pass\n', encoding='utf-8')
    violation = tmp_path / 'tests/test_new.py'
    violation.write_text('pass\n', encoding='utf-8')
    source = tmp_path / 'backend/api/domain/assets/new.py'
    source.parent.mkdir(parents=True)
    source.write_text('from endpoints import forbidden\n', encoding='utf-8')
    private = tmp_path / 'data/private-secret.txt'
    private.parent.mkdir()
    private.write_bytes(b'\xffprivate')
    template = tmp_path / 'templates/private.csv'
    template.parent.mkdir()
    template.write_bytes(b'\xffprivate')
    root_state = tmp_path / '.tmp_state.json'
    root_state.write_bytes(b'\xffprivate')
    env = tmp_path / 'frontend/public/.env.local'
    env.parent.mkdir(parents=True)
    env.write_bytes(b'\xffprivate')
    retired = tmp_path / 'strategy'
    retired.mkdir()
    (retired / 'private-content.txt').write_bytes(b'\xffprivate')
    original_read = Path.read_text
    def guarded_read(path, *args, **kwargs):
        assert path not in {private, template, root_state, env, retired / 'private-content.txt'}
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded_read)
    index = tracked_files(tmp_path)
    local = worktree_files(tmp_path)
    assert 'tests/test_deleted.py' in index and 'tests/test_deleted.py' not in local
    assert 'tests/test_ignored.py' not in local
    assert 'tests/test_new.py' in local and 'backend/api/domain/assets/new.py' in local
    assert 'data/private-secret.txt' not in local
    assert 'templates/private.csv' not in local and '.tmp_state.json' not in local
    assert 'frontend/public/.env.local' not in local
    assert 'strategy/__retired_directory__' in local and 'strategy/private-content.txt' not in local
    errors = check_architecture(tmp_path, local)
    assert any('test_new.py' in e for e in errors)
    assert any('domain must not import' in e for e in errors)
    assert any('strategy/' in e for e in errors)


def test_worktree_ignores_retired_directory_unless_its_contents_are_indexed(tmp_path):
    files = fixture_repo(tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('backtest_V2/\n', encoding='utf-8')
    directory = tmp_path / 'backtest_V2'
    directory.mkdir()
    (directory / 'old.py').write_text('pass\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(tmp_path), 'add', *files, '.gitignore'], check=True)
    assert 'backtest_V2/__retired_directory__' not in worktree_files(tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '-f', 'backtest_V2/old.py'], check=True)
    assert 'backtest_V2/__retired_directory__' in worktree_files(tmp_path)
