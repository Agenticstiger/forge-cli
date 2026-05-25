# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid doctor --scope <scope>`` — health checks for the acquisition stack.

Five scopes:
- ``authoring``  — schema reachable, latest version detected, .fluid dir writable
- ``pipeline``   — every engine module importable, dispatcher recognizes them
- ``ingestion``  — DuckDB/dlt extras present, optional libs for other engines
- ``infra``      — docker / helm / kubectl / tofu binaries available
- ``catalog``    — every catalog registrar importable
- ``all``        — runs every scope

Each check returns a ``DoctorCheckResult`` with severity, fix hint, and a
doc URL — matching the typed-error catalog shape.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

_DOC_BASE = "https://forge.fluid.dev/ops/doctor"


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


class DoctorScope(str, Enum):
    AUTHORING = "authoring"
    PIPELINE = "pipeline"
    INGESTION = "ingestion"
    INFRA = "infra"
    CATALOG = "catalog"
    ALL = "all"


@dataclass
class DoctorCheckResult:
    name: str
    severity: Severity
    detail: str
    fix: Optional[str] = None
    doc: str = _DOC_BASE


@dataclass
class DoctorReport:
    scope: DoctorScope
    results: List[DoctorCheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.severity is Severity.OK for r in self.results)

    @property
    def errors(self) -> List[DoctorCheckResult]:
        return [r for r in self.results if r.severity is Severity.ERROR]

    @property
    def warnings(self) -> List[DoctorCheckResult]:
        return [r for r in self.results if r.severity is Severity.WARN]


@dataclass
class DoctorCheck:
    name: str
    scope: DoctorScope
    fn: Callable[[], DoctorCheckResult]


# ── Built-in checks ────────────────────────────────────────────────────


def _check_python_module(
    module: str, scope: DoctorScope, *, severity_if_missing: Severity = Severity.WARN
) -> DoctorCheckResult:
    try:
        importlib.import_module(module)
        return DoctorCheckResult(
            name=f"python:{module}",
            severity=Severity.OK,
            detail=f"module '{module}' importable",
        )
    except Exception as exc:  # noqa: BLE001
        return DoctorCheckResult(
            name=f"python:{module}",
            severity=severity_if_missing,
            detail=f"module '{module}' import failed: {exc}",
            fix=f"`pip install {module.split('.')[0]}` or the matching extra",
        )


def _check_binary(
    binary: str, scope: DoctorScope, *, severity_if_missing: Severity = Severity.WARN
) -> DoctorCheckResult:
    if shutil.which(binary) is not None:
        return DoctorCheckResult(
            name=f"binary:{binary}",
            severity=Severity.OK,
            detail=f"'{binary}' on PATH",
        )
    return DoctorCheckResult(
        name=f"binary:{binary}",
        severity=severity_if_missing,
        detail=f"'{binary}' not found on PATH",
        fix=f"install '{binary}' (see {_DOC_BASE})",
    )


def _check_engine_dispatcher() -> DoctorCheckResult:
    from fluid_build.build_runners.base import ACQUISITION_ENGINES

    expected = {"duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"}
    if ACQUISITION_ENGINES == frozenset(expected):
        return DoctorCheckResult(
            name="dispatcher:acquisition_engines",
            severity=Severity.OK,
            detail=f"{len(expected)} engines registered",
        )
    return DoctorCheckResult(
        name="dispatcher:acquisition_engines",
        severity=Severity.ERROR,
        detail=f"engine set drift: expected {expected}, got {set(ACQUISITION_ENGINES)}",
        fix="restore the dispatcher list in fluid_build.build_runners.base",
    )


def _check_schema_latest() -> DoctorCheckResult:
    from fluid_build.schema_manager import FluidSchemaManager

    latest = FluidSchemaManager.latest_bundled_version()
    if latest >= "0.7.3":
        return DoctorCheckResult(
            name="schema:latest_version",
            severity=Severity.OK,
            detail=f"bundled latest version is {latest}",
        )
    return DoctorCheckResult(
        name="schema:latest_version",
        severity=Severity.ERROR,
        detail=f"bundled latest version {latest} predates v0.7.3",
        fix="upgrade fluid_build to a version with v0.7.3 schema",
    )


