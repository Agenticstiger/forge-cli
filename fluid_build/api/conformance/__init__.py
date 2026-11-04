# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Conformance suite for runners and providers.

Third parties import these mixins/fixtures into their own pytest tree to
prove their implementations satisfy the public Protocol. Built-in runners
must also pass the same suite.

Usage::

    from fluid_build.api.conformance import RunnerConformance

    class TestMyRunner(RunnerConformance):
        runner = MyRunner()
        fixtures = "fluid_build.api.conformance.fixtures.minimal"
"""

from __future__ import annotations

from .runner import RunnerConformance

__all__ = ["RunnerConformance"]
