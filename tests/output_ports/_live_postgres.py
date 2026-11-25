# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The live Postgres server the integration tests run against.

``FLUID_TEST_POSTGRES_DSN`` names the server. Without it, a module that needs it skips,
except where ``FLUID_TEST_POSTGRES_REQUIRED=1`` is set, as in the CI job that starts
the server. There a missing DSN or a missing driver fails the run, so the job cannot
pass with every live test skipped. ``tests/test_ci_postgres_live.py`` pins the job.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Tuple

import pytest

DSN_VARIABLE = "FLUID_TEST_POSTGRES_DSN"
REQUIRED_VARIABLE = "FLUID_TEST_POSTGRES_REQUIRED"


def live_postgres() -> Tuple[str, ModuleType]:
    """The server's DSN and the ``psycopg`` module, for a module to call at import.

    Skips the calling module when no server is named, and fails it instead where the
    job requires the live tests."""
    dsn = os.environ.get(DSN_VARIABLE, "")
    required = os.environ.get(REQUIRED_VARIABLE) == "1"
    if not dsn:
        if required:
            pytest.fail(
                f"{DSN_VARIABLE} is unset, but {REQUIRED_VARIABLE}=1 requires the live "
                "Postgres tests to run",
                pytrace=False,
            )
        pytest.skip(f"{DSN_VARIABLE} is unset", allow_module_level=True)
    if required:
        import psycopg

        return dsn, psycopg
    return dsn, pytest.importorskip("psycopg")
