from unittest.mock import MagicMock

import pytest

from tests.support.asset_pg_safety import resolve_asset_test_target, validate_asset_test_database


LOCAL = "host=/tmp/pams-asset-test-safe dbname=pams_asset_test_safe user=pams"
CI_DSN = "postgresql://pams:pams_ci@127.0.0.1:5432/pams_asset_test_ci"
CI_ENV = {"CI": "true", "GITHUB_ACTIONS": "true", "PAMS_ASSET_CI_TEST_DSN": CI_DSN}


def test_never_falls_back_to_application_database():
    assert resolve_asset_test_target({"PGHOST": "production", "PGDATABASE": "pams"}) is None
    with pytest.raises(ValueError, match="required"):
        resolve_asset_test_target({"CI": "true", "GITHUB_ACTIONS": "true"})


def test_explicit_local_and_ci_targets():
    assert resolve_asset_test_target({"PAMS_ASSET_ISOLATED_TEST_DSN": LOCAL}) == (LOCAL, "unix", "pams_asset_test_safe")
    assert resolve_asset_test_target(CI_ENV) == (CI_DSN, "github-ci", "pams_asset_test_ci")


@pytest.mark.parametrize("dsn", [
    "host=/tmp/other dbname=pams_asset_test_safe user=pams",
    "host=/tmp/pams-asset-test-safe/../other dbname=pams_asset_test_safe user=pams",
    "host=/tmp/pams-asset-test-safe dbname=pams user=pams",
    "host=/tmp/pams-asset-test-safe dbname=pams_asset_test_safe hostaddr=127.0.0.1 user=pams",
    "host=/tmp/pams-asset-test-safe dbname=pams_asset_test_safe options=-csearch_path=public user=pams",
])
def test_rejects_unsafe_local_targets(dsn):
    with pytest.raises(ValueError):
        resolve_asset_test_target({"PAMS_ASSET_ISOLATED_TEST_DSN": dsn})


@pytest.mark.parametrize("env", [
    {"PAMS_ASSET_CI_TEST_DSN": CI_DSN},
    {"CI": "true", "PAMS_ASSET_CI_TEST_DSN": CI_DSN},
    {**CI_ENV, "PAMS_ASSET_CI_TEST_DSN": CI_DSN.replace("127.0.0.1", "localhost")},
    {**CI_ENV, "PAMS_ASSET_CI_TEST_DSN": CI_DSN.replace("pams_asset_test_ci", "pams")},
    {**CI_ENV, "PAMS_ASSET_CI_TEST_DSN": CI_DSN + "?hostaddr=127.0.0.1"},
    {**CI_ENV, "PAMS_ASSET_ISOLATED_TEST_DSN": LOCAL},
    {**CI_ENV, "PGHOSTADDR": "192.0.2.1"},
    {**CI_ENV, "PGSERVICE": "production"},
    {**CI_ENV, "PGOPTIONS": "-csearch_path=public"},
])
def test_rejects_unsafe_ci_targets(env):
    with pytest.raises(ValueError):
        resolve_asset_test_target(env)


@pytest.mark.parametrize("identity,version", [
    ({"name": "pams_asset_test_ci", "marker": "wrong", "host": "127.0.0.1", "port": 5432}, 160000),
    ({"name": "pams_asset_test_ci", "marker": "pams-isolated-asset-identity", "host": "192.0.2.1", "port": 5432}, 160000),
    ({"name": "pams_asset_test_ci", "marker": "pams-isolated-asset-identity", "host": "127.0.0.1", "port": 5432}, 150000),
])
def test_identity_rejection_only_reads_metadata(identity, version):
    conn = MagicMock(server_version=version)
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = identity
    with pytest.raises(ValueError):
        validate_asset_test_database(conn, "github-ci", "pams_asset_test_ci")
    assert cur.execute.call_count == 1
    assert cur.execute.call_args.args[0].startswith("SELECT ")
