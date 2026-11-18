# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One OpenTofu provider cache for a whole pytest run.

Why. A tofu-backed test runs ``tofu init`` in its own temporary directory. With no
provider cache, ``init`` downloads the provider into that directory: ``hashicorp/aws`` is
0.75 GB unpacked, so one run of ``tests/iac`` wrote 15-31 GB into pytest's basetemp and
spent about 40 s per ``init`` on the download. With ``TF_PLUGIN_CACHE_DIR`` set, OpenTofu
installs the package into the cache once and leaves a symlink in each
``.terraform/providers``: the test directory holds no provider at all.

Two things are not enough on their own (OpenTofu 1.13, measured 2026-11-18):

* A cache per test module or per xdist worker (``tmp_path_factory.mktemp`` in each module)
  is still one copy per module per worker.
* A cache with no ``.terraform.lock.hcl`` next to the configuration is not trusted. OpenTofu
  cannot tell that the cached package is the one a lock would name, so it downloads the
  provider again on every ``init`` and checks it against the registry's checksums (43 s
  against 5 s for ``hashicorp/aws``). These test directories carry no lock file (only
  Databricks ships one), so the run also sets ``TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE``,
  which lets ``init`` use the cached package without one. The lock file ``init`` then writes
  records the hash for this platform only; forge-cli never reads it back.

Sharing. The cache sits beside the worker directories, ``<basetemp>/tofu-plugin-cache``, so
every xdist worker uses the same one (pytest-xdist's "making session-scoped fixtures
execute only once": ``getbasetemp().parent`` is the directory the workers share).
OpenTofu 1.10 and later lock the cache for concurrent use (a second ``init`` waits for the
first to finish the download); the engine floor is 1.12 (``fluid_build/iac/versions.py``).

An operator's own ``TF_PLUGIN_CACHE_DIR`` and flag are kept: CI sets one for the whole lane,
and a developer can point a run at a cache that lives across runs. Without ``tofu`` on PATH
nothing is set, so the suites that never run it see no change.

Sources: OpenTofu, "Provider Plugin Cache" (opentofu.org/docs/cli/config/config-file) and
the 1.10.0 changelog (cache locking); pytest-xdist how-to.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional

import pytest

#: OpenTofu's provider cache directory. It must exist before ``tofu init`` runs.
CACHE_DIR_ENV = "TF_PLUGIN_CACHE_DIR"

#: Use a cached package that the dependency lock file does not list.
TRUST_CACHE_ENV = "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"

#: The cache directory's name under the run's shared basetemp.
CACHE_DIR_NAME = "tofu-plugin-cache"


def cache_settings(
    environ: Mapping[str, str], basetemp: Path, *, worker: Optional[str]
) -> Dict[str, str]:
    """The OpenTofu variables a run sets: the cache directory, and the lock-file flag.

    ``basetemp`` is this process's pytest basetemp, ``worker`` its xdist worker id (``None``
    outside xdist). Under xdist the basetemp is ``<run>/popen-gwN``, so the run's shared
    directory is its parent. A variable the operator already set keeps their value.
    """
    configured = environ.get(CACHE_DIR_ENV, "").strip()
    if configured:
        directory = Path(os.path.abspath(configured))
    else:
        directory = (basetemp.parent if worker else basetemp) / CACHE_DIR_NAME
    settings = {CACHE_DIR_ENV: str(directory)}
    if TRUST_CACHE_ENV not in environ:
        settings[TRUST_CACHE_ENV] = "true"
    return settings


@pytest.fixture(scope="session", autouse=True)
def tofu_plugin_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Optional[Path]]:
    """The run's one OpenTofu provider cache, exported to every ``tofu`` the tests start.

    Autouse and session-scoped, so ``os.environ`` carries it before any other fixture runs:
    ``build_tofu_env()`` passes both variables through, and a test that calls ``tofu``
    without an environment of its own inherits them. Yields the directory, or ``None`` when
    there is no ``tofu`` on PATH or the directory cannot be made (the provider is then
    installed per test directory, as before).
    """
    if shutil.which("tofu") is None:
        yield None
        return
    settings = cache_settings(
        os.environ,
        tmp_path_factory.getbasetemp(),
        worker=os.environ.get("PYTEST_XDIST_WORKER"),
    )
    cache = Path(settings[CACHE_DIR_ENV])
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield None
        return
    with pytest.MonkeyPatch.context() as patch:
        for name, value in settings.items():
            patch.setenv(name, value)
        yield cache
