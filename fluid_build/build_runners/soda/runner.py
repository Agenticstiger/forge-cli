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

"""Soda Core shell-out runner used by ``fluid test --engine soda``.

Mirrors the dbt-runner pattern (``build_runners/dbt/runner.py``) for binary
discovery: ``$SODA_EXECUTABLE`` env override → ``shutil.which("soda")`` →
fail loud (no Docker fallback for the initial version; if Soda usage takes
off we can layer that on later, the same as dbt got).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

LOG = logging.getLogger("fluid.build_runners.soda")


class SodaNotInstalled(RuntimeError):
    """Raised when the ``soda`` binary cannot be located.

    The error message includes install instructions so the operator
    doesn't have to grep upstream docs. We fail loud rather than silently
    degrading to the native engine — silent fallback would hide intent
    and make CI debugging painful.
    """


@dataclass
class SodaResult:
    """Outcome of one ``soda scan`` invocation."""

    return_code: int
    raw_stdout: str
    raw_stderr: str
    parsed: dict[str, Any] = field(default_factory=dict)
    checks_passed: int = 0
    checks_failed: int = 0
    checks_warned: int = 0
    failed_check_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.checks_failed == 0 and self.return_code == 0


def resolve_soda_executable(*, env: Optional[dict[str, str]] = None) -> str:
    """Locate the ``soda`` binary, raising :class:`SodaNotInstalled` on miss.

    Resolution order (matches the existing dbt-runner pattern in spirit):
      1. ``$SODA_EXECUTABLE`` env var, if set and pointing at an executable
      2. ``shutil.which("soda")``
    """
    e = env if env is not None else os.environ

    override = e.get("SODA_EXECUTABLE")
    if override and shutil.which(override):
        return override
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    found = shutil.which("soda")
    if found:
        return found

    raise SodaNotInstalled(
        "soda binary not found on $PATH. Install with `pip install "
        "soda-core-<your-datasource>` (e.g. soda-core-postgres) and "
        "ensure the `soda` command is on your PATH, or set $SODA_EXECUTABLE "
        "to an absolute path."
    )


def run_soda_scan(
    soda_yaml_path: str,
    *,
    datasource: str,
    config_path: Optional[str] = None,
    executable: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 600,
) -> SodaResult:
    """Run ``soda scan`` against an emitted SodaCL file and parse the result.

    Parameters
    ----------
    soda_yaml_path:
        Filesystem path to the SodaCL YAML emitted by
        :func:`fluid_build.exporters.sodacl.render_sodacl`.
    datasource:
        Soda data-source name (must already be configured in the user's
        ``configuration.yml``; see Soda Core docs).
    config_path:
        Optional path to Soda's ``configuration.yml``.
    executable:
        Pre-resolved soda binary path (skip the discovery step).
    extra_args:
        Additional CLI args to pass to ``soda scan``.
    env:
        Override the process env. Used in tests; production callers
        should leave this as ``None``.
    timeout:
        Wall-clock seconds before the scan is killed (default 10 minutes).

    Returns
    -------
    SodaResult
        Parsed result including per-check counts and failed-check names.
    """
    soda_bin = executable or resolve_soda_executable(env=env)
    cmd: list[str] = [soda_bin, "scan", "-d", datasource]
    if config_path:
        cmd.extend(["-c", config_path])
    cmd.append(soda_yaml_path)
    if extra_args:
        cmd.extend(extra_args)

    LOG.info("running soda scan: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )

    result = SodaResult(
        return_code=proc.returncode,
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
    )

    # Soda emits structured output when ``--soda-cloud`` is configured but
    # also a JSON-shaped summary block in normal stdout. We parse both
    # opportunistically; if neither is present, the result still reflects
    # the exit code.
    parsed = _parse_soda_output(proc.stdout)
    if parsed:
        result.parsed = parsed
        result.checks_passed = parsed.get("checks_passed", 0)
        result.checks_failed = parsed.get("checks_failed", 0)
        result.checks_warned = parsed.get("checks_warned", 0)
        result.failed_check_names = list(parsed.get("failed_check_names", []))

    return result


def _parse_soda_output(stdout: str) -> dict[str, Any]:
    """Best-effort extraction of check counts from Soda's stdout.

    Soda's plain stdout looks like::

        [16:42:01] Soda Core 3.x.x
        [16:42:01] Scan summary:
        [16:42:01] 4/5 checks PASSED:
        [16:42:01]     orders ...
        [16:42:01] 1/5 checks FAILED:
        [16:42:01]     orders in DataSourceName
        [16:42:01]       duplicate_count(order_id) = 0 [FAILED]
        [16:42:01]         check_value: 12
        [16:42:01] Oops! 1 failures. 0 warnings. 0 errors. 4 pass.

    We pull the totals from the summary line at the bottom. If Soda's
    output evolves and we can't find the markers, we return an empty
    dict and let the caller fall back to the exit-code-only signal.

    Also accepts a JSON-formatted line that Soda emits when
    ``--scan-as-output-json`` is set (forward-compat for cleaner parsing).
    """
    # Forward-compat: try JSON-line first.
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if "checks" in data or "checks_passed" in data:
                return _normalize_json_summary(data)

    # Legacy: parse the human-readable summary line.
    summary: dict[str, Any] = {}
    failed_names: list[str] = []
    in_failed_block = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()

        # The summary tag from Soda Core 3.x.
        # "Oops! N failures. M warnings. P errors. Q pass."
        if line.startswith("Oops!") or "failures." in line and "pass." in line:
            summary.update(_parse_summary_line(line))
        if "checks FAILED:" in line:
            in_failed_block = True
            continue
        if "checks PASSED:" in line:
            in_failed_block = False
            continue
        if in_failed_block and "[FAILED]" in line:
            # Strip the timestamp prefix if present.
            name = line.split("[FAILED]")[0].strip()
            # Soda lines start with the check expression; keep it intact.
            failed_names.append(name)

    if summary:
        summary.setdefault("checks_passed", 0)
        summary.setdefault("checks_failed", 0)
        summary.setdefault("checks_warned", 0)
        summary["failed_check_names"] = failed_names
        return summary
    return {}


def _parse_summary_line(line: str) -> dict[str, int]:
    """Pull integers out of ``Oops! 1 failures. 0 warnings. 0 errors. 4 pass.``"""
    out: dict[str, int] = {}
    for token in ("failures", "warnings", "errors", "pass"):
        marker = token
        idx = line.find(marker)
        if idx < 0:
            continue
        before = line[:idx].rstrip(". ")
        # The integer is the last whitespace-separated word.
        for word in reversed(before.split()):
            try:
                value = int(word)
                if marker == "failures":
                    out["checks_failed"] = value
                elif marker == "warnings":
                    out["checks_warned"] = value
                elif marker == "errors":
                    out["checks_errors"] = value
                elif marker == "pass":
                    out["checks_passed"] = value
                break
            except ValueError:
                continue
    return out


def _normalize_json_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce Soda's JSON-line shape into the same dict our caller expects."""
    out: dict[str, Any] = {}
    # Top-level numeric fields.
    for key in ("checks_passed", "checks_failed", "checks_warned"):
        if key in data and isinstance(data[key], int):
            out[key] = data[key]
    # Or derive from a checks[] list when present.
    checks = data.get("checks")
    if isinstance(checks, list):
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("outcome") == "pass")
        failed = sum(1 for c in checks if isinstance(c, dict) and c.get("outcome") == "fail")
        warned = sum(1 for c in checks if isinstance(c, dict) and c.get("outcome") == "warn")
        out.setdefault("checks_passed", passed)
        out.setdefault("checks_failed", failed)
        out.setdefault("checks_warned", warned)
        out["failed_check_names"] = [
            c.get("name", "") for c in checks if isinstance(c, dict) and c.get("outcome") == "fail"
        ]
    return out
