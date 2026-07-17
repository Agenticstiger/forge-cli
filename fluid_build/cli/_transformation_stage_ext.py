# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Transformation-pattern (dbt) hooks into the ``fluid verify`` stage.

Sibling to :mod:`fluid_build.cli._acquisition_stage_ext`. Where the
acquisition extension probes that data *landed*, this one probes that the
contract's *transformations passed their tests*: it reads the run record the
dbt runner writes from ``target/run_results.json`` (see
``build_runners/dbt/runner.py::_persist_dbt_run_record``) and turns it into
``VerifyCheck``/``VerifyResult`` rows.

We reuse the acquisition extension's ``VerifyCheck`` / ``VerifyResult`` /
``latest_run_record`` so ``cli/verify.py`` renders both probe families with
one identical loop, and so a dbt run record and an acquisition run record are
read the same way.

**Why this is separate from the dbt-test *emit* side.** The contract → dbt
test compilation lives elsewhere (``exporters/dbt_tests.py`` /
``copilot/tools/dbt_test_generator.py``). This module only *consumes* the
executed result — it never emits tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# Reuse the acquisition extension's verify primitives so both probe families
# share one shape and one render loop in cli/verify.py.
from fluid_build.cli._acquisition_stage_ext import (
    VerifyCheck,
    VerifyResult,
    latest_run_record,
)

# ``cli/verify.py`` consults this set to decide which failed transformation
# checks are CRITICAL — i.e. bump ``critical_mismatch_count`` so ``--strict``
# fails the exit code (a failing contract test is a hard quality regression),
# vs. merely counting toward the summary ``mismatch_count``. "Run record
# absent" is deliberately NOT critical: ``fluid verify`` may legitimately run
# before any ``fluid apply --mode amend-and-build``, and that shouldn't hard-
# fail CI on its own.
CRITICAL_TRANSFORMATION_CHECK_NAMES = frozenset({"dbt_tests_passed", "no_error_severity_failures"})


def transformation_builds(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return builds that execute via dbt (and thus write run records).

    Excludes inline-SQL builds: an ``engine: dbt`` build that carries
    ``properties.sql`` runs through the local DuckDB engine, not dbt (see
    ``build_runners/base.py``), so it never produces a ``run_results.json``.
    """
    from fluid_build.build_runners.base import is_dbt_build, is_embedded_sql_build

    return [
        b for b in contract.get("builds", []) if is_dbt_build(b) and not is_embedded_sql_build(b)
    ]


def is_transformation_contract(contract: Dict[str, Any]) -> bool:
    return bool(transformation_builds(contract))


def _int(record_facets: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(record_facets.get(key, default))
    except (TypeError, ValueError):
        return default


def verify_transformation(contract: Dict[str, Any], workdir: Path) -> List[VerifyResult]:
    """Post-apply probes for each dbt (transformation) build.

    Checks (best-effort — missing data becomes a failed check, not a crash):

    * ``run_record_present`` — a run record exists for the build (non-critical:
      verify may run before amend-and-build).
    * ``dbt_tests_passed`` — no error-severity dbt *test* failed.
    * ``no_error_severity_failures`` — no dbt node (model or test) is at
      error severity.
    """
    results: List[VerifyResult] = []
    product_id = contract.get("id", "")
    for build in transformation_builds(contract):
        bid = build.get("id", "")
        result = VerifyResult(product_id=product_id, build_id=bid)
        record = latest_run_record(workdir, product_id, bid)

        if record is None:
            result.checks.append(
                VerifyCheck(
                    name="run_record_present",
                    passed=False,
                    detail="no dbt run record found — has 'apply --mode amend-and-build' run?",
                )
            )
            results.append(result)
            continue

        facets = record.get("facets") or {}

        tests_total = _int(facets, "tests_total")
        tests_passed = _int(facets, "tests_passed")
        tests_failed = _int(facets, "tests_failed")
        result.checks.append(
            VerifyCheck(
                name="dbt_tests_passed",
                passed=tests_failed == 0,
                detail=f"{tests_passed}/{tests_total} tests passed, {tests_failed} failed",
            )
        )

        error_failures = _int(facets, "error_severity_failures")
        result.checks.append(
            VerifyCheck(
                name="no_error_severity_failures",
                passed=error_failures == 0,
                detail=f"error_severity_failures={error_failures} (state={record.get('state')})",
            )
        )

        results.append(result)
    return results
