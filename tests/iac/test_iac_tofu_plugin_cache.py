# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run's one OpenTofu provider cache, against the real ``tofu``.

``tests/_infrastructure/tofu_cache.py`` gives every tofu-backed test one cache for the whole
run. Without it ``tofu init`` installs the provider into each test's own directory: a copy of
``hashicorp/aws`` is 0.75 GB, and ``tests/iac`` once wrote 15-31 GB that way. These tests run
``init`` for a small provider (``hashicorp/null``) in two directories and pin what the cache
is for. The pure choices (which directory, what an operator's setting keeps) are pinned in
``tests/_infrastructure/test_tofu_cache.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from fluid_build.iac.credentials import build_tofu_env
from tests._infrastructure import tofu_cache as cache
from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.skipif(shutil.which("tofu") is None, reason="tofu binary not installed"),
]

_MODULE = {
    "terraform": {"required_providers": {"null": {"source": "hashicorp/null", "version": "~> 3.0"}}}
}


def _init(workdir: Path) -> Path:
    """``tofu init`` in ``workdir`` (with the environment the tests inherit); the provider
    package ``init`` put under ``.terraform/providers``."""
    workdir.mkdir()
    (workdir / "main.tf.json").write_text(json.dumps(_MODULE))
    _tofu_init_or_skip(workdir)
    (package,) = (workdir / ".terraform/providers/registry.opentofu.org/hashicorp/null").glob("*/*")
    return package


def test_the_run_exports_its_cache_to_every_tofu(tofu_plugin_cache):
    assert tofu_plugin_cache is not None and tofu_plugin_cache.is_dir()
    assert os.environ[cache.CACHE_DIR_ENV] == str(tofu_plugin_cache)
    assert os.environ[cache.TRUST_CACHE_ENV]
    # The test modules that build tofu's environment get both through build_tofu_env().
    env = build_tofu_env()
    assert env[cache.CACHE_DIR_ENV] == str(tofu_plugin_cache)
    assert cache.TRUST_CACHE_ENV in env


def test_a_directory_holds_a_link_to_the_provider_not_a_copy(tmp_path, tofu_plugin_cache):
    first, second = _init(tmp_path / "first"), _init(tmp_path / "second")
    for package in (first, second):
        assert package.is_symlink(), f"{package} is a copy of the provider"
        target = os.readlink(package)
        assert target.startswith((str(tofu_plugin_cache), os.path.realpath(tofu_plugin_cache)))
    # Nothing but links and small files is left in the directory.
    for workdir in (tmp_path / "first", tmp_path / "second"):
        real_files = [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(workdir / ".terraform")
            for name in names
            if not os.path.islink(os.path.join(root, name))
        ]
        assert all(os.lstat(path).st_size < 1_000_000 for path in real_files), real_files


def test_the_second_init_uses_the_cached_package_instead_of_downloading_it_again(
    tmp_path, tofu_plugin_cache
):
    # Without the lock-file flag, OpenTofu will not trust a cached package that no lock file
    # lists: it downloads the provider again and records the registry's checksums (a ``zh:`` line
    # each). With it, ``init`` records the cached package's own hash and nothing else.
    _init(tmp_path / "first")
    _init(tmp_path / "second")
    lock = (tmp_path / "second" / ".terraform.lock.hcl").read_text()
    assert '"zh:' not in lock, (
        "the second init downloaded the provider again; is "
        f"{cache.TRUST_CACHE_ENV} off in this environment?"
    )
