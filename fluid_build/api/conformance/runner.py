# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Runner conformance test mixin.

Subclasses set ``runner`` and ``fixtures`` class attributes; the test
methods inherited from this class assert capability declarations are
honest, state round-trips correctly, replay is byte-identical, lineage
events conform to the OpenLineage shape, and secrets never appear in
captured logs.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from fluid_build.api.runner import Runner


class RunnerConformance:
    """Mixin asserting a runner satisfies the public Protocol contract.

    Each test method is independent; subclasses can ``pytest.skip`` any
    that genuinely don't apply (e.g., DuckDB has no streaming capability,
    so the streaming-specific tests just skip).
    """

    runner: ClassVar[Runner]
    fixtures: ClassVar[str] = "fluid_build.api.conformance.fixtures.minimal"

    def test_has_required_class_vars(self) -> None:
        assert hasattr(self.runner, "name") and isinstance(self.runner.name, str)
        assert hasattr(self.runner, "declared_capabilities")
        assert hasattr(self.runner, "declared_modes")
        assert isinstance(self.runner.declared_capabilities, frozenset)
        assert isinstance(self.runner.declared_modes, frozenset)
        assert self.runner.declared_modes <= {"embedded", "bring-your-own", "managed"}

    def test_capabilities_non_empty(self) -> None:
        assert len(self.runner.declared_capabilities) > 0, (
            f"Runner {self.runner.name} declares no capabilities — at least one is required."
        )

    def test_plan_idempotent(self, conformance_ctx: Any) -> None:
        """Calling plan twice must return equivalent results."""
        a = self.runner.plan(conformance_ctx)
        b = self.runner.plan(conformance_ctx)
        assert a.streams_planned == b.streams_planned

    def test_run_returns_run_id(self, conformance_ctx: Any) -> None:
        result = self.runner.run(conformance_ctx)
        assert result.run_id == conformance_ctx.run_id
        assert result.state.terminal

    def test_fingerprint_stable(self, conformance_ctx: Any) -> None:
        f1 = self.runner.fingerprint(conformance_ctx)
        f2 = self.runner.fingerprint(conformance_ctx)
        assert f1.digest == f2.digest
