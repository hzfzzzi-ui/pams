"""Observe an approved deployment; never build, restart, migrate or repair it."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler
from uuid import uuid4


Runner = Callable[[list[str]], str]
IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
SHA = re.compile(r'^[0-9a-f]{64}$')


def run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=45)
    if result.returncode:
        raise RuntimeError('command_failed')
    return result.stdout


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_manifest(value: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError('invalid_manifest')
    if value.get('schema_version') != 1 or not NAME.fullmatch(str(value.get('release_id', ''))):
        raise ValueError('invalid_manifest_identity')
    if not re.fullmatch(r'[0-9a-f]{40}', str(value.get('integrated_commit', ''))):
        raise ValueError('missing_integrated_commit')
    components = value.get('components')
    if not isinstance(components, dict) or not components:
        raise ValueError('missing_components')
    for name, item in components.items():
        if not isinstance(name, str) or not isinstance(item, dict) or not isinstance(item.get('files'), dict):
            raise ValueError('invalid_component_shape')
        if not NAME.fullmatch(name) or not NAME.fullmatch(str(item.get('container', ''))):
            raise ValueError('invalid_component')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(item.get('image_id', ''))):
            raise ValueError('invalid_image_identity')
        if not re.fullmatch(r'[0-9a-f]{40}', str(item.get('source_commit', ''))):
            raise ValueError('missing_component_provenance')
        if not item.get('files') or not SHA.fullmatch(str(item.get('runtime_policy_sha256', ''))):
            raise ValueError('missing_component_evidence')
        for path, digest in item['files'].items():
            if (not isinstance(path, str) or not path.startswith('/') or '..' in PurePosixPath(path).parts
                    or any(c in path for c in '\r\n\\') or not SHA.fullmatch(str(digest))):
                raise ValueError('invalid_file_evidence')
    database = value.get('database')
    if database:
        if not isinstance(database, dict) or not isinstance(database.get('capabilities', []), list):
            raise ValueError('invalid_database_shape')
        for key in ('container', 'user', 'database'):
            if not NAME.fullmatch(str(database.get(key, ''))):
                raise ValueError('invalid_database_target')
        if (not re.fullmatch(r'sha256:[0-9a-f]{64}', str(database.get('image_id', '')))
                or not SHA.fullmatch(str(database.get('runtime_policy_sha256', '')))):
            raise ValueError('missing_database_identity')
        ids = set()
        for item in database.get('capabilities', []):
            if not isinstance(item, dict) or item.get('id') in ids:
                raise ValueError('duplicate_or_invalid_capability')
            ids.add(item.get('id'))
            if not NAME.fullmatch(str(item.get('id', ''))) or item.get('kind') not in ('column', 'table'):
                raise ValueError('invalid_capability')
            for key in ('schema', 'table') + (('column',) if item['kind'] == 'column' else ()):
                if not IDENTIFIER.fullmatch(str(item.get(key, ''))):
                    raise ValueError('invalid_schema_identifier')
            if type(item.get('expected_exists')) is not bool or type(item.get('deferred')) is not bool:
                raise ValueError('missing_capability_policy')
            if item['deferred'] and (item['expected_exists'] or not item.get('reason')):
                raise ValueError('invalid_deferred_policy')
    for url in value.get('health_urls', []):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or parts.hostname not in ('localhost', '127.0.0.1', 'pams.local'):
            raise ValueError('invalid_health_target')
        if parts.path != '/health' or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('health_only')


def runtime_policy(inspect: dict) -> dict:
    """No environment values or credential fingerprints are exported."""
    config = inspect['Config']
    host = inspect['HostConfig']
    return {'mounts': sorted([{'type': m['Type'], 'source': m['Source'], 'target': m['Destination'],
                               'rw': m['RW']} for m in inspect['Mounts']], key=lambda m: m['target']),
            'ports': host.get('PortBindings'), 'restart': host.get('RestartPolicy'),
            'privileged': host.get('Privileged'), 'user': config.get('User'),
            'cap_add': host.get('CapAdd'), 'cap_drop': host.get('CapDrop'),
            'security_opt': host.get('SecurityOpt'), 'readonly_rootfs': host.get('ReadonlyRootfs'),
            'cmd': config.get('Cmd'), 'entrypoint': config.get('Entrypoint'),
            'working_dir': config.get('WorkingDir')}


def observe_component(expected: dict, runner: Runner) -> dict:
    try:
        rows = json.loads(runner(['docker', 'inspect', expected['container']]))
        if len(rows) != 1:
            raise ValueError('ambiguous_container')
        row = rows[0]
        checks = {'running': row['State']['Running'] is True,
                  'image': row['Image'] == expected['image_id'], 'files': None,
                  'runtime_policy': canonical_hash(runtime_policy(row)) == expected['runtime_policy_sha256']}
        if not all(checks[k] for k in ('running', 'image', 'runtime_policy')):
            return {'status': 'drift', 'checks': checks, 'image_id': row['Image'],
                    'container_id': row['Id'], 'reason': 'unapproved_runtime_no_exec'}
        paths = sorted(expected['files'])
        lines = runner(['docker', 'exec', row['Id'], 'sha256sum', '--', *paths]).splitlines()
        files = {}
        for line in lines:
            match = re.fullmatch(r'([0-9a-f]{64}) [ *](.+)', line)
            if not match or match[2] in files:
                raise ValueError('invalid_file_observation')
            files[match[2]] = match[1]
        checks['files'] = files == expected['files']
        ending = json.loads(runner(['docker', 'inspect', expected['container']]))[0]
        if (ending['Id'], ending['State']['StartedAt'], ending['State']['Running']) != (
                row['Id'], row['State']['StartedAt'], row['State']['Running']):
            raise ValueError('container_changed_during_observation')
        if canonical_hash(runtime_policy(ending)) != expected['runtime_policy_sha256']:
            raise ValueError('policy_changed_during_observation')
        return {'status': 'matched' if all(checks.values()) else 'drift', 'checks': checks,
                'image_id': row['Image'], 'container_id': row['Id'],
                'started_at': row['State']['StartedAt'], 'expected_source_commit': expected['source_commit'],
                'provenance': 'approved_file_fingerprint_not_self_reported_revision',
                'file_count': len(files)}
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        return {'status': 'unknown', 'reason': type(exc).__name__}


def observe_capabilities(database: dict, runner: Runner) -> list[dict]:
    items = database.get('capabilities', [])
    if not items:
        return []
    selects = []
    for item in items:
        table = 'columns' if item['kind'] == 'column' else 'tables'
        where = f"table_schema='{item['schema']}' AND table_name='{item['table']}'"
        if item['kind'] == 'column':
            where += f" AND column_name='{item['column']}'"
        selects.append(f"SELECT json_build_object('id','{item['id']}','exists',EXISTS(SELECT 1 FROM information_schema.{table} WHERE {where}));")
    sql = 'BEGIN READ ONLY; SET LOCAL statement_timeout=5000; ' + ' '.join(selects) + ' COMMIT;'
    try:
        row = json.loads(runner(['docker', 'inspect', database['container']]))[0]
        if (row['Image'] != database['image_id'] or not row['State']['Running']
                or canonical_hash(runtime_policy(row)) != database['runtime_policy_sha256']):
            return [{'id': i['id'], 'status': 'drift', 'reason': 'unapproved_database_no_exec'} for i in items]
        text = runner(['docker', 'exec', row['Id'], 'psql', '-X', '-A', '-t', '-v',
                       'ON_ERROR_STOP=1', '-U', database['user'], '-d', database['database'], '-c', sql])
        rows = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
        actual = {r['id']: r['exists'] for r in rows}
        if len(rows) != len(items) or len(actual) != len(items) or any(type(v) is not bool for v in actual.values()):
            raise ValueError('incomplete_capability_evidence')
        ending = json.loads(runner(['docker', 'inspect', database['container']]))[0]
        if (ending['Id'], ending['State']['StartedAt'], ending['State']['Running']) != (
                row['Id'], row['State']['StartedAt'], True):
            raise ValueError('database_changed_during_observation')
        if canonical_hash(runtime_policy(ending)) != database['runtime_policy_sha256']:
            raise ValueError('database_policy_changed_during_observation')
        return [{'id': i['id'], 'status': 'matched' if actual.get(i['id']) == i['expected_exists'] else 'drift',
                 'enabled': actual.get(i['id']),
                 'policy': 'deferred_by_approval' if i['deferred'] else 'required',
                 'reason': i.get('reason')} for i in items]
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        return [{'id': i['id'], 'status': 'unknown', 'reason': type(exc).__name__} for i in items]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_health(url: str) -> bool:
    with build_opener(NoRedirect).open(url, timeout=10) as response:
        return response.status == 200 and json.load(response).get('ok') is True


def verify(manifest: dict, *, runner: Runner = run_command,
           health: Callable[[str], bool] = read_health, now: datetime | None = None) -> dict:
    validate_manifest(manifest)
    started = now or datetime.now(timezone.utc)
    components = {name: observe_component(item, runner) for name, item in manifest['components'].items()}
    capabilities = observe_capabilities(manifest.get('database', {}), runner)
    health_checks = []
    for url in manifest.get('health_urls', []):
        try:
            health_checks.append({'url': url, 'status': 'matched' if health(url) else 'drift'})
        except (OSError, HTTPException, ValueError, TypeError):
            health_checks.append({'url': url, 'status': 'unknown'})
    states = [c['status'] for c in components.values()] + [c['status'] for c in capabilities + health_checks]
    status = 'drift' if 'drift' in states else 'unknown' if 'unknown' in states else 'matched'
    return {'schema_version': 1, 'release_id': manifest['release_id'], 'manifest_sha256': canonical_hash(manifest),
            'integrated_commit': manifest['integrated_commit'], 'observed_at': started.isoformat(),
            'completed_at': (now or datetime.now(timezone.utc)).isoformat(),
            'identity_status': status, 'components': components, 'capabilities': capabilities,
            'health': health_checks, 'financial_verification': 'not_performed', 'user_acceptance': 'not_inferred',
            'record_status': 'pending', 'coverage': ['image_identity', 'listed_file_fingerprints',
                'mounts_ports_command_permissions', 'listed_schema_capabilities', 'health'],
            'not_checked': ['secret_environment_values', 'unlisted_files', 'financial_amounts', 'UI', 'backup_restore']}


def fresh(report: dict, *, now: datetime | None = None, max_age: int = 600) -> bool:
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(report['observed_at'])).total_seconds()
        return 0 <= age <= max_age
    except (KeyError, TypeError, ValueError):
        return False


def record_text(report: dict) -> str:
    lines = [f"<!-- pams-release:{report['release_id']} -->", f"<!-- pams-observed:{report['observed_at']} -->", '## 自动运行身份核验',
             f"发布批次：`{report['release_id']}`；观察时间：`{report['observed_at']}`。",
             f"身份：**{report['identity_status']}**；核准清单 SHA256：`{report['manifest_sha256']}`。"]
    for name, item in report['components'].items():
        lines.append(f"- {name}：{item['status']}，镜像 `{item.get('image_id', 'unknown')}`。")
    for item in report['capabilities']:
        lines.append(f"- 能力 {item['id']}：{item['status']}，enabled={item.get('enabled', 'unknown')}，{item.get('policy', 'unknown')}。")
    lines.append('仅运行身份核验；不代表财务核准、用户验收或稳定基线。敏感环境、未列文件和备份恢复不在本报告范围。')
    return '\n'.join(lines)


@contextmanager
def local_lock(key: str):
    path = Path(tempfile.gettempdir()) / ('pams-release-' + hashlib.sha256(key.encode()).hexdigest() + '.lock')
    deadline = time.monotonic() + 45
    # The OS releases ownership on process death; the persistent file is not ownership.
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        if sys.platform == 'win32':
            import msvcrt
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def unlock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def unlock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('local_record_lock_timeout') from None
                time.sleep(0.1)
        try:
            yield
        finally:
            unlock()


def publish(report: dict, repository: str, pr: int, runner: Runner = run_command) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or pr <= 0 or not fresh(report):
        raise ValueError('invalid_or_stale_record')
    with local_lock(f'{repository}:{pr}:{report["release_id"]}'):
        if not fresh(report):
            raise ValueError('stale_record_after_lock')
        _publish_locked(report, repository, pr, runner)


def _publish_locked(report: dict, repository: str, pr: int, runner: Runner) -> None:
    endpoint = f'repos/{repository}/issues/{pr}/comments'
    pages = json.loads(runner(['gh', 'api', '--paginate', '--slurp', endpoint]))
    marker = f"<!-- pams-release:{report['release_id']} -->"
    user = json.loads(runner(['gh', 'api', 'user']))['login']
    comments = [c for page in pages for c in page if c.get('user', {}).get('login') == user
                and c.get('body', '').startswith(marker)]
    if len(comments) > 1:
        raise ValueError('ambiguous_release_record')
    if comments:
        match = re.search(r'<!-- pams-observed:(.+?) -->', comments[0]['body'])
        if not match or datetime.fromisoformat(match[1]) > datetime.fromisoformat(report['observed_at']):
            raise ValueError('newer_or_unversioned_record_exists')
    target = f"repos/{repository}/issues/comments/{comments[0]['id']}" if comments else endpoint
    # JSON input keeps Markdown and UTF-8 out of shell quoting and command-line limits.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8', delete=False) as handle:
        json.dump({'body': record_text(report)}, handle, ensure_ascii=False)
        path = Path(handle.name)
    try:
        if not fresh(report):
            raise ValueError('stale_record_before_write')
        runner(['gh', 'api', target, '--method', 'PATCH' if comments else 'POST', '--input', str(path)])
    finally:
        path.unlink(missing_ok=True)


def save_report(directory: Path, report: dict) -> None:
    with local_lock('output:' + str(directory.resolve())):
        _save_report_locked(directory, report)


def _save_report_locked(directory: Path, report: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid4().hex + '.json'
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    with (directory / name).open('x', encoding='utf-8') as history:
        history.write(payload)
    latest = directory / 'latest.json'
    if latest.exists():
        try:
            old = json.loads(latest.read_text(encoding='utf-8'))
            if datetime.fromisoformat(old['observed_at']) > datetime.fromisoformat(report['observed_at']):
                return
        except (ValueError, TypeError, KeyError):
            pass  # A corrupt latest report must not prevent a fresh observation.
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, suffix='.tmp', encoding='utf-8', delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    try:
        temporary.replace(directory / 'latest.json')
    finally:
        temporary.unlink(missing_ok=True)


def report_ok(report: dict, manifest: dict) -> bool:
    try:
        return _report_ok(report, manifest)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _report_ok(report: dict, manifest: dict) -> bool:
    validate_manifest(manifest)
    if (not fresh(report) or report.get('schema_version') != 1 or report.get('identity_status') != 'matched'
            or report.get('record_status') not in ('pending', 'synced')
            or report.get('manifest_sha256') != canonical_hash(manifest)
            or report.get('release_id') != manifest['release_id']
            or report.get('integrated_commit') != manifest['integrated_commit']):
        return False
    components = report.get('components', {})
    if set(components) != set(manifest['components']):
        return False
    for name, item in components.items():
        if (item.get('status') != 'matched' or item.get('image_id') != manifest['components'][name]['image_id']
                or item.get('checks') != {'running': True, 'image': True, 'files': True, 'runtime_policy': True}):
            return False
    expected = manifest.get('database', {}).get('capabilities', [])
    actual = report.get('capabilities', [])
    if len(actual) != len(expected) or {i.get('id') for i in actual} != {i['id'] for i in expected}:
        return False
    if any(i.get('status') != 'matched' for i in actual):
        return False
    policies = {i['id']: i for i in expected}
    for item in actual:
        policy = policies[item['id']]
        if (item.get('enabled') is not policy['expected_exists']
                or item.get('policy') != ('deferred_by_approval' if policy['deferred'] else 'required')):
            return False
    health = report.get('health', [])
    return (len(health) == len(manifest.get('health_urls', []))
            and {h.get('url') for h in health} == set(manifest.get('health_urls', []))
            and all(h.get('status') == 'matched' for h in health))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--check-report', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--record-pr', type=int)
    parser.add_argument('--repository', default='hzfzzzi-ui/pams')
    args = parser.parse_args()
    if args.check_report:
        if not args.manifest:
            parser.error('--check-report requires the approved --manifest')
        report = json.loads(args.check_report.read_text(encoding='utf-8-sig'))
        manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        return 0 if report_ok(report, manifest) else 2
    if not args.manifest:
        parser.error('--manifest or --check-report is required; no implicit production target')
    if args.record_pr is not None and args.record_pr <= 0:
        parser.error('--record-pr must be positive')
    try:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        report = verify(manifest)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = {'schema_version': 1, 'release_id': 'unknown', 'identity_status': 'unknown',
                  'observed_at': datetime.now(timezone.utc).isoformat(), 'reason': type(exc).__name__,
                  'record_status': 'pending', 'financial_verification': 'not_performed'}
    if args.record_pr and report['release_id'] != 'unknown':
        try:
            publish(report, args.repository, args.record_pr)
            report['record_status'] = 'synced'
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            report['record_status'] = 'failed'
            report['record_error'] = type(exc).__name__
    if args.output_dir:
        save_report(args.output_dir, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if fresh(report) and report['identity_status'] == 'matched' and report['record_status'] != 'failed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
