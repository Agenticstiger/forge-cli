# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SQL transformation engine — generates SQL scripts from FLUID contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import (
    GenerationResult,
    Severity,
    TransformationEngine,
    TransformationIntent,
    ValidationIssue,
)
from ..registry import register_engine
from .scripts import generate_scripts

try:
    from fluid_build.util.contract import get_build_engine
except ImportError:  # pragma: no cover

    def get_build_engine(build):
        return build.get("engine") or build.get("type")


@register_engine
class SqlEngine(TransformationEngine):
    """Generates standalone SQL scripts from a FLUID contract."""

    name = "sql"
    supported_patterns = ("embedded-logic", "multi-stage")

    def generate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
        *,
        build_index: int = 0,
        schema_context: Optional[Dict[str, Any]] = None,
        transformation_intent: Optional[TransformationIntent] = None,
    ) -> GenerationResult:
        return generate_scripts(
            contract,
            build,
            transformation_intent=transformation_intent,
        )

    def validate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []

        engine = get_build_engine(build)
        if engine and engine != "sql":
            issues.append(
                ValidationIssue(
                    message=f"Engine '{engine}' is not 'sql'",
                    severity=Severity.ERROR,
                    field="builds[].engine",
                )
            )

        pattern = build.get("pattern", "embedded-logic")
        if pattern not in self.supported_patterns:
            issues.append(
                ValidationIssue(
                    message=f"Pattern '{pattern}' is not supported by the sql engine",
                    severity=Severity.WARNING,
                    field="builds[].pattern",
                )
            )

        return issues
