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

"""dbt transformation engine — generates dbt projects from FLUID contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import (
    GenerationResult,
    Severity,
    TransformationEngine,
    TransformationIntent,
    ValidationIssue,
)
from ..registry import register_engine
from .models import generate_models
from .profiles import generate_profiles
from .project_yml import generate_project_yml
from .schema_yml import generate_schema_yml
from .sources import generate_sources

try:
    from fluid_build.util.contract import get_build_engine, get_exposes
except ImportError:  # pragma: no cover

    def get_build_engine(build):
        return build.get("engine") or build.get("type")

    def get_exposes(contract):
        return contract.get("exposes", [])


@register_engine
class DbtEngine(TransformationEngine):
    """Generates a complete dbt project from a FLUID contract."""

    name = "dbt"
    supported_patterns = ("hybrid-reference", "embedded-logic", "multi-stage")

    def generate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
        *,
        build_index: int = 0,
        schema_context: Optional[Dict[str, Any]] = None,
        transformation_intent: Optional[TransformationIntent] = None,
        workspace_root: Optional[Path] = None,
    ) -> GenerationResult:
        files: GenerationResult = {}

        # dbt_project.yml
        files["dbt_project.yml"] = generate_project_yml(contract, build)

        # models/sources.yml — when a workspace_root is supplied we walk it
        # looking for upstream contracts so ``sources.yml`` can point at the
        # real database/schema/table rather than env_var placeholders.
        sources_content = generate_sources(
            contract,
            schema_context=schema_context,
            workspace_root=workspace_root,
        )
        if sources_content:
            files["models/sources.yml"] = sources_content

        # SQL model files
        model_files = generate_models(
            contract,
            build,
            schema_context=schema_context,
            transformation_intent=transformation_intent,
        )
        files.update(model_files)

        # models/<layer>/schema.yml (dbt tests from DQ rules)
        schema_files = generate_schema_yml(contract)
        files.update(schema_files)

        # profiles.yml
        profiles_content = generate_profiles(contract, build)
        if profiles_content:
            files["profiles.yml"] = profiles_content

        return files

    def validate(
        self,
        contract: Dict[str, Any],
        build: Dict[str, Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []

        engine = get_build_engine(build)
        if engine and engine not in ("dbt", "dbt-bigquery", "dbt-duckdb"):
            issues.append(
                ValidationIssue(
                    message=f"Engine '{engine}' is not a dbt variant",
                    severity=Severity.ERROR,
                    field="builds[].engine",
                )
            )

        pattern = build.get("pattern", "hybrid-reference")
        if pattern not in self.supported_patterns:
            issues.append(
                ValidationIssue(
                    message=f"Pattern '{pattern}' is not supported by the dbt engine",
                    severity=Severity.ERROR,
                    field="builds[].pattern",
                )
            )

        exposes = get_exposes(contract)
        if not exposes:
            issues.append(
                ValidationIssue(
                    message="Contract has no exposes[] — dbt needs at least one output to generate models",
                    severity=Severity.WARNING,
                    field="exposes",
                )
            )

        return issues
