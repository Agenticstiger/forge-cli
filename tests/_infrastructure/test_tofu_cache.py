# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which provider cache a worker names, and what an operator's own setting keeps
(``tests/_infrastructure/tofu_cache.py``). The real ``tofu`` runs in
``tests/iac/test_iac_tofu_plugin_cache.py``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._infrastructure import tofu_cache as cache

pytestmark = pytest.mark.unit


def _named_by(worker_basetemp: Path, worker: str) -> str:
    return cache.cache_settings({}, worker_basetemp, worker=worker)[cache.CACHE_DIR_ENV]


def test_a_worker_names_the_cache_beside_the_worker_directories(tmp_path):
    run = tmp_path / "pytest-7"
    assert _named_by(run / "popen-gw3", "gw3") == str(run / cache.CACHE_DIR_NAME)


def test_every_worker_of_one_run_names_the_same_cache(tmp_path):
    run = tmp_path / "pytest-7"
    named = {_named_by(run / f"popen-gw{n}", f"gw{n}") for n in range(8)}
    assert named == {str(run / cache.CACHE_DIR_NAME)}


def test_two_runs_do_not_share_a_cache(tmp_path):
    first = _named_by(tmp_path / "pytest-7" / "popen-gw0", "gw0")
    second = _named_by(tmp_path / "pytest-8" / "popen-gw0", "gw0")
    assert first != second


def test_a_run_without_xdist_keeps_the_cache_in_its_own_basetemp(tmp_path):
    settings = cache.cache_settings({}, tmp_path / "pytest-7", worker=None)
    assert settings[cache.CACHE_DIR_ENV] == str(tmp_path / "pytest-7" / cache.CACHE_DIR_NAME)


def test_the_operators_cache_directory_is_kept(tmp_path):
    chosen = tmp_path / "ci-cache"
    settings = cache.cache_settings(
        {cache.CACHE_DIR_ENV: str(chosen)}, tmp_path / "pytest-7" / "popen-gw0", worker="gw0"
    )
    assert settings[cache.CACHE_DIR_ENV] == str(chosen)


def test_a_relative_cache_directory_is_made_absolute(tmp_path, monkeypatch):
    # tofu runs in each test's own directory, where a relative path would name another cache.
    monkeypatch.chdir(tmp_path)
    settings = cache.cache_settings({cache.CACHE_DIR_ENV: "providers"}, tmp_path, worker=None)
    assert settings[cache.CACHE_DIR_ENV] == str(tmp_path / "providers")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_cache_directory_is_not_a_choice(tmp_path, blank):
    settings = cache.cache_settings(
        {cache.CACHE_DIR_ENV: blank}, tmp_path / "pytest-7", worker=None
    )
    assert settings[cache.CACHE_DIR_ENV] == str(tmp_path / "pytest-7" / cache.CACHE_DIR_NAME)


def test_the_cached_package_is_trusted_without_a_lock_file_unless_the_operator_decided(tmp_path):
    assert cache.cache_settings({}, tmp_path, worker=None)[cache.TRUST_CACHE_ENV] == "true"
    for decided in ("0", "true", "1"):
        settings = cache.cache_settings({cache.TRUST_CACHE_ENV: decided}, tmp_path, worker=None)
        assert cache.TRUST_CACHE_ENV not in settings


class _Factory:
    """What ``provider_cache`` asks of pytest's ``tmp_path_factory``."""

    def __init__(self, basetemp: Path) -> None:
        self._basetemp = basetemp

    def getbasetemp(self) -> Path:
        return self._basetemp


@pytest.fixture
def clean_tofu_environment(monkeypatch):
    """A ``tofu`` on PATH and none of the variables the run sets."""
    monkeypatch.setattr(cache.shutil, "which", lambda name: "/usr/local/bin/tofu")
    for name in (cache.CACHE_DIR_ENV, cache.TRUST_CACHE_ENV, "PYTEST_XDIST_WORKER"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_worker_exports_the_cache_every_worker_of_the_run_shares(
    tmp_path, clean_tofu_environment
):
    # The call site, not only the helper: the fixture must hand cache_settings the xdist
    # worker id, or each worker gets a cache of its own and a run installs the provider N times.
    clean_tofu_environment.setenv("PYTEST_XDIST_WORKER", "gw2")
    run = tmp_path / "pytest-7"
    with cache.provider_cache(_Factory(run / "popen-gw2")) as directory:
        assert directory == run / cache.CACHE_DIR_NAME
        assert directory.is_dir()
        assert os.environ[cache.CACHE_DIR_ENV] == str(directory)
        assert os.environ[cache.TRUST_CACHE_ENV] == "true"
    assert cache.CACHE_DIR_ENV not in os.environ  # put back when the session ends
    assert cache.TRUST_CACHE_ENV not in os.environ


def test_without_xdist_the_cache_is_in_the_runs_own_basetemp(tmp_path, clean_tofu_environment):
    with cache.provider_cache(_Factory(tmp_path / "pytest-7")) as directory:
        assert directory == tmp_path / "pytest-7" / cache.CACHE_DIR_NAME


def test_without_tofu_nothing_is_set_or_made(tmp_path, clean_tofu_environment):
    clean_tofu_environment.setattr(cache.shutil, "which", lambda name: None)
    with cache.provider_cache(_Factory(tmp_path / "pytest-7")) as directory:
        assert directory is None
        assert cache.CACHE_DIR_ENV not in os.environ
    assert not (tmp_path / "pytest-7").exists()


def test_a_cache_directory_that_cannot_be_made_leaves_the_environment_alone(
    tmp_path, clean_tofu_environment
):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    clean_tofu_environment.setenv(cache.CACHE_DIR_ENV, str(blocker / "cache"))
    with cache.provider_cache(_Factory(tmp_path / "pytest-7")) as directory:
        assert directory is None
        assert os.environ[cache.CACHE_DIR_ENV] == str(blocker / "cache")  # the operator's, as set
        assert cache.TRUST_CACHE_ENV not in os.environ


def test_both_variables_pass_through_the_environment_tofu_is_given():
    # The test modules build tofu's environment with build_tofu_env(): a name it drops would
    # leave their tofu on its own per-directory install.
    from fluid_build.iac.credentials import TOFU_ENV_NAMES

    assert {cache.CACHE_DIR_ENV, cache.TRUST_CACHE_ENV} <= TOFU_ENV_NAMES
