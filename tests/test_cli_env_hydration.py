# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression coverage for ``fluid_build.cli._common.hydrate_dotenv``.

The helper is called at the top of ``fluid verify`` and ``fluid publish`` to
mirror the env side-effect that ``fluid apply`` gets implicitly via the
Snowflake credential resolver chain. Without it a subprocess that only
sources a launchpad (which typically exports just ``FLUID_SECRETS_FILE``,
pointing at an ignored secrets file) sees empty ``DMM_API_KEY`` /
``SNOWFLAKE_*`` and the DMM health check fails with the generic "endpoint
not accessible" message.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluid_build.cli._common import hydrate_dotenv

_TEST_ENV_KEYS = (
    "FLUID_SECRETS_FILE",
    "DMM_API_KEY",
    "DMM_API_URL",
    "CLI_ENV_HYDRATION_TEST_KEY",
    "CLI_ENV_HYDRATION_TEST_OVERRIDE",
)


@pytest.fixture()
def _clean_test_env(monkeypatch):
    # Pre-test: monkeypatch handles delenv + restore on teardown for *its* changes.
    for key in _TEST_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    # Post-test: dotenv's ``load_dotenv`` mutates ``os.environ`` directly, so
    # monkeypatch can't revert what it never saw. Drop those keys explicitly to
    # stop leaks across tests that share the worker (e.g., provider tests that
    # assume ``DMM_API_KEY`` is unset).
    import os

    for key in _TEST_ENV_KEYS:
        os.environ.pop(key, None)


def test_hydrate_dotenv_loads_fluid_secrets_file(
    tmp_path: Path, monkeypatch, _clean_test_env
) -> None:
    # Launchpad-style: nothing in project .env, everything in the secrets file.
    secrets = tmp_path / "fluid.local.env"
    secrets.write_text("DMM_API_KEY=ed_live_from_secrets_file\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))

    hydrate_dotenv(tmp_path, environment=None)

    import os

    assert os.environ.get("DMM_API_KEY") == "ed_live_from_secrets_file"


def test_hydrate_dotenv_fluid_secrets_file_overrides_dotenv(
    tmp_path: Path, monkeypatch, _clean_test_env
) -> None:
    # Both .env and FLUID_SECRETS_FILE set the same key -> secrets file wins.
    # This matches the launchpad convention: .env ships safe defaults,
    # secrets file ships the real values.
    (tmp_path / ".env").write_text("CLI_ENV_HYDRATION_TEST_OVERRIDE=from_dotenv\n")
    secrets = tmp_path / "fluid.local.env"
    secrets.write_text("CLI_ENV_HYDRATION_TEST_OVERRIDE=from_secrets_file\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))

    hydrate_dotenv(tmp_path, environment=None)

    import os

    assert os.environ.get("CLI_ENV_HYDRATION_TEST_OVERRIDE") == "from_secrets_file"


def test_hydrate_dotenv_fluid_secrets_file_missing_is_silent_noop(
    tmp_path: Path, monkeypatch, _clean_test_env, caplog
) -> None:
    # Pointing at a non-existent file must not raise or warn — the helper is
    # convenience hydration, not a gate.
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(tmp_path / "does-not-exist.env"))

    with caplog.at_level(logging.WARNING, logger="fluid.cli.env"):
        hydrate_dotenv(tmp_path, environment=None)

    import os

    assert os.environ.get("DMM_API_KEY") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_hydrate_dotenv_reads_project_dotenv_without_secrets_file(
    tmp_path: Path, monkeypatch, _clean_test_env
) -> None:
    # Baseline: no FLUID_SECRETS_FILE, just a project .env file.
    (tmp_path / ".env").write_text("CLI_ENV_HYDRATION_TEST_KEY=from_plain_dotenv\n")

    hydrate_dotenv(tmp_path, environment=None)

    import os

    assert os.environ.get("CLI_ENV_HYDRATION_TEST_KEY") == "from_plain_dotenv"
