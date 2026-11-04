# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for small utility modules at 0% coverage."""

import fluid_build.tools
from fluid_build.util.schema import project_id_from_contract


def test_project_id_from_contract_returns_fallback():
    assert project_id_from_contract({}, "my-project") == "my-project"


def test_project_id_from_contract_no_fallback():
    assert project_id_from_contract({}) is None


def test_tools_all():
    assert fluid_build.tools.__all__ == []
