"""Partition a complete pytest collection by file for isolated CI workers."""

from collections import Counter

import pytest


def shard_files(nodeids, count):
    """Balance whole files deterministically, keeping file-local tests together."""
    files = Counter(nodeid.split('::', 1)[0] for nodeid in nodeids)
    loads = [0] * count
    result = {}
    for path, size in sorted(files.items(), key=lambda item: (-item[1], item[0])):
        worker = min(range(count), key=lambda index: (loads[index], index))
        result[path] = worker
        # Database integration tests spend more time creating isolated databases.
        weight = 4 if path.startswith('tests/database/') else 1
        loads[worker] += size * weight
    return result


def pytest_addoption(parser):
    parser.addoption('--pams-shard', help='Select an isolated CI shard, e.g. 1/4')


def pytest_collection_modifyitems(config, items):
    value = config.getoption('--pams-shard')
    if value is None:
        return
    try:
        index, count = (int(part) for part in value.split('/'))
        if not 1 <= index <= count:
            raise ValueError
    except ValueError as exc:
        raise pytest.UsageError('--pams-shard requires an index between 1 and count, e.g. 1/4') from exc
    assignments = shard_files((item.nodeid for item in items), count)
    selected, deselected = [], []
    for item in items:
        target = selected if assignments[item.nodeid.split('::', 1)[0]] == index - 1 else deselected
        target.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
