import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import threading
import subprocess
import sys
from urllib.error import HTTPError

import pytest

spec = importlib.util.spec_from_file_location('verify_release', Path(__file__).parents[2] / 'scripts/verify_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def inputs():
    row = {'Id': 'container-instance', 'Image': 'sha256:' + 'a' * 64,
           'State': {'Running': True, 'StartedAt': '2026-01-01T00:00:00Z'},
           'Mounts': [], 'Config': {'Cmd': ['python', 'app.py'], 'Env': ['PASSWORD=private-value']},
           'HostConfig': {'PortBindings': {}, 'RestartPolicy': {'Name': 'unless-stopped'}, 'Privileged': False}}
    manifest = {'schema_version': 1, 'release_id': 'release-generic', 'integrated_commit': 'b' * 40,
                'components': {'generic': {'container': 'service-generic', 'source_commit': 'c' * 40,
                  'image_id': row['Image'], 'files': {'/app/code.py': 'd' * 64},
                  'runtime_policy_sha256': release.canonical_hash(release.runtime_policy(row))}},
                'database': {'container': 'db-generic', 'user': 'app', 'database': 'app',
                    'image_id': row['Image'],
                    'runtime_policy_sha256': release.canonical_hash(release.runtime_policy(row)), 'capabilities': [
                    {'id': 'metadata-field', 'kind': 'column', 'schema': 'public', 'table': 'facts',
                     'column': 'unit', 'expected_exists': False, 'deferred': True, 'reason': 'not_authorized'}]},
                'health_urls': ['http://127.0.0.1:8090/health']}
    def runner(args):
        if args[1] == 'inspect':
            return json.dumps([row])
        if args[3] == 'sha256sum':
            return 'd' * 64 + '  /app/code.py\n'
        assert args[3] == 'psql'
        sql = args[-1]
        assert sql.startswith('BEGIN READ ONLY;') and 'statement_timeout=5000' in sql
        assert not any(word in sql.upper() for word in ('UPDATE ', 'INSERT ', 'ALTER ', 'DELETE '))
        return 'BEGIN\n' + json.dumps({'id': 'metadata-field', 'exists': False}) + '\nCOMMIT\n'
    return manifest, row, runner


def test_matching_release_with_approved_disabled_capability_is_not_financial_approval():
    manifest, row, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True, now=NOW)
    assert report['identity_status'] == 'matched'
    assert report['capabilities'][0]['enabled'] is False
    assert report['capabilities'][0]['policy'] == 'deferred_by_approval'
    assert report['financial_verification'] == 'not_performed'
    assert report['user_acceptance'] == 'not_inferred'
    assert 'private-value' not in json.dumps(report)
    assert release.fresh(report, now=NOW)


@pytest.mark.parametrize('change', ['image', 'file', 'mount', 'stopped', 'capability', 'health'])
def test_arbitrary_component_drift_is_not_hidden(change):
    manifest, row, runner = inputs()
    if change == 'image':
        row['Image'] = 'sha256:' + 'e' * 64
    if change == 'mount':
        row['Mounts'] = [{'Type': 'bind', 'Source': '/different', 'Destination': '/app/data', 'RW': True}]
    if change == 'stopped':
        row['State']['Running'] = False
    def changed(args):
        text = runner(args)
        if change == 'file' and args[3:4] == ['sha256sum']:
            return text.replace('d' * 64, 'f' * 64)
        if change == 'capability' and args[3:4] == ['psql']:
            return text.replace('false', 'true')
        return text
    report = release.verify(manifest, runner=changed, health=lambda _: change != 'health', now=NOW)
    assert report['identity_status'] == 'drift'


@pytest.mark.parametrize('stage', ['inspect', 'sha256sum', 'psql', 'health'])
def test_failure_is_unknown_not_reused_success(stage):
    manifest, _, runner = inputs()
    def failed(args):
        if stage in args:
            raise RuntimeError('secret-password-do-not-emit')
        return runner(args)
    def health(_):
        if stage == 'health':
            raise OSError('secret-password-do-not-emit')
        return True
    report = release.verify(manifest, runner=failed, health=health, now=NOW)
    assert report['identity_status'] == 'unknown'
    assert 'secret-password' not in json.dumps(report)


