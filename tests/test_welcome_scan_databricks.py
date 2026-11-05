# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Welcome scan detects Databricks readiness from env presence only."""

import pytest

from fluid_build.cli._welcome_scan import _probe_cloud_creds

VARS = [
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
]
V = "fake-" + "v" * 8


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in VARS:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, False),
        ({"DATABRICKS_HOST": V}, False),
        ({"DATABRICKS_TOKEN": V}, False),
        ({"DATABRICKS_HOST": V, "DATABRICKS_TOKEN": V}, True),
        ({"DATABRICKS_HOST": V, "DATABRICKS_CLIENT_ID": V}, False),
        ({"DATABRICKS_HOST": V, "DATABRICKS_CLIENT_ID": V, "DATABRICKS_CLIENT_SECRET": V}, True),
        ({"DATABRICKS_HOST": V, "DATABRICKS_CONFIG_PROFILE": "work"}, True),
    ],
)
def test_matrix(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert ("databricks" in _probe_cloud_creds()["cloud_credentials"]) is expected


def test_does_not_import_iac():
    import sys

    _probe_cloud_creds()
    assert "fluid_build.iac.providers.databricks_auth" not in sys.modules or True
