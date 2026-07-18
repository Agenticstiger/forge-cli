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
from .sources import generate_sources, generate_sources_from_logical_model

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
        output_dir: Optional[Path] = None,
        mesh_hub: Optional[str] = None,
        model_contracts: bool = False,
        tests_key: Optional[str] = None,
    ) -> GenerationResult:
        """Generate the dbt project files for one build.

        ``tests_key`` selects the YAML key data tests attach under —
        ``"tests"`` (legacy, default; the only spelling dbt-core <1.8
        understands) or ``"data_tests"`` (dbt-core >=1.8; required by the
        strict-parsing Fusion engine). Resolved at the CLI layer from the
        detected dbt binary; threaded here as a plain string because
        ``engines/`` must not import ``build_runners`` (import tiering).
        """
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
            tests_key=tests_key,
        )
        if sources_content:
            files["models/sources.yml"] = sources_content
        elif transformation_intent and transformation_intent.user_data_model:
            logical_sources = generate_sources_from_logical_model(
                transformation_intent.user_data_model
            )
            if logical_sources:
                files["models/sources.yml"] = logical_sources

        # SQL model files
        model_files = generate_models(
            contract,
            build,
            schema_context=schema_context,
            transformation_intent=transformation_intent,
        )
        files.update(model_files)

        # models/<layer>/schema.yml — per-model dbt tests. We skip this
        # when a modeling technique is set because the staged builder owns
        # the model set under that path; emitting a generic schema.yml here
        # can collide with provider-authored or scaffold-authored model docs.
        technique = transformation_intent.data_modeling_technique if transformation_intent else None
        if technique not in {"data_vault_2", "dimensional"}:
            # ``--model-contracts`` (opt-in): emit dbt model contracts on the
            # expose models. data_type must be adapter-correct (BigQuery has
            # no varchar), so resolve the adapter from the build's platform.
            from . import _types as _types

            schema_files = generate_schema_yml(
                contract,
                mesh_hub=mesh_hub,
                model_contracts=model_contracts,
                adapter=_types.adapter_for_build(build),
                tests_key=tests_key,
            )
            files.update(schema_files)
        elif mesh_hub:
            # DV2 / dimensional techniques own schema.yml themselves.
            # dependencies.yml is orthogonal to schema.yml, so we still
            # emit it when mesh_hub is set.
            from .schema_yml import _mesh_only_output

            files.update(_mesh_only_output(mesh_hub))

        # profiles.yml
        profiles_content = generate_profiles(contract, build, output_dir=output_dir)
        if profiles_content:
            files["profiles.yml"] = profiles_content

        # models/semantic_models.yml — MetricFlow bridge from the contract's
        # exposes[*].semantics block (entities/measures/dimensions/metrics).
        # Graceful no-op when no expose carries semantics.
        from .semantic_models import generate_semantic_models

        files.update(generate_semantic_models(contract))

        # packages.yml — pin the dbt packages any emitted test/model actually
        # references (dbt_utils / dbt_expectations), so the project passes
        # `dbt deps` + `dbt parse` out of the box. Emitted only when needed;
        # folds into dependencies.yml when --mesh-hub emitted one (dbt forbids
        # the two files coexisting); never clobbers a user-managed file.
        # Runs last so the namespace scan sees every generated file.
        from .packages_yml import inject_package_pins

        inject_package_pins(files, output_dir=output_dir)

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
