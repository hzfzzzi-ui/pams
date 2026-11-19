from tests.support.ci_shard import shard_files


def test_shards_preserve_each_test_file_once_and_are_order_independent():
    nodeids = [
        'tests/database/test_one.py::test_a[1]',
        'tests/database/test_one.py::test_a[2]',
        'tests/investments/test_two.py::test_b',
        'tests/frontend/test_three.py::test_c',
        'tests/ledger/test_four.py::test_d',
    ]
    assignment = shard_files(nodeids, 4)
    assert assignment == shard_files(reversed(nodeids), 4)
    selected = [[nodeid for nodeid in nodeids if assignment[nodeid.split('::', 1)[0]] == shard]
                for shard in range(4)]
    assert sorted(nodeid for shard in selected for nodeid in shard) == sorted(nodeids)
    assert assignment['tests/database/test_one.py'] in range(4)
    assert all(selected)
