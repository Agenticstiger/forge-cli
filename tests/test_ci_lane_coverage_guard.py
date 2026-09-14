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

"""Tests for ``scripts/ci/assert_lane_coverage.py``.

The script is a CI gate: it fails a lane that exited 0 while skipping the
tests it advertises. A gate that misreports is worse than no gate, and this
one did — pytest's JUnit writer omits ``@file`` in this repo's configuration,
so the first implementation matched a path needle against a dotted module
name, found nothing, and declared a passing area uncovered. ``test_area_is_
covered_when_only_classname_is_present`` is that bug.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "assert_lane_coverage.py"
_spec = importlib.util.spec_from_file_location("assert_lane_coverage", _SCRIPT)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

_BQ = "tests/providers/test_bigquery_emulated_happy_path.py"


def _report(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "report.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>')
    return path


def test_area_is_covered_when_only_classname_is_present(tmp_path: Path) -> None:
    """The regression: a dotted classname must satisfy a path-shaped needle."""
    report = _report(
        tmp_path,
        '<testcase classname="tests.providers.test_bigquery_emulated_happy_path"'
        ' name="test_happy_path"/>',
    )
    assert guard.main([str(report), "--require", f"BigQuery={_BQ}"]) == 0


def test_area_is_covered_when_file_attribute_is_present(tmp_path: Path) -> None:
    """The other writer shape — an explicit @file — must work identically."""
    report = _report(tmp_path, f'<testcase file="{_BQ}" name="test_happy_path"/>')
    assert guard.main([str(report), "--require", f"BigQuery={_BQ}"]) == 0


def test_a_class_scoped_testcase_still_matches_its_file(tmp_path: Path) -> None:
    """``module.TestClass`` normalises to a path the file needle is a prefix of."""
    report = _report(
        tmp_path,
        '<testcase classname="tests.providers.test_bigquery_emulated_happy_path.TestBQ"'
        ' name="test_happy_path"/>',
    )
    assert guard.main([str(report), "--require", f"BigQuery={_BQ}"]) == 0


def test_a_skipped_area_fails_the_lane(tmp_path: Path) -> None:
    """The disease: every test skipped, pytest exits 0, the lane proved nothing."""
    report = _report(
        tmp_path,
        '<testcase classname="tests.providers.test_bigquery_emulated_happy_path"'
        ' name="test_happy_path"><skipped message="no emulator"/></testcase>',
    )
    assert guard.main([str(report), "--require", f"BigQuery={_BQ}"]) == 1


def test_a_failed_test_does_not_count_as_coverage(tmp_path: Path) -> None:
    """A failure is not a pass; the area is still unproven."""
    report = _report(
        tmp_path,
        '<testcase classname="tests.providers.test_bigquery_emulated_happy_path"'
        ' name="test_happy_path"><failure message="boom"/></testcase>',
    )
    assert guard.main([str(report), "--require", f"BigQuery={_BQ}"]) == 1


def test_one_passing_area_does_not_excuse_another(tmp_path: Path) -> None:
    """Requirements are conjunctive — a green half must not carry a dead half."""
    report = _report(
        tmp_path,
        '<testcase classname="tests.providers.test_bigquery_emulated_happy_path"'
        ' name="test_happy_path"/>',
    )
    assert (
        guard.main(
            [
                str(report),
                "--require",
                f"BigQuery={_BQ}",
                "--require",
                "AWS=tests/providers/test_aws_localstack_happy_path.py",
            ]
        )
        == 1
    )


def test_a_missing_report_fails_rather_than_passing_vacuously(tmp_path: Path) -> None:
    """No report means pytest never ran — the one case that must never be green."""
    assert guard.main([str(tmp_path / "absent.xml"), "--require", f"BigQuery={_BQ}"]) == 1


def test_no_requirements_is_a_pass(tmp_path: Path) -> None:
    """Callers that assert nothing get no opinion, not a spurious failure."""
    report = _report(tmp_path, '<testcase classname="tests.anything" name="t"/>')
    assert guard.main([str(report)]) == 0


def test_a_malformed_requirement_is_a_usage_error(tmp_path: Path) -> None:
    """A typo'd --require must not silently assert nothing."""
    report = _report(tmp_path, '<testcase classname="tests.anything" name="t"/>')
    assert guard.main([str(report), "--require", "no-equals-sign"]) == 2
