# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Skip a test that needs the ``[privacy]`` extra, unless the run asks for it.

The extra brings OpenDP, mbi, Polars and JAX, which the plain test install leaves out, so
these tests skip there. CI's privacy lane sets ``FLUID_REQUIRE_OPENDP=1`` and installs the
extra: a missing package is then a failure, not a quiet skip.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType

import pytest

REQUIRED = os.environ.get("FLUID_REQUIRE_OPENDP") == "1"


def need(module: str) -> ModuleType:
    """Import *module*, or skip the test when it is missing and the run does not require it."""
    if REQUIRED:
        return importlib.import_module(module)
    return pytest.importorskip(module)
