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

"""Tests for the soda runner — binary resolution + output parsing.

We mock ``subprocess.run`` so these tests don't need the soda binary on PATH.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fluid_build.build_runners.soda.runner import (
    SodaNotInstalled,
    SodaResult,
    _parse_soda_output,
    resolve_soda_executable,
    run_soda_scan,
)


def test_resolve_soda_executable_uses_env_override(tmp_path, monkeypatch):
    """``$SODA_EXECUTABLE`` wins when set and pointing at an executable file."""
    fake = tmp_path / "soda"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SODA_EXECUTABLE", str(fake))
    resolved = resolve_soda_executable()
    assert resolved == str(fake)


def test_resolve_soda_executable_falls_back_to_which(monkeypatch):
    """Falls back to ``shutil.which("soda")`` when env var not set."""
    monkeypatch.delenv("SODA_EXECUTABLE", raising=False)
    with patch(
        "fluid_build.build_runners.soda.runner.shutil.which",
        return_value="/usr/local/bin/soda",
    ):
        assert resolve_soda_executable() == "/usr/local/bin/soda"


def test_resolve_soda_executable_raises_when_missing(monkeypatch):
    """``SodaNotInstalled`` is raised when soda can't be found."""
    monkeypatch.delenv("SODA_EXECUTABLE", raising=False)
    with patch("fluid_build.build_runners.soda.runner.shutil.which", return_value=None):
        with pytest.raises(SodaNotInstalled) as exc_info:
            resolve_soda_executable()
        # Error message must include install hint so operators don't have to grep docs.
        assert "pip install" in str(exc_info.value)
        assert "SODA_EXECUTABLE" in str(exc_info.value)


def test_parse_output_handles_summary_line():
    """The plain-text 'Oops!' summary line yields the right counts."""
    stdout = (
        "Scan summary:\n"
        "1/5 checks FAILED:\n"
        "    duplicate_count(order_id) = 0 [FAILED]\n"
        "      check_value: 12\n"
        "4/5 checks PASSED:\n"
        "    missing_count(order_id) = 0\n"
        "Oops! 1 failures. 0 warnings. 0 errors. 4 pass.\n"
    )
    out = _parse_soda_output(stdout)
    assert out["checks_passed"] == 4
    assert out["checks_failed"] == 1
    assert out["checks_warned"] == 0


def test_parse_output_handles_json_line():
    """A JSON-line summary (forward-compat with --scan-as-output-json) parses cleanly."""
    stdout = (
        "[16:42:01] Soda Core 3.x.x\n"
        '{"checks_passed": 3, "checks_failed": 2, "checks_warned": 1}\n'
    )
    out = _parse_soda_output(stdout)
    assert out["checks_passed"] == 3
    assert out["checks_failed"] == 2
    assert out["checks_warned"] == 1


def test_run_soda_scan_invokes_binary_with_expected_args(tmp_path):
    """``run_soda_scan`` should call subprocess.run with the canonical args."""
    fake_sodacl = tmp_path / "sodacl.yml"
    fake_sodacl.write_text("checks for X: []\n")

    class _FakeProc:
        returncode = 0
        stdout = "Oops! 0 failures. 0 warnings. 0 errors. 1 pass.\n"
        stderr = ""

    with patch(
        "fluid_build.build_runners.soda.runner.subprocess.run", return_value=_FakeProc()
    ) as run_mock:
        result = run_soda_scan(
            str(fake_sodacl),
            datasource="local-pg",
            executable="/fake/soda",
        )

    args, kwargs = run_mock.call_args
    cmd = args[0]
    assert cmd[0] == "/fake/soda"
    assert "scan" in cmd
    assert "-d" in cmd
    assert "local-pg" in cmd
    assert str(fake_sodacl) in cmd
    assert isinstance(result, SodaResult)
    assert result.ok is True
    assert result.checks_passed == 1


def test_soda_emit_junit_xml(tmp_path):
    """``--engine soda --output junit`` emits a parseable JUnit XML file."""
    from xml.etree import ElementTree as ET

    from fluid_build.build_runners.soda.runner import SodaResult
    from fluid_build.cli.test import _emit_soda_junit

    result = SodaResult(
        return_code=0,
        raw_stdout="",
        raw_stderr="",
        checks_passed=3,
        checks_failed=1,
        checks_warned=0,
        failed_check_names=["duplicate_count(order_id) = 0"],
    )
    out_path = tmp_path / "soda.xml"
    _emit_soda_junit(result, "local-pg", str(out_path))

    tree = ET.parse(str(out_path))
    suite = tree.getroot()
    assert suite.tag == "testsuite"
    assert suite.get("name") == "fluid-test-soda:local-pg"
    assert suite.get("failures") == "1"
    testcases = suite.findall("testcase")
    # One for the passed checks, one for the failed check.
    names = [tc.get("name") for tc in testcases]
    assert "duplicate_count(order_id) = 0" in names
    # The failed case carries a <failure> child.
    failed_tc = next(tc for tc in testcases if tc.get("name") == "duplicate_count(order_id) = 0")
    assert failed_tc.find("failure") is not None


def test_soda_emit_junit_redacts_stderr(tmp_path):
    """Stderr embedded in failure body must go through the secret redactor."""
    from xml.etree import ElementTree as ET

    from fluid_build.build_runners.soda.runner import SodaResult
    from fluid_build.cli.test import _emit_soda_junit

    result = SodaResult(
        return_code=2,
        raw_stdout="",
        raw_stderr="password=secret123 connection refused",
        checks_passed=0,
        checks_failed=0,
        failed_check_names=[],
    )
    out_path = tmp_path / "soda.xml"
    _emit_soda_junit(result, "local-pg", str(out_path))

    tree = ET.parse(str(out_path))
    fail_text = tree.find(".//failure").text or ""
    # The literal "secret123" must not appear in the XML — the redactor
    # masks ``password=...`` patterns.
    assert "secret123" not in fail_text


def test_run_soda_scan_propagates_failure(tmp_path):
    """A non-zero subprocess return code OR failed checks flips result.ok to False."""
    fake_sodacl = tmp_path / "sodacl.yml"
    fake_sodacl.write_text("checks for X: []\n")

    class _FakeProc:
        returncode = 2
        stdout = "Oops! 3 failures. 0 warnings. 0 errors. 0 pass.\n"
        stderr = "connection refused\n"

    with patch("fluid_build.build_runners.soda.runner.subprocess.run", return_value=_FakeProc()):
        result = run_soda_scan(str(fake_sodacl), datasource="prod", executable="/fake/soda")
    assert result.ok is False
    assert result.checks_failed == 3
    assert "connection refused" in result.raw_stderr


def test_soda_not_installed_is_exported_from_the_package():
    """``cli/test.py`` imports ``SodaNotInstalled`` from the package.

    It was defined in ``.runner`` but omitted from the package's
    ``__all__`` / re-exports, so ``fluid test --engine soda`` died with
    ``ImportError: cannot import name 'SodaNotInstalled'`` before it
    could reach either a scan or the designed "soda not installed"
    message — the advertised alternate engine was 100% unreachable.
    """
    import fluid_build.build_runners.soda as soda_pkg
    from fluid_build.build_runners.soda import SodaNotInstalled as Exported
    from fluid_build.build_runners.soda.runner import SodaNotInstalled as Defined

    assert Exported is Defined
    assert "SodaNotInstalled" in soda_pkg.__all__
