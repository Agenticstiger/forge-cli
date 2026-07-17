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

"""Defensive parser for dbt's ``target/run_results.json`` artifact.

``fluid apply --mode amend-and-build`` shells ``dbt build`` (models +
data-quality tests compiled from the contract's ``dq`` rules). dbt writes
its structured outcome to ``<project>/target/run_results.json`` — a stable,
versioned JSON artifact (current schema ``v6``, ``schemas.getdbt.com``).
This module parses that artifact into a small, version-agnostic shape so
the runner can turn it into a FLUID run record and ``fluid verify`` can
gate on failing contract tests.

**Why stdlib, not ``dbt-artifacts-parser``.** We surveyed
``yu-iskw/dbt-artifacts-parser`` (Apache-2.0) and the OpenMetadata fork —
both parse every ``run_results`` version into typed pydantic models. We
deliberately diverge (see the ``borrow-before-build`` receipts on the PR):

* The four fields FLUID needs — ``unique_id``, ``status``, ``failures``,
  ``execution_time`` — are **stable across run_results v1..v6**. A typed
  per-version parser buys us nothing here.
* That library pulls a heavy ``pydantic`` v2 dependency that would land on
  the light-CLI startup path, and it version-couples to dbt releases (a new
  ``vN`` needs a library bump). Defensive stdlib parsing tolerates any
  ``vN`` and never crashes on a partial / truncated artifact.

We *mirror* its version-agnostic dispatch idea (read
``metadata.dbt_schema_version`` for provenance) but keep parsing tolerant:
every field access is guarded, an unreadable / malformed artifact yields
``None`` rather than raising into the build path.

All imports here are stdlib and function-local where non-trivial, per the
light-CLI startup-budget invariant (this module is only imported from the
dbt runner's post-build hook, never on the ``fluid --help`` cold path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# dbt status vocabularies (stable across artifact versions). Tests report
# ``pass`` / ``fail`` / ``error`` / ``warn`` / ``skipped``; models report
# ``success`` / ``error`` / ``skipped``; source-freshness reports
# ``pass`` / ``warn`` / ``error`` / ``runtime error``.
OK_STATUSES = frozenset({"success", "pass"})
WARN_STATUSES = frozenset({"warn"})
SKIP_STATUSES = frozenset({"skipped"})
# "error-severity": a hard failure of a model or a contract test. dbt uses
# ``error`` for a model that blew up and for a test at ``severity: error``;
# ``fail`` for a test that returned rows; ``runtime error`` for freshness.
ERROR_STATUSES = frozenset({"error", "fail", "runtime error"})


@dataclass
class NodeResult:
    """One entry from ``run_results.json`` ``results[]`` — the version-stable
    subset FLUID cares about."""

    unique_id: str
    status: str
    failures: Optional[int] = None
    execution_time: float = 0.0
    message: Optional[str] = None

    @property
    def is_test(self) -> bool:
        # dbt unique_ids are ``<resource_type>.<package>.<name>``; test nodes
        # are ``test.…`` (both schema tests and data/unit tests).
        return self.unique_id.startswith("test.")

    @property
    def is_error(self) -> bool:
        return self.status.strip().lower() in ERROR_STATUSES

    @property
    def is_warn(self) -> bool:
        return self.status.strip().lower() in WARN_STATUSES

    @property
    def is_ok(self) -> bool:
        return self.status.strip().lower() in OK_STATUSES


@dataclass
class RunResults:
    """Parsed, version-agnostic view of a ``run_results.json`` artifact."""

    schema_version: str = ""
    dbt_version: Optional[str] = None
    invocation_id: Optional[str] = None
    generated_at: Optional[str] = None
    elapsed_time: float = 0.0
    results: List[NodeResult] = field(default_factory=list)

    # ── rollups ──────────────────────────────────────────────────────────
    @property
    def tests(self) -> List[NodeResult]:
        return [n for n in self.results if n.is_test]

    @property
    def models(self) -> List[NodeResult]:
        return [n for n in self.results if not n.is_test]

    @property
    def error_nodes(self) -> List[NodeResult]:
        return [n for n in self.results if n.is_error]

    @property
    def failed_tests(self) -> List[NodeResult]:
        return [n for n in self.tests if n.is_error]

    @property
    def warned_tests(self) -> List[NodeResult]:
        return [n for n in self.tests if n.is_warn]

    @property
    def passed_tests(self) -> List[NodeResult]:
        return [n for n in self.tests if n.is_ok]

    def counts(self) -> Dict[str, int]:
        return {
            "nodes_total": len(self.results),
            "models_total": len(self.models),
            "models_errored": sum(1 for n in self.models if n.is_error),
            "tests_total": len(self.tests),
            "tests_passed": len(self.passed_tests),
            "tests_failed": len(self.failed_tests),
            "tests_warned": len(self.warned_tests),
            "error_severity_failures": len(self.error_nodes),
        }


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion. ``failures`` is an int for tests, ``null``
    for models, and occasionally a stringified int in older adapters."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_results_path(project_dir: Path) -> Path:
    """Canonical location of the artifact for a dbt project."""
    return Path(project_dir) / "target" / "run_results.json"


def _parse_node(raw: Any) -> Optional[NodeResult]:
    if not isinstance(raw, dict):
        return None
    unique_id = raw.get("unique_id")
    if not isinstance(unique_id, str) or not unique_id:
        return None
    status = raw.get("status")
    return NodeResult(
        unique_id=unique_id,
        status=str(status) if status is not None else "unknown",
        failures=_coerce_int(raw.get("failures")),
        execution_time=_coerce_float(raw.get("execution_time")),
        message=(str(raw["message"]) if raw.get("message") is not None else None),
    )


def parse_run_results_dict(data: Any) -> Optional[RunResults]:
    """Parse an already-loaded ``run_results.json`` mapping.

    Returns ``None`` when the payload is not a dict or lacks a ``results``
    array — a malformed / truncated artifact must never raise into the
    build path.
    """
    if not isinstance(data, dict):
        return None
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return None

    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    schema_version = ""
    raw_schema = metadata.get("dbt_schema_version")
    if isinstance(raw_schema, str):
        # e.g. "https://schemas.getdbt.com/dbt/run-results/v6.json" → "v6"
        schema_version = raw_schema.rstrip("/").rsplit("/", 1)[-1].replace(".json", "")

    nodes: List[NodeResult] = []
    for raw in raw_results:
        node = _parse_node(raw)
        if node is not None:
            nodes.append(node)

    return RunResults(
        schema_version=schema_version,
        dbt_version=(str(metadata["dbt_version"]) if metadata.get("dbt_version") else None),
        invocation_id=(str(metadata["invocation_id"]) if metadata.get("invocation_id") else None),
        generated_at=(str(metadata["generated_at"]) if metadata.get("generated_at") else None),
        elapsed_time=_coerce_float(data.get("elapsed_time")),
        results=nodes,
    )


def parse_run_results(project_dir: Path) -> Optional[RunResults]:
    """Load and parse ``<project_dir>/target/run_results.json``.

    Best-effort: a missing file, an unreadable file, or malformed JSON all
    yield ``None`` rather than raising. The dbt runner treats ``None`` as
    "no artifact to record" and continues.
    """
    import json  # function-local: keep this off any hot import path

    path = run_results_path(project_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parse_run_results_dict(data)