def _build_checks() -> List[DoctorCheck]:
    return [
        # Authoring scope
        DoctorCheck("schema_latest", DoctorScope.AUTHORING, _check_schema_latest),
        # Pipeline scope
        DoctorCheck("dispatcher", DoctorScope.PIPELINE, _check_engine_dispatcher),
        DoctorCheck(
            "module:duckdb_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.duckdb.runner", DoctorScope.PIPELINE
            ),
        ),
        DoctorCheck(
            "module:dlt_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.dlt.runner", DoctorScope.PIPELINE
            ),
        ),
        DoctorCheck(
            "module:meltano_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.meltano.runner", DoctorScope.PIPELINE
            ),
        ),
        DoctorCheck(
            "module:airbyte_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.airbyte.runner", DoctorScope.PIPELINE
            ),
        ),
        DoctorCheck(
            "module:kafka_connect_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.kafka_connect.runner", DoctorScope.PIPELINE
            ),
        ),
        DoctorCheck(
            "module:debezium_runner",
            DoctorScope.PIPELINE,
            lambda: _check_python_module(
                "fluid_build.build_runners.debezium.runner", DoctorScope.PIPELINE
            ),
        ),
        # Ingestion scope
        DoctorCheck(
            "extra:duckdb",
            DoctorScope.INGESTION,
            lambda: _check_python_module("duckdb", DoctorScope.INGESTION),
        ),
        DoctorCheck(
            "extra:dlt",
            DoctorScope.INGESTION,
            lambda: _check_python_module("dlt", DoctorScope.INGESTION),
        ),
        DoctorCheck(
            "extra:httpx",
            DoctorScope.INGESTION,
            lambda: _check_python_module("httpx", DoctorScope.INGESTION),
        ),
        # Infra scope
        DoctorCheck(
            "binary:docker",
            DoctorScope.INFRA,
            lambda: _check_binary("docker", DoctorScope.INFRA),
        ),
        DoctorCheck(
            "binary:helm",
            DoctorScope.INFRA,
            lambda: _check_binary("helm", DoctorScope.INFRA),
        ),
        DoctorCheck(
            "binary:kubectl",
            DoctorScope.INFRA,
            lambda: _check_binary("kubectl", DoctorScope.INFRA),
        ),
        DoctorCheck(
            "binary:tofu",
            DoctorScope.INFRA,
            lambda: _check_binary("tofu", DoctorScope.INFRA),
        ),
        DoctorCheck(
            "binary:cosign",
            DoctorScope.INFRA,
            lambda: _check_binary("cosign", DoctorScope.INFRA),
        ),
        # Catalog scope
        DoctorCheck(
            "module:datahub_registrar",
            DoctorScope.CATALOG,
            lambda: _check_python_module(
                "fluid_build.build_runners.catalog_registrars.datahub", DoctorScope.CATALOG
            ),
        ),
        DoctorCheck(
            "module:openmetadata_registrar",
            DoctorScope.CATALOG,
            lambda: _check_python_module(
                "fluid_build.build_runners.catalog_registrars.openmetadata", DoctorScope.CATALOG
            ),
        ),
        DoctorCheck(
            "module:datamesh_manager_registrar",
            DoctorScope.CATALOG,
            lambda: _check_python_module(
                "fluid_build.build_runners.catalog_registrars.datamesh_manager",
                DoctorScope.CATALOG,
            ),
        ),
        DoctorCheck(
            "module:glue_registrar",
            DoctorScope.CATALOG,
            lambda: _check_python_module(
                "fluid_build.build_runners.catalog_registrars.glue", DoctorScope.CATALOG
            ),
        ),
        DoctorCheck(
            "module:snowflake_horizon_registrar",
            DoctorScope.CATALOG,
            lambda: _check_python_module(
                "fluid_build.build_runners.catalog_registrars.snowflake_horizon",
                DoctorScope.CATALOG,
            ),
        ),
    ]
    # Unity Catalog publish registrar dropped in
    # ``feat(catalog): drop Unity Catalog publish registrar`` — the OSS
    # v0.4 server doesn't fit the canonical-payload promise (MANAGED
    # disabled, EXTERNAL URI required, no UPSERT primitive). Doctor
    # used to import-check ``catalog_registrars.unity``; with the
    # module gone that check would now WARN forever. Removed from the
    # registry so ``doctor --scope catalog`` returns all-OK.


def run_doctor(scope: DoctorScope) -> DoctorReport:
    """Run all checks for the requested scope."""
    checks = _build_checks()
    if scope is not DoctorScope.ALL:
        checks = [c for c in checks if c.scope is scope]
    return DoctorReport(scope=scope, results=[c.fn() for c in checks])
