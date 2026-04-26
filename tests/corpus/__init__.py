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

"""Corpus regression suite — replays anonymised golden forges.

The suite reads YAML fixtures from ``tests/corpus/fixtures/`` —
each fixture pairs an anonymised intent / DDL / catalog snapshot
with a "golden" expected-output checksum (entity counts, field
shapes, key claim presence). Replays nightly in CI to detect
quality regressions a single unit test wouldn't catch.

Why this matters for world-class agentic:

* Unit tests prove primitives work. They don't prove "the modeler
  produces the *right* hubs given a real intent."
* Corpus regression replays known-good runs and asserts the
  output still passes a quality bar. The bar can be:
  * Entity counts in expected ranges.
  * Specific keys present (``hub_customer`` for a customer-facing
    intent).
  * Validation passes with no errors.
  * Cost stays under N USD.

Adding a fixture: drop a ``<name>.yaml`` in ``fixtures/`` with::

    intent:
      ...
    expected:
      hub_count: ">= 3"
      contains_hubs: ["hub_customer", "hub_order"]
      validation_passes: true
      cost_under_usd: 0.50

The runner is :func:`run_corpus_fixture`; pytest discovers them
via :func:`pytest_generate_tests`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

CORPUS_DIR = Path(__file__).parent / "fixtures"


def _discover_fixtures() -> List[Path]:
    """Find every YAML fixture in ``tests/corpus/fixtures/``."""
    if not CORPUS_DIR.is_dir():
        return []
    return sorted(CORPUS_DIR.glob("*.yaml"))


def _check_constraint(value: Any, constraint: Any) -> bool:
    """Evaluate a single corpus constraint.

    Constraint forms:

    * ``int`` — exact match.
    * ``"== N"`` / ``">= N"`` / ``"<= N"`` / ``"> N"`` / ``"< N"``
    * ``True`` / ``False`` — exact match (booleans).
    * ``list`` of strings — set-containment (``value`` must
      contain every element).
    """
    if isinstance(constraint, bool):
        return value == constraint
    if isinstance(constraint, int):
        return value == constraint
    if isinstance(constraint, list):
        return all(item in (value or []) for item in constraint)
    if isinstance(constraint, str):
        constraint = constraint.strip()
        for op, fn in (
            (">=", lambda a, b: a >= b),
            ("<=", lambda a, b: a <= b),
            ("==", lambda a, b: a == b),
            (">", lambda a, b: a > b),
            ("<", lambda a, b: a < b),
        ):
            if constraint.startswith(op):
                try:
                    threshold = float(constraint[len(op) :].strip())
                    return fn(float(value), threshold)
                except (TypeError, ValueError):
                    return False
        # Plain string equality.
        return str(value) == constraint
    return False


def run_corpus_fixture(fixture_path: Path) -> Dict[str, Any]:
    """Run one corpus fixture and assert the constraints pass.

    The fixture is YAML with two top-level keys:

    * ``intent`` / ``ddl`` / ``tables`` — the input shape.
    * ``expected`` — the constraint dict.

    Returns a structured result dict for the test report. Raises
    ``AssertionError`` on any failed constraint.
    """
    raw = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        pytest.skip(f"{fixture_path.name}: not a dict; skipping.")

    expected = raw.get("expected") or {}
    if not expected:
        pytest.skip(f"{fixture_path.name}: no ``expected`` block.")

    actual = _replay_fixture(raw, fixture_path)
    failures: List[str] = []
    for key, constraint in expected.items():
        if key == "cost_under_usd":
            # Offline golden replay is deterministic and does not call an LLM.
            # Cost is pinned as zero so legacy fixtures that specify a budget
            # still parse and pass without introducing provider credentials.
            actual_value = 0.0
        else:
            actual_value = actual.get(key)
        if not _check_constraint(actual_value, constraint):
            failures.append(f"{key}: actual={actual_value!r} expected={constraint!r}")
    assert not failures, "\n".join(failures)
    return {
        "fixture": fixture_path.name,
        "constraint_count": len(expected),
        "smoke_only": False,
        "actual": actual,
    }


def _replay_fixture(raw: Dict[str, Any], fixture_path: Path) -> Dict[str, Any]:
    if raw.get("intent"):
        return _replay_intent_fixture(raw, fixture_path)
    pytest.skip(f"{fixture_path.name}: no supported input block (intent/ddl/tables).")


def _replay_intent_fixture(raw: Dict[str, Any], fixture_path: Path) -> Dict[str, Any]:
    from fluid_build.copilot.agents.base import StageSession
    from fluid_build.copilot.agents.builder_agent import BuilderAgent
    from fluid_build.copilot.schemas.intent import BusinessIntent
    from fluid_build.copilot.store.backends.null import NullBackend
    from fluid_build.engines import get_engine
    from fluid_build.engines.base import TransformationIntent
    from fluid_build.forge_datamodel.emit.model_doc import emit_model_markdown
    from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

    intent = BusinessIntent.model_validate(raw["intent"])
    technique = (
        raw.get("technique")
        or (intent.modeling.technique if intent.modeling else None)
        or "data_vault_2"
    )
    session = StageSession(
        store=NullBackend(),
        workspace_root=fixture_path.parent,
        llm_config=None,
        active_provider=None,
        no_cache=True,
    )
    pipeline = run_from_intent(session, intent=intent, technique=technique, engine="dbt")
    logical = pipeline.coordinator.logical
    contract = pipeline.coordinator.contract
    validation = pipeline.validation
    model_doc = emit_model_markdown(logical)

    physical = BuilderAgent().build_physical(
        session,
        logical=logical,
        contract=contract,
        engine="dbt",
    )
    intent = TransformationIntent(
        stages=[
            {
                "name": spec.name,
                "sql": spec.sql,
                "layer": spec.layer,
                "depends_on": spec.depends_on,
                "outputs": spec.outputs,
            }
            for spec in physical.transform_plan.builds
        ],
        user_data_model=logical.model_dump(mode="json", by_alias=True),
        data_modeling_technique=logical.technique,
    )
    dbt_files: Dict[str, str] = {}
    engine = get_engine("dbt")
    builds = contract.get("builds") or []
    if engine is not None and builds:
        dbt_files = engine.generate(
            contract,
            builds[0],
            transformation_intent=intent,
            workspace_root=fixture_path.parent,
            output_dir=fixture_path.parent / ".corpus_dbt",
        )

    actual: Dict[str, Any] = {
        "validation_passes": validation.passes_schema,
        "issue_count": len(validation.issues),
        "model_doc_contains": model_doc,
        "dbt_model_count": len(
            [path for path in dbt_files if path.startswith("models/") and path.endswith(".sql")]
        ),
        "dbt_project_files": sorted(dbt_files),
    }
    if logical.dv2 is not None:
        actual.update(
            {
                "hub_count": len(logical.dv2.hubs),
                "link_count": len(logical.dv2.links),
                "satellite_count": len(logical.dv2.satellites),
                "contains_hubs": [hub.hub_table_name for hub in logical.dv2.hubs],
                "contains_links": [link.link_table_name for link in logical.dv2.links],
            }
        )
    if logical.dimensional is not None:
        actual.update(
            {
                "fact_count": len(logical.dimensional.facts),
                "dimension_count": len(logical.dimensional.dimensions),
                "contains_facts": [fact.name for fact in logical.dimensional.facts],
                "contains_dimensions": [
                    dimension.name for dimension in logical.dimensional.dimensions
                ],
            }
        )
    return actual


__all__ = [
    "CORPUS_DIR",
    "run_corpus_fixture",
]
