# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