@pytest.mark.parametrize('mutation', ['traversal', 'shell', 'sql', 'health_refresh', 'health_external', 'default_image', 'missing_policy'])
def test_untrusted_manifest_cannot_execute_commands_or_sql(mutation):
    manifest, _, _ = inputs()
    if mutation == 'traversal':
        manifest['components']['generic']['files'] = {'/app/../data/private': 'd' * 64}
    if mutation == 'shell':
        manifest['components']['generic']['container'] = 'api;rm'
    if mutation == 'sql':
        manifest['database']['capabilities'][0]['table'] = "facts';UPDATE facts"
    if mutation == 'health_refresh':
        manifest['health_urls'] = ['http://pams.local/api/sync']
    if mutation == 'health_external':
        manifest['health_urls'] = ['http://external.example/health']
    if mutation == 'default_image':
        manifest['components']['generic']['image_id'] = 'latest'
    if mutation == 'missing_policy':
        del manifest['database']['capabilities'][0]['reason']
    with pytest.raises(ValueError):
        release.verify(manifest, runner=lambda _: pytest.fail('Executed before validation'))


@pytest.mark.parametrize('age,expected', [(0, True), (600, True), (601, False), (-1, False)])
def test_report_freshness(age, expected):
    assert release.fresh({'observed_at': NOW.isoformat()}, now=NOW + timedelta(seconds=age)) is expected


def test_report_is_atomic_history_is_preserved(tmp_path):
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True, now=NOW)
    release.save_report(tmp_path, report)
    changed = copy.deepcopy(report)
    changed['identity_status'] = 'unknown'
    release.save_report(tmp_path, changed)
    assert len(list(tmp_path.glob('*.json'))) == 3
    assert json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))['identity_status'] == 'unknown'


def test_identical_clock_ticks_never_overwrite_history(tmp_path, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True, now=NOW)
    monkeypatch.setattr(release, 'datetime', FixedClock)
    release.save_report(tmp_path, report)
    changed = {**report, 'identity_status': 'unknown'}
    release.save_report(tmp_path, changed)
    history = [path for path in tmp_path.glob('*.json') if path.name != 'latest.json']
    assert len(history) == 2
    assert {json.loads(path.read_text(encoding='utf-8'))['identity_status'] for path in history} == {'matched', 'unknown'}
    assert json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8')) == changed


@pytest.mark.parametrize('existing', [False, True])
def test_github_record_is_idempotent_and_uses_only_own_marker(existing, tmp_path, monkeypatch):
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    marker = '<!-- pams-release:release-generic -->'
    calls = []
    def gh(args):
        calls.append(args)
        if '--slurp' in args:
            comments = [{'id': 8, 'body': marker, 'user': {'login': 'other-user'}}]
            if existing:
                comments.append({'id': 9, 'body': release.record_text(report), 'user': {'login': 'current-user'}})
            return json.dumps([comments])
        if args[-1] == 'user':
            return '{"login":"current-user"}'
        body = json.loads(Path(args[-1]).read_text(encoding='utf-8'))['body']
        assert body.startswith(marker) and '不代表财务核准' in body
        return '{}'
    release.publish(report, 'owner/repository', 42, gh)
    assert calls[-1][calls[-1].index('--method') + 1] == ('PATCH' if existing else 'POST')
    assert not Path(calls[-1][-1]).exists()


def test_old_observation_cannot_be_published():
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True, now=NOW)
    with pytest.raises(ValueError, match='stale'):
        release.publish(report, 'owner/repository', 42, lambda _: pytest.fail('network'))


@pytest.mark.parametrize('key,value', [('CapAdd', ['SYS_ADMIN']), ('CapDrop', ['NET_RAW']),
    ('SecurityOpt', ['seccomp=unconfined']), ('ReadonlyRootfs', True)])
def test_permission_drift_prevents_exec(key, value):
    manifest, row, runner = inputs()
    row['HostConfig'][key] = value
    def guarded(args):
        assert args[1] != 'exec'
        return runner(args)
    assert release.verify(manifest, runner=guarded, health=lambda _: True)['identity_status'] == 'drift'


def test_exec_uses_inspected_id_and_detects_replacement():
    manifest, row, runner = inputs()
    inspected = 0
    def changed(args):
        nonlocal inspected
        if args[1] == 'inspect':
            inspected += 1
            if inspected == 2:
                row['Id'] = 'replacement'
        if args[1] == 'exec':
            assert args[2] == row['Id']
        return runner(args)
    report = release.verify(manifest, runner=changed, health=lambda _: True)
    assert report['components']['generic']['status'] == 'unknown'


def test_incomplete_health_read_is_unknown():
    manifest, _, runner = inputs()
    def truncated(_):
        raise IncompleteRead(b'{', 10)
    report = release.verify(manifest, runner=runner, health=truncated)
    assert report['identity_status'] == 'unknown'


