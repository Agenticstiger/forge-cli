# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which provider cache a worker names, and what an operator's own setting keeps
(``tests/_infrastructure/tofu_cache.py``). The real ``tofu`` runs in
``tests/iac/test_iac_tofu_plugin_cache.py``."""

from __future__ import annotations

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


def test_both_variables_pass_through_the_environment_tofu_is_given():
    # The test modules build tofu's environment with build_tofu_env(): a name it drops would
    # leave their tofu on its own per-directory install.
    from fluid_build.iac.credentials import TOFU_ENV_NAMES

    assert {cache.CACHE_DIR_ENV, cache.TRUST_CACHE_ENV} <= TOFU_ENV_NAMES
