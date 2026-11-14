# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli.forge — exceptions, ForgeMode, CopilotAgent pure methods."""

from pathlib import Path

import pytest

from fluid_build.cli.forge import (
    ContextValidationError,
    ForgeError,
    ForgeMode,
    InvalidProjectNameError,
    ProjectGenerationError,
)

# ── Custom exceptions ──


class TestForgeExceptions:
    def test_invalid_project_name(self):
        assert issubclass(InvalidProjectNameError, ForgeError)

    def test_project_generation_error(self):
        assert issubclass(ProjectGenerationError, ForgeError)

    def test_context_validation_error(self):
        assert issubclass(ContextValidationError, ForgeError)


# ── ForgeMode ──


class TestForgeMode:
    def test_values(self):
        assert ForgeMode.AI_COPILOT.value == "copilot"
        assert ForgeMode.BLANK.value == "blank"