def test_health_redirect_never_reaches_redirect_target():
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/sync-not-health')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError):
            release.read_health(f'http://127.0.0.1:{server.server_port}/health')
        assert paths == ['/health']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('change', ['failed_record', 'incomplete', 'manifest', 'component', 'capability', 'commit', 'malformed'])
def test_saved_report_cannot_hide_failure_or_manifest_change(change):
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    assert release.report_ok(report, manifest)
    if change == 'failed_record':
        report['record_status'] = 'failed'
    elif change == 'incomplete':
        report = {'identity_status': 'matched', 'observed_at': report['observed_at']}
    elif change == 'manifest':
        manifest['components']['generic']['source_commit'] = 'f' * 40
    elif change == 'component':
        report['components']['generic']['checks']['files'] = None
    elif change == 'capability':
        report['capabilities'][0]['enabled'] = True
    elif change == 'commit':
        report['integrated_commit'] = 'a' * 40
    else:
        report['components'] = None
    assert not release.report_ok(report, manifest)


def test_latest_cannot_be_overwritten_by_older_run_and_corruption_is_replaced(tmp_path):
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    release.save_report(tmp_path, report)
    older = copy.deepcopy(report)
    older['observed_at'] = NOW.isoformat()
    release.save_report(tmp_path, older)
    assert json.loads((tmp_path / 'latest.json').read_text()) == report
    (tmp_path / 'latest.json').write_text('{corrupt')
    release.save_report(tmp_path, report)
    assert json.loads((tmp_path / 'latest.json').read_text()) == report


def test_concurrent_same_host_publish_creates_one_comment():
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    comments = []
    methods = []
    def gh(args):
        if '--slurp' in args:
            return json.dumps([comments])
        if args[-1] == 'user':
            return '{"login":"current-user"}'
        method = args[args.index('--method') + 1]
        methods.append(method)
        body = json.loads(Path(args[-1]).read_text(encoding='utf-8'))['body']
        if method == 'POST':
            comments.append({'id': 9, 'body': body, 'user': {'login': 'current-user'}})
        else:
            comments[0]['body'] = body
        return '{}'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(release.publish, report, 'owner/thread-test', 123, gh) for _ in range(2)]
        for result in results:
            result.result(timeout=10)
    assert methods.count('POST') == 1 and methods.count('PATCH') == 1


def test_older_observation_cannot_overwrite_newer_github_record():
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    newer = copy.deepcopy(report)
    newer['observed_at'] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    def gh(args):
        if '--slurp' in args:
            return json.dumps([[{'id': 9, 'body': release.record_text(newer), 'user': {'login': 'current-user'}}]])
        if args[-1] == 'user':
            return '{"login":"current-user"}'
        pytest.fail('Must not overwrite newer record')
    with pytest.raises(ValueError, match='newer'):
        release.publish(report, 'owner/repository', 42, gh)


@pytest.mark.parametrize('database', [False, True])
def test_policy_changed_at_final_inspect_is_not_matched(database):
    manifest, row, runner = inputs()
    count = 0
    target = manifest['database']['container'] if database else manifest['components']['generic']['container']
    def changed(args):
        nonlocal count
        result = runner(args)
        if args[1] == 'inspect' and args[-1] == target:
            count += 1
            if count == 2:
                ending = copy.deepcopy(row)
                ending['HostConfig']['RestartPolicy'] = {'Name': 'no'}
                return json.dumps([ending])
        return result
    report = release.verify(manifest, runner=changed, health=lambda _: True)
    assert report['identity_status'] == 'unknown'


def test_publication_rechecks_freshness_before_write(monkeypatch):
    manifest, _, runner = inputs()
    report = release.verify(manifest, runner=runner, health=lambda _: True)
    checks = iter([True, True, False])
    monkeypatch.setattr(release, 'fresh', lambda _: next(checks))
    def gh(args):
        if '--slurp' in args:
            return '[[]]'
        if args[-1] == 'user':
            return '{"login":"current-user"}'
        pytest.fail('stale write')
    with pytest.raises(ValueError, match='stale'):
        release.publish(report, 'owner/freshness', 123, gh)


def test_process_death_releases_local_os_lock():
    script = str(Path(release.__file__).resolve())
    code = ("import importlib.util,os; s=importlib.util.spec_from_file_location('release'," + repr(script)
            + "); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "lock=m.local_lock('abrupt-process-regression'); lock.__enter__(); os._exit(0)")
    subprocess.run([sys.executable, '-c', code], check=True, timeout=10)
    with release.local_lock('abrupt-process-regression'):
        pass
